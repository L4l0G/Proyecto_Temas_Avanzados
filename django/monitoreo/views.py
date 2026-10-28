import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .models import Alerta, ComandoRemoto, Equipo, PROGRAMAS_DETENIBLES
from .serializers import EquipoSerializer, HistorialEstadoSerializer

# Si un equipo no manda un reporte en ese lapso de tiempo, se muestra como desconectado/inactivo aunque su último reporte haya dicho lo contrario.
UMBRAL_SIN_REPORTE = timedelta(minutes=3)

# Umbrales de "alto" por métrica. Memoria queda más alto que CPU/disco porque en equipos con poca RAM (ej. 4GB), y es normal que el uso
# ronde 80-85% con cualquier actividad cotidiana (no es un foco rojo real
# hasta pasar de 90%).
UMBRAL_MEDIO = 60
UMBRAL_ALTO_CPU = 85
UMBRAL_ALTO_MEMORIA = 90
UMBRAL_ALTO_DISCO = 85


def _calcular_estado_visible(equipo, ultimo_reporte):
    sin_reporte_reciente = (
        ultimo_reporte is None
        or timezone.now() - ultimo_reporte.timestamp > UMBRAL_SIN_REPORTE
    )

    if equipo.estado == Equipo.Estado.MANTENIMIENTO:
        estado_visible = equipo.estado
    elif sin_reporte_reciente:
        estado_visible = Equipo.Estado.INACTIVO
        _crear_alerta(
            equipo, Alerta.Tipo.SIN_RESPUESTA, Alerta.Nivel.CRITICO,
            f'"{equipo.nombre}" no ha reportado en más de {UMBRAL_SIN_REPORTE.seconds // 60} minutos.',
        )
    else:
        estado_visible = equipo.estado

    equipo.estado_visible = estado_visible
    equipo.estado_visible_display = Equipo.Estado(estado_visible).label
    equipo.conectado_visible = bool(
        ultimo_reporte and ultimo_reporte.conectado and not sin_reporte_reciente
    )

    # Deja el cambio guardado (no solo mostrado), para que el admin y cualquier filtro por estado reflejen lo mismo que ve el personal en el tablero.
    if estado_visible != equipo.estado:
        equipo.estado = estado_visible
        equipo.save(update_fields=['estado'])


@login_required
def tablero(request):
    equipos = Equipo.objects.all()

    tipos_falla_por_equipo = {}
    for equipo_id, tipo in Alerta.objects.filter(atendida=False).values_list('equipo_id', 'tipo'):
        tipos_falla_por_equipo.setdefault(equipo_id, set()).add(tipo)

    for equipo in equipos:
        ultimo_reporte = equipo.historial.order_by('-timestamp').first()
        equipo.ultimo_reporte = ultimo_reporte  # type: ignore[attr-defined]
        _calcular_estado_visible(equipo, ultimo_reporte)
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
    consejos = []

    if not equipo.conectado_visible:
        consejos.append(
            'Este equipo no ha presentado reporte recientemente. Verifica que esté encendido, '
            'conectado a la red, y que el programa de monitoreo se encuentre corriendo.'
        )
        return consejos  

    if ultimo_reporte.cpu_porcentaje >= UMBRAL_ALTO_CPU:
        consejos.append(
            'El uso de CPU es muy alto. Se recomienda cerrar programas o ventanas que no se estén usando; '
            'si no mejora, reinicia el equipo.'
        )
    if ultimo_reporte.memoria_porcentaje >= UMBRAL_ALTO_MEMORIA:
        consejos.append(
            'El uso de memoria es muy alto. Se recomienda cerrar aplicaciones o pestañas del navegador que no '
            'se estén usando; si persiste, reinicia el equipo.'
        )
    if ultimo_reporte.disco_porcentaje >= UMBRAL_ALTO_DISCO:
        consejos.append(
            'Queda poco espacio libre en disco. Elimina archivos temporales o descargas '
            'innecesarias para liberar espacio.'
        )

    return consejos


