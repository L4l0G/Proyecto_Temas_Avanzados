import secrets
from collections import namedtuple
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import Alerta, ComandoRemoto, Equipo, HistorialEstado, PROGRAMAS_DETENIBLES
from .serializers import EquipoSerializer, EspecificacionesSerializer, HistorialEstadoSerializer

# Si un equipo no manda un reporte en ese lapso de tiempo, se muestra como desconectado/inactivo aunque su último reporte haya dicho lo contrario.
UMBRAL_SIN_REPORTE = timedelta(minutes=3)

# Cuánto tiempo se guarda el historial de reportes. Cada equipo agrega 1,440 registros al día; los más viejos
# se borran solos al recibir cada reporte (Render en el plan gratis no permite tareas programadas).
RETENCION_HISTORIAL = timedelta(days=30)

UMBRAL_MEDIO = 60

# Cada métrica que se vigila, con su umbral de "alto", la alerta que genera y el consejo que se muestra en el detalle.
# Memoria queda más alto que CPU/disco porque en equipos con poca RAM (ej. 4GB), y es normal que el uso
# ronde 80-85% con cualquier actividad cotidiana (no es un foco rojo real hasta pasar de 90%).
Metrica = namedtuple('Metrica', 'nombre umbral_alto tipo_alerta mensaje_alerta consejo')
METRICAS = [
    Metrica(
        'cpu', 85, Alerta.Tipo.CPU_ALTO,
        'Uso de CPU muy alto en "{equipo}" ({valor:.0f}%).',
        'El uso de CPU es muy alto. Se recomienda cerrar programas o ventanas que no se estén usando; '
        'si no mejora, reinicia el equipo.',
    ),
    Metrica(
        'memoria', 90, Alerta.Tipo.MEMORIA_ALTA,
        'Uso de memoria muy alto en "{equipo}" ({valor:.0f}%).',
        'El uso de memoria es muy alto. Se recomienda cerrar aplicaciones o pestañas del navegador que no '
        'se estén usando; si persiste, reinicia el equipo.',
    ),
    Metrica(
        'disco', 85, Alerta.Tipo.DISCO_LLENO,
        'Poco espacio en disco en "{equipo}" ({valor:.0f}% usado).',
        'Queda poco espacio libre en disco. Elimina archivos temporales o descargas '
        'innecesarias para liberar espacio.',
    ),
]


def _valor(reporte, metrica):
    return getattr(reporte, f'{metrica.nombre}_porcentaje')


def _tiene_valor(valor):
    # Un texto con solo espacios cuenta como vacío (DRF lo recortaría a "" y borraría el dato guardado).
    return bool(valor.strip() if isinstance(valor, str) else valor)


def _metricas_altas(reporte):
    return [metrica for metrica in METRICAS if _valor(reporte, metrica) >= metrica.umbral_alto]


def _alerta_sin_respuesta_pendiente(ultimo_timestamp, ultima_alerta_sin_respuesta):
    # Se crea una sola alerta "sin respuesta" por cada desconexión: si ya hay una posterior al último reporte
    # (aunque alguien ya la haya atendido), no se crea otra. Antes, al atenderla, reaparecía en la siguiente
    # recarga del tablero mientras el equipo siguiera apagado.
    if ultima_alerta_sin_respuesta is None:
        return True
    return ultimo_timestamp is not None and ultima_alerta_sin_respuesta < ultimo_timestamp


def _alertas_sin_respuesta(equipo):
    return Alerta.objects.filter(equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA).order_by('-timestamp')


def _calcular_estado_visible(equipo, ultimo_timestamp, ultimo_conectado, ultima_alerta_sin_respuesta):
    # Recibe solo la fecha y la conexión del último reporte (no el reporte completo), para que el tablero
    # pueda obtenerlas de todos los equipos en una sola consulta.
    sin_reporte_reciente = (
        ultimo_timestamp is None
        or timezone.now() - ultimo_timestamp > UMBRAL_SIN_REPORTE
    )

    # Un equipo en mantenimiento conserva su estado aunque no reporte.
    if sin_reporte_reciente and equipo.estado != Equipo.Estado.MANTENIMIENTO:
        if _alerta_sin_respuesta_pendiente(ultimo_timestamp, ultima_alerta_sin_respuesta):
            _crear_alerta(
                equipo, Alerta.Tipo.SIN_RESPUESTA, Alerta.Nivel.CRITICO,
                f'"{equipo.nombre}" no ha reportado en más de {UMBRAL_SIN_REPORTE.seconds // 60} minutos.',
            )
        # Deja el cambio guardado (no solo mostrado), para que el admin y cualquier filtro por estado reflejen lo
        # mismo que ve el personal en el tablero. Se filtra por el estado que se leyó: si justo llegó un reporte y
        # el equipo pasó a activo, no se le pisa con inactivo.
        if equipo.estado != Equipo.Estado.INACTIVO:
            Equipo.objects.filter(pk=equipo.pk, estado=equipo.estado).update(estado=Equipo.Estado.INACTIVO)
            equipo.estado = Equipo.Estado.INACTIVO

    equipo.estado_visible = equipo.estado
    equipo.estado_visible_display = equipo.get_estado_display()
    equipo.conectado_visible = bool(ultimo_conectado and not sin_reporte_reciente)