@login_required
def equipo_detalle(request, pk):
    equipo = get_object_or_404(Equipo, pk=pk)
    ultimo_reporte = equipo.historial.order_by('-timestamp').first()  # type: ignore[attr-defined]
    _calcular_estado_visible(equipo, ultimo_reporte)
    consejos = _generar_consejos(equipo, ultimo_reporte) if ultimo_reporte else []
    metricas = None
    if ultimo_reporte:
        metricas = {
            'cpu_clase': _clase_metrica(ultimo_reporte.cpu_porcentaje, UMBRAL_ALTO_CPU),
            'memoria_clase': _clase_metrica(ultimo_reporte.memoria_porcentaje, UMBRAL_ALTO_MEMORIA),
            'disco_clase': _clase_metrica(ultimo_reporte.disco_porcentaje, UMBRAL_ALTO_DISCO),
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
    alerta.leida = True
    alerta.save(update_fields=['atendida', 'leida'])
    messages.success(request, 'Alerta marcada como atendida.')
    return redirect('tablero')


@login_required
@require_GET
def alertas_no_leidas(request):
    no_leidas = Alerta.objects.filter(leida=False).select_related('equipo').order_by('-timestamp')
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
    Alerta.objects.filter(leida=False).update(leida=True)
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

    serializer = HistorialEstadoSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    historial = serializer.save()

    # Si el equipo vuelve a reportar, la falla "sin respuesta" ya se resolvio sola: se cierra la alerta para que
    # no siga apareciendo en el filtro del tablero ni en las notificaciones.
    if historial.conectado:
        Alerta.objects.filter(
            equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA, atendida=False,
        ).update(atendida=True, leida=True)

    if historial.cpu_porcentaje >= UMBRAL_ALTO_CPU:
        _crear_alerta(
            equipo, Alerta.Tipo.CPU_ALTO, Alerta.Nivel.ADVERTENCIA,
            f'Uso de CPU muy alto en "{equipo.nombre}" ({historial.cpu_porcentaje:.0f}%).',
        )
    if historial.memoria_porcentaje >= UMBRAL_ALTO_MEMORIA:
        _crear_alerta(
            equipo, Alerta.Tipo.MEMORIA_ALTA, Alerta.Nivel.ADVERTENCIA,
            f'Uso de memoria muy alto en "{equipo.nombre}" ({historial.memoria_porcentaje:.0f}%).',
        )
    if historial.disco_porcentaje >= UMBRAL_ALTO_DISCO:
        _crear_alerta(
            equipo, Alerta.Tipo.DISCO_LLENO, Alerta.Nivel.ADVERTENCIA,
            f'Poco espacio en disco en "{equipo.nombre}" ({historial.disco_porcentaje:.0f}% usado).',
        )

    equipo.estado = Equipo.Estado.ACTIVO if historial.conectado else Equipo.Estado.INACTIVO  # type: ignore[attr-defined]

    direccion_ip = request.data.get('direccion_ip')
    if direccion_ip:
        try:
            validate_ipv46_address(direccion_ip)
        except ValidationError:
            return Response({'detail': 'direccion_ip invalida.'}, status=status.HTTP_400_BAD_REQUEST)
        equipo.direccion_ip = direccion_ip
    numero_serie = request.data.get('numero_serie')
    if numero_serie:
        equipo.numero_serie = numero_serie
    procesador = request.data.get('procesador')
    if procesador:
        equipo.procesador = procesador
    sistema_operativo = request.data.get('sistema_operativo')
    if sistema_operativo:
        equipo.sistema_operativo = sistema_operativo
    ram_total_mb = request.data.get('ram_total_mb')
    if ram_total_mb:
        equipo.ram_total_mb = ram_total_mb
    disco_total_gb = request.data.get('disco_total_gb')
    if disco_total_gb:
        equipo.disco_total_gb = disco_total_gb

    equipo.save()

    comandos_pendientes = list(equipo.comandos.filter(estado=ComandoRemoto.Estado.PENDIENTE))
    if comandos_pendientes:
        ComandoRemoto.objects.filter(
            id__in=[c.id for c in comandos_pendientes]
        ).update(estado=ComandoRemoto.Estado.ENVIADO, enviado=timezone.now())

    data = dict(serializer.data)
    data['detener_procesos'] = [c.proceso for c in comandos_pendientes]

    return Response(data, status=status.HTTP_201_CREATED)