@login_required
def tablero(request):
    # Antes se pedía el último reporte de cada equipo dentro del bucle (una consulta por equipo).
    # Ahora la base calcula la fecha y la conexión del último reporte de todos en la misma consulta de equipos.
    ultimo_reporte = HistorialEstado.objects.filter(equipo=OuterRef('pk')).order_by('-timestamp')
    equipos = Equipo.objects.annotate(
        ultimo_timestamp=Subquery(ultimo_reporte.values('timestamp')[:1]),
        ultimo_conectado=Subquery(ultimo_reporte.values('conectado')[:1]),
        ultima_alerta_sin_respuesta=Subquery(_alertas_sin_respuesta(OuterRef('pk')).values('timestamp')[:1]),
    )

    tipos_falla_por_equipo = {}
    for equipo_id, tipo in Alerta.objects.filter(atendida=False).values_list('equipo_id', 'tipo'):
        tipos_falla_por_equipo.setdefault(equipo_id, set()).add(tipo)

    for equipo in equipos:
        _calcular_estado_visible(
            equipo, equipo.ultimo_timestamp, equipo.ultimo_conectado, equipo.ultima_alerta_sin_respuesta,
        )
        equipo.tipos_falla = ' '.join(sorted(tipos_falla_por_equipo.get(equipo.pk, [])))  # type: ignore[attr-defined]

    ubicaciones = sorted({equipo.ubicacion for equipo in equipos if equipo.ubicacion})
    return render(request, 'monitoreo/tablero.html', {
        'equipos': equipos,
        'ubicaciones': ubicaciones,
    })


def _clase_metrica(valor, umbral_alto=85):
    if valor >= umbral_alto:
        return 'metric-alto'
    if valor >= UMBRAL_MEDIO:
        return 'metric-medio'
    return 'metric-bajo'


def _crear_alerta(equipo, tipo, nivel, mensaje):
    # get_or_create (+ la restriccion unica en Alerta.Meta) evita que dos
    # peticiones simultaneas (ej. dos usuarios cargando el tablero a
    # la vez) creen un duplicado - un exists()+create() por separado no es
    # atomico y si podia duplicar la alerta.
    Alerta.objects.get_or_create(
        equipo=equipo, tipo=tipo, atendida=False,
        defaults={'nivel': nivel, 'mensaje': mensaje},
    )


def _generar_consejos(equipo, ultimo_reporte):
    if not equipo.conectado_visible:
        return [
            'Este equipo no ha presentado reporte recientemente. Verifica que esté encendido, '
            'conectado a la red, y que el programa de monitoreo se encuentre corriendo.'
        ]
    return [metrica.consejo for metrica in _metricas_altas(ultimo_reporte)]


@login_required
def equipo_detalle(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    ultimo_reporte = equipo.historial.order_by('-timestamp').first()  # type: ignore[attr-defined]
    _calcular_estado_visible(
        equipo,
        ultimo_reporte.timestamp if ultimo_reporte else None,
        ultimo_reporte.conectado if ultimo_reporte else False,
        _alertas_sin_respuesta(equipo).values_list('timestamp', flat=True).first(),
    )
    consejos, metricas = [], None
    if ultimo_reporte:
        consejos = _generar_consejos(equipo, ultimo_reporte)
        metricas = {
            f'{metrica.nombre}_clase': _clase_metrica(_valor(ultimo_reporte, metrica), metrica.umbral_alto)
            for metrica in METRICAS
        }
    return render(request, 'monitoreo/equipo_detalle.html', {
        'equipo': equipo,
        'ultimo_reporte': ultimo_reporte,
        'consejos': consejos,
        'metricas': metricas,
        'programas_detenibles': PROGRAMAS_DETENIBLES,
    })


@login_required
@permission_required('monitoreo.change_equipo', raise_exception=True)
@require_GET
def equipo_datos_sensibles(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    return JsonResponse({
        'direccion_ip': equipo.direccion_ip or '',
        'numero_serie': equipo.numero_serie or '',
    })


@login_required
@permission_required('monitoreo.change_equipo', raise_exception=True)
@require_POST
def equipo_detener_proceso(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    proceso = request.POST.get('proceso')
    nombres_validos = {nombre for nombre, _ in PROGRAMAS_DETENIBLES}

    if proceso not in nombres_validos:
        messages.error(request, 'Programa no válido.')
        return redirect('equipo_detalle', pk=pk)

    ComandoRemoto.objects.create(equipo=equipo, proceso=proceso)
    messages.success(
        request,
        f'Se solicitó detener "{proceso}" en {equipo.nombre}. '
        '\n\nSe aplicará en el próximo reporte del equipo (máximo 1 minuto).',
    )
    return redirect('equipo_detalle', pk=pk)


@login_required
@permission_required('monitoreo.delete_equipo', raise_exception=True)
@require_POST
def equipo_eliminar(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    nombre = equipo.nombre
    equipo.delete()
    messages.success(request, f'Se eliminó el equipo "{nombre}" del tablero.')
    return redirect('tablero')


@login_required
@permission_required('monitoreo.change_equipo', raise_exception=True)
@require_POST
def equipo_desemparejar(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    if not equipo.token:
        messages.error(request, f'"{equipo.nombre}" no tiene ningún dispositivo emparejado.')
        return redirect('equipo_detalle', pk=pk)

    codigo = equipo.generar_codigo_emparejamiento()
    messages.success(
        request,
        f'Se desvinculó el dispositivo de "{equipo.nombre}". '
        f'Código de emparejamiento para el siguiente equipo: {codigo}',
    )
    return redirect('equipo_detalle', pk=pk)


@login_required
@require_POST
def alerta_atender(request, pk):
    alerta = get_object_or_404(Alerta, pk=pk)
    alerta.atendida = True
    alerta.save(update_fields=['atendida'])
    messages.success(request, 'Alerta marcada como atendida.')
    return redirect('tablero')


@login_required
@require_GET
def alertas_no_leidas(request):
    no_leidas = Alerta.no_leidas_por(request.user).select_related('equipo').order_by('-timestamp')
    return JsonResponse({
        'total': no_leidas.count(),
        'alertas': [
            {
                'id': alerta.pk,
                'equipo': alerta.equipo.nombre,
                'mensaje': alerta.mensaje,
                'nivel': alerta.nivel,
            }
            for alerta in no_leidas[:20]
        ],
    })


@login_required
@require_POST
def alertas_marcar_leidas(request):
    request.user.alertas_leidas.add(*Alerta.no_leidas_por(request.user))
    return JsonResponse({'ok': True})


@api_view(['GET'])
def listar_equipos(request):
    equipos = Equipo.objects.all()
    serializer = EquipoSerializer(equipos, many=True)
    return Response(serializer.data)


@api_view(['POST'])
def equipo_reclamar(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    codigo = str(request.data.get('codigo', '')).strip().upper()

    if not codigo or not equipo.codigo_emparejamiento or not secrets.compare_digest(codigo, equipo.codigo_emparejamiento):
        return Response({'detail': 'Código de emparejamiento inválido o ya usado.'}, status=status.HTTP_403_FORBIDDEN)

    equipo.token = secrets.token_hex(24)
    equipo.codigo_emparejamiento = ''
    equipo.save(update_fields=['token', 'codigo_emparejamiento'])

    return Response({'token': equipo.token})


@api_view(['POST'])
def reportar_estado(request):
    equipo_id = request.data.get('equipo')
    token = str(request.data.get('token', ''))

    if not equipo_id or not token:
        return Response({'detail': 'Se requiere equipo y token.'}, status=status.HTTP_400_BAD_REQUEST)

    equipo = get_object_or_404(Equipo, pk=equipo_id)
    if not equipo.token or not secrets.compare_digest(token, equipo.token):
        return Response({'detail': 'Token inválido para este equipo.'}, status=status.HTTP_403_FORBIDDEN)

    # Primero se valida todo el reporte y solo después se guarda. Antes la IP se revisaba al final: si venía mal
    # escrita, el historial y las alertas ya se habían guardado y quedaban datos a medias.
    serializer = HistorialEstadoSerializer(data=request.data)
    # Los datos de hardware que llegan vacíos (ej. WMI no respondió) no borran los que ya se tenían.
    especificaciones = EspecificacionesSerializer(
        equipo, partial=True,
        data={campo: request.data[campo] for campo in EspecificacionesSerializer.Meta.fields if _tiene_valor(request.data.get(campo))},
    )
    # Se validan los dos (no "and") para responder con todos los errores a la vez.
    validos = [serializer.is_valid(), especificaciones.is_valid()]
    if not all(validos):
        return Response({**serializer.errors, **especificaciones.errors}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        historial = serializer.save()
        HistorialEstado.objects.filter(equipo=equipo, timestamp__lt=timezone.now() - RETENCION_HISTORIAL).delete()

        # Si el equipo vuelve a reportar, la falla "sin respuesta" ya se resolvio sola: se cierra la alerta para que
        # no siga apareciendo en el filtro del tablero ni en las notificaciones.
        if historial.conectado:
            Alerta.objects.filter(
                equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA, atendida=False,
            ).update(atendida=True)

        for metrica in _metricas_altas(historial):
            _crear_alerta(
                equipo, metrica.tipo_alerta, Alerta.Nivel.ADVERTENCIA,
                metrica.mensaje_alerta.format(equipo=equipo.nombre, valor=_valor(historial, metrica)),
            )

        especificaciones.save(estado=Equipo.Estado.ACTIVO if historial.conectado else Equipo.Estado.INACTIVO)

        comandos_pendientes = list(equipo.comandos.filter(estado=ComandoRemoto.Estado.PENDIENTE))
        ComandoRemoto.objects.filter(
            id__in=[c.id for c in comandos_pendientes]
        ).update(estado=ComandoRemoto.Estado.ENVIADO, enviado=timezone.now())

    data = dict(serializer.data)
    data['detener_procesos'] = [c.proceso for c in comandos_pendientes]

    return Response(data, status=status.HTTP_201_CREATED)
