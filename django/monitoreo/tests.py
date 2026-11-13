from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Alerta, ComandoRemoto, Equipo, HistorialEstado
from .permisos import traducir_permisos


class EquipoModelTests(TestCase):
    def test_generar_codigo_emparejamiento_invalida_el_token_anterior(self):
        equipo = Equipo.objects.create(nombre="PC-01", token="token-viejo")

        codigo = equipo.generar_codigo_emparejamiento()

        equipo.refresh_from_db()
        self.assertEqual(len(codigo), 6)
        self.assertEqual(equipo.codigo_emparejamiento, codigo)
        self.assertEqual(equipo.token, "")


class EquipoReclamarViewTests(TestCase):
    def setUp(self):
        self.equipo = Equipo.objects.create(nombre="PC-01")
        self.equipo.generar_codigo_emparejamiento()

    def test_codigo_correcto_entrega_un_token(self):
        respuesta = self.client.post(
            reverse('equipo_reclamar', args=[self.equipo.pk]),
            {'codigo': self.equipo.codigo_emparejamiento},
        )

        self.assertEqual(respuesta.status_code, 200)
        self.equipo.refresh_from_db()
        self.assertEqual(respuesta.json()['token'], self.equipo.token)
        self.assertNotEqual(self.equipo.token, "")
        self.assertEqual(self.equipo.codigo_emparejamiento, "")

    def test_codigo_incorrecto_es_rechazado(self):
        respuesta = self.client.post(
            reverse('equipo_reclamar', args=[self.equipo.pk]),
            {'codigo': 'NOESVALIDO'},
        )

        self.assertEqual(respuesta.status_code, 403)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.token, "")

    def test_el_codigo_ya_usado_no_sirve_una_segunda_vez(self):
        codigo = self.equipo.codigo_emparejamiento
        self.client.post(reverse('equipo_reclamar', args=[self.equipo.pk]), {'codigo': codigo})

        segunda_respuesta = self.client.post(
            reverse('equipo_reclamar', args=[self.equipo.pk]), {'codigo': codigo}
        )

        self.assertEqual(segunda_respuesta.status_code, 403)


class ReportarEstadoViewTests(TestCase):
    def setUp(self):
        self.equipo = Equipo.objects.create(nombre="PC-01")
        self.equipo.token = "token-valido"
        self.equipo.save(update_fields=['token'])

    def _reportar(self, **overrides):
        datos = {
            'equipo': self.equipo.pk,
            'token': 'token-valido',
            'cpu_porcentaje': 10,
            'memoria_porcentaje': 10,
            'disco_porcentaje': 10,
            'conectado': True,
        }
        datos.update(overrides)
        return self.client.post(reverse('reportar_estado'), datos)

    def test_token_invalido_es_rechazado_y_no_guarda_historial(self):
        respuesta = self.client.post(reverse('reportar_estado'), {
            'equipo': self.equipo.pk, 'token': 'otro-token',
            'cpu_porcentaje': 1, 'memoria_porcentaje': 1, 'disco_porcentaje': 1,
            'conectado': True,
        })

        self.assertEqual(respuesta.status_code, 403)
        self.assertEqual(self.equipo.historial.count(), 0)

    def test_reporte_valido_guarda_historial_y_marca_el_equipo_activo(self):
        respuesta = self._reportar(conectado=True)

        self.assertEqual(respuesta.status_code, 201)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Equipo.Estado.ACTIVO)
        self.assertEqual(self.equipo.historial.count(), 1)

    def test_memoria_alta_solo_alerta_a_partir_de_90_por_ciento(self):
        # En equipos con poca RAM (ej. 4GB), 80-85% es uso
        # normal, por eso el umbral de memoria quedó más alto que CPU/disco.
        self._reportar(memoria_porcentaje=85)
        self.assertFalse(Alerta.objects.filter(tipo=Alerta.Tipo.MEMORIA_ALTA).exists())

        self._reportar(memoria_porcentaje=91)
        self.assertTrue(Alerta.objects.filter(tipo=Alerta.Tipo.MEMORIA_ALTA).exists())

    def test_no_duplica_alerta_mientras_la_anterior_siga_sin_atender(self):
        self._reportar(cpu_porcentaje=95)
        self._reportar(cpu_porcentaje=96)

        self.assertEqual(Alerta.objects.filter(tipo=Alerta.Tipo.CPU_ALTO).count(), 1)

    def test_vuelve_a_alertar_despues_de_que_se_atendio_la_anterior(self):
        self._reportar(cpu_porcentaje=95)
        Alerta.objects.update(atendida=True)

        self._reportar(cpu_porcentaje=96)

        self.assertEqual(Alerta.objects.filter(tipo=Alerta.Tipo.CPU_ALTO).count(), 2)

    def test_al_volver_a_reportar_se_cierra_la_alerta_sin_respuesta(self):
        Alerta.objects.create(
            equipo=self.equipo, tipo=Alerta.Tipo.SIN_RESPUESTA,
            nivel=Alerta.Nivel.CRITICO, mensaje='No ha reportado.',
        )
        Alerta.objects.create(
            equipo=self.equipo, tipo=Alerta.Tipo.DISCO_LLENO,
            nivel=Alerta.Nivel.ADVERTENCIA, mensaje='Poco espacio.',
        )

        self._reportar()

        sin_respuesta = Alerta.objects.get(tipo=Alerta.Tipo.SIN_RESPUESTA)
        self.assertTrue(sin_respuesta.atendida)
        # Las demas fallas no dependen de la conexion: se siguen atendiendo a mano.
        self.assertFalse(Alerta.objects.get(tipo=Alerta.Tipo.DISCO_LLENO).atendida)

    def test_cada_reporte_borra_el_historial_de_mas_de_30_dias_del_equipo(self):
        otro_equipo = Equipo.objects.create(nombre="PC-02")
        viejo = HistorialEstado.objects.create(equipo=self.equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1)
        reciente = HistorialEstado.objects.create(equipo=self.equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1)
        viejo_de_otro = HistorialEstado.objects.create(equipo=otro_equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1)
        HistorialEstado.objects.filter(pk__in=[viejo.pk, viejo_de_otro.pk]).update(timestamp=timezone.now() - timedelta(days=31))
        HistorialEstado.objects.filter(pk=reciente.pk).update(timestamp=timezone.now() - timedelta(days=29))

        self._reportar()

        self.assertFalse(HistorialEstado.objects.filter(pk=viejo.pk).exists())
        self.assertTrue(HistorialEstado.objects.filter(pk=reciente.pk).exists())
        # Solo se limpia el historial del equipo que reportó.
        self.assertTrue(HistorialEstado.objects.filter(pk=viejo_de_otro.pk).exists())

    def test_reenvia_comandos_remotos_pendientes_en_el_siguiente_reporte(self):
        ComandoRemoto.objects.create(equipo=self.equipo, proceso='brave.exe')

        respuesta = self._reportar()

        self.assertEqual(respuesta.json()['detener_procesos'], ['brave.exe'])
        comando = ComandoRemoto.objects.get()
        self.assertEqual(comando.estado, ComandoRemoto.Estado.ENVIADO)

    def test_direccion_ip_malformada_da_400_en_vez_de_tronar(self):
        respuesta = self._reportar(direccion_ip='no-es-una-ip')

        self.assertEqual(respuesta.status_code, 400)
        self.equipo.refresh_from_db()
        self.assertIsNone(self.equipo.direccion_ip)

    def test_direccion_ip_valida_se_guarda(self):
        self._reportar(direccion_ip='192.168.1.50')

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.direccion_ip, '192.168.1.50')

    def test_reporte_con_ip_malformada_no_guarda_nada_a_medias(self):
        # Antes el historial y las alertas se guardaban antes de revisar la IP.
        ComandoRemoto.objects.create(equipo=self.equipo, proceso='brave.exe')

        respuesta = self._reportar(cpu_porcentaje=95, direccion_ip='no-es-una-ip')

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self.equipo.historial.count(), 0)
        self.assertFalse(Alerta.objects.exists())
        self.assertEqual(ComandoRemoto.objects.get().estado, ComandoRemoto.Estado.PENDIENTE)

    def test_ram_que_no_es_numero_da_400_y_no_guarda_nada(self):
        respuesta = self._reportar(ram_total_mb='mucha')

        self.assertEqual(respuesta.status_code, 400)
        self.assertEqual(self.equipo.historial.count(), 0)

    def test_datos_de_hardware_vacios_no_borran_los_anteriores(self):
        self._reportar(procesador='AMD Ryzen 7 5700X', ram_total_mb=16384)

        self._reportar(procesador='', ram_total_mb='')

        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.procesador, 'AMD Ryzen 7 5700X')
        self.assertEqual(self.equipo.ram_total_mb, 16384)

    def test_texto_de_solo_espacios_no_borra_el_dato_anterior(self):
        self._reportar(procesador='AMD Ryzen 7 5700X')

        respuesta = self._reportar(procesador='   ')

        self.assertEqual(respuesta.status_code, 201)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.procesador, 'AMD Ryzen 7 5700X')

    def _reportar_json(self, **overrides):
        # El agente real manda JSON (requests.post(..., json=datos)), no form-data.
        datos = {
            'equipo': self.equipo.pk,
            'token': 'token-valido',
            'cpu_porcentaje': 10,
            'memoria_porcentaje': 10,
            'disco_porcentaje': 10,
            'conectado': True,
        }
        datos.update(overrides)
        return self.client.post(reverse('reportar_estado'), datos, content_type='application/json')

    def test_reporte_en_json_guarda_todos_los_datos_de_hardware(self):
        respuesta = self._reportar_json(
            direccion_ip='10.0.0.7', numero_serie='ABC123', procesador='Intel Core i5',
            sistema_operativo='Windows-11', ram_total_mb=8192, disco_total_gb=512,
        )

        self.assertEqual(respuesta.status_code, 201)
        self.assertEqual(respuesta.json()['detener_procesos'], [])
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.direccion_ip, '10.0.0.7')
        self.assertEqual(self.equipo.numero_serie, 'ABC123')
        self.assertEqual(self.equipo.procesador, 'Intel Core i5')
        self.assertEqual(self.equipo.sistema_operativo, 'Windows-11')
        self.assertEqual(self.equipo.ram_total_mb, 8192)
        self.assertEqual(self.equipo.disco_total_gb, 512)

    def test_hardware_null_o_vacio_en_json_no_borra_los_anteriores(self):
        # El agente manda None si psutil falla y "" si WMI no responde.
        self._reportar_json(direccion_ip='10.0.0.7', numero_serie='ABC123', ram_total_mb=8192, disco_total_gb=512)

        respuesta = self._reportar_json(direccion_ip='', numero_serie='', ram_total_mb=None, disco_total_gb=None)

        self.assertEqual(respuesta.status_code, 201)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.direccion_ip, '10.0.0.7')
        self.assertEqual(self.equipo.numero_serie, 'ABC123')
        self.assertEqual(self.equipo.ram_total_mb, 8192)
        self.assertEqual(self.equipo.disco_total_gb, 512)

    def test_ram_cero_en_json_no_borra_la_anterior(self):
        # 0 cuenta como "sin dato", igual que antes del cambio.
        self._reportar_json(ram_total_mb=8192)

        respuesta = self._reportar_json(ram_total_mb=0)

        self.assertEqual(respuesta.status_code, 201)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.ram_total_mb, 8192)

    def test_ram_negativa_da_400_y_no_guarda_nada(self):
        respuesta = self._reportar_json(ram_total_mb=-1)

        self.assertEqual(respuesta.status_code, 400)
        self.assertIn('ram_total_mb', respuesta.json())
        self.assertEqual(self.equipo.historial.count(), 0)

    def test_texto_de_hardware_demasiado_largo_da_400_y_no_guarda_nada(self):
        # procesador es max_length=200; antes en Postgres esto tronaba con 500 después de guardar el historial.
        respuesta = self._reportar_json(cpu_porcentaje=95, procesador='x' * 201)

        self.assertEqual(respuesta.status_code, 400)
        self.assertIn('procesador', respuesta.json())
        self.assertEqual(self.equipo.historial.count(), 0)
        self.assertFalse(Alerta.objects.exists())

    def test_error_400_trae_los_errores_de_ambos_serializers(self):
        respuesta = self._reportar_json(cpu_porcentaje='mucho', direccion_ip='no-es-una-ip')

        self.assertEqual(respuesta.status_code, 400)
        errores = respuesta.json()
        self.assertIn('cpu_porcentaje', errores)
        self.assertIn('direccion_ip', errores)

    def test_reporte_invalido_no_toca_estado_ni_comandos_ni_alertas(self):
        Equipo.objects.filter(pk=self.equipo.pk).update(estado=Equipo.Estado.MANTENIMIENTO)
        ComandoRemoto.objects.create(equipo=self.equipo, proceso='brave.exe')
        Alerta.objects.create(
            equipo=self.equipo, tipo=Alerta.Tipo.SIN_RESPUESTA,
            nivel=Alerta.Nivel.CRITICO, mensaje='No ha reportado.',
        )

        respuesta = self._reportar_json(cpu_porcentaje=95, ram_total_mb='mucha')

        self.assertEqual(respuesta.status_code, 400)
        self.assertNotIn('detener_procesos', respuesta.json())
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Equipo.Estado.MANTENIMIENTO)
        self.assertEqual(ComandoRemoto.objects.get().estado, ComandoRemoto.Estado.PENDIENTE)
        self.assertFalse(Alerta.objects.get(tipo=Alerta.Tipo.SIN_RESPUESTA).atendida)
        self.assertFalse(Alerta.objects.filter(tipo=Alerta.Tipo.CPU_ALTO).exists())

    def test_si_algo_falla_al_guardar_se_revierte_todo_el_reporte(self):
        # Simula un error a mitad del guardado (ya creado el historial) para comprobar transaction.atomic().
        ComandoRemoto.objects.create(equipo=self.equipo, proceso='brave.exe')

        with mock.patch('monitoreo.views._crear_alerta', side_effect=RuntimeError('falla simulada')):
            with self.assertRaises(RuntimeError):
                self._reportar_json(cpu_porcentaje=95, procesador='Intel Core i5')

        self.assertEqual(self.equipo.historial.count(), 0)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.procesador, '')
        self.assertEqual(ComandoRemoto.objects.get().estado, ComandoRemoto.Estado.PENDIENTE)

    def test_cada_metrica_alerta_justo_en_su_umbral_con_su_mensaje(self):
        self._reportar(cpu_porcentaje=85, memoria_porcentaje=90, disco_porcentaje=85)

        alertas = {a.tipo: a for a in Alerta.objects.all()}
        self.assertEqual(
            set(alertas), {Alerta.Tipo.CPU_ALTO, Alerta.Tipo.MEMORIA_ALTA, Alerta.Tipo.DISCO_LLENO},
        )
        self.assertTrue(all(a.nivel == Alerta.Nivel.ADVERTENCIA for a in alertas.values()))
        self.assertEqual(alertas[Alerta.Tipo.CPU_ALTO].mensaje, 'Uso de CPU muy alto en "PC-01" (85%).')
        self.assertEqual(alertas[Alerta.Tipo.MEMORIA_ALTA].mensaje, 'Uso de memoria muy alto en "PC-01" (90%).')
        self.assertEqual(alertas[Alerta.Tipo.DISCO_LLENO].mensaje, 'Poco espacio en disco en "PC-01" (85% usado).')

    def test_justo_debajo_del_umbral_no_alerta(self):
        self._reportar(cpu_porcentaje=84.9, memoria_porcentaje=89.9, disco_porcentaje=84.9)

        self.assertFalse(Alerta.objects.exists())

    def test_reporte_desconectado_marca_inactivo_y_no_cierra_sin_respuesta(self):
        Alerta.objects.create(
            equipo=self.equipo, tipo=Alerta.Tipo.SIN_RESPUESTA,
            nivel=Alerta.Nivel.CRITICO, mensaje='No ha reportado.',
        )

        respuesta = self._reportar_json(conectado=False)

        self.assertEqual(respuesta.status_code, 201)
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Equipo.Estado.INACTIVO)
        self.assertFalse(Alerta.objects.get().atendida)


class EquipoDetalleViewTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user('operador', password='clave-de-prueba-1')
        self.client.force_login(self.usuario)
        self.equipo = Equipo.objects.create(nombre="PC-01", estado=Equipo.Estado.ACTIVO)

    def _crear_reporte(self, cpu, memoria, disco, hace=None):
        historial = HistorialEstado.objects.create(
            equipo=self.equipo, cpu_porcentaje=cpu, memoria_porcentaje=memoria, disco_porcentaje=disco,
        )
        if hace:
            # update() evita auto_now_add, así se simula un reporte viejo.
            HistorialEstado.objects.filter(pk=historial.pk).update(timestamp=timezone.now() - hace)
        return historial

    def _ver_detalle(self):
        return self.client.get(reverse('equipo_detalle', args=[self.equipo.pk]))

    def test_clases_de_metricas_usan_el_umbral_de_cada_una(self):
        # 85 es "alto" para CPU pero solo "medio" para memoria (su umbral es 90).
        self._crear_reporte(cpu=85, memoria=85, disco=59)

        respuesta = self._ver_detalle()

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context['metricas'], {
            'cpu_clase': 'metric-alto',
            'memoria_clase': 'metric-medio',
            'disco_clase': 'metric-bajo',
        })

    def test_consejos_solo_para_las_metricas_altas_y_en_orden(self):
        self._crear_reporte(cpu=90, memoria=89, disco=85)

        consejos = self._ver_detalle().context['consejos']

        self.assertEqual(len(consejos), 2)
        self.assertIn('CPU', consejos[0])
        self.assertIn('disco', consejos[1])

    def test_sin_metricas_altas_no_hay_consejos(self):
        self._crear_reporte(cpu=10, memoria=10, disco=10)

        respuesta = self._ver_detalle()

        self.assertEqual(respuesta.context['consejos'], [])
        self.assertNotContains(respuesta, 'Posibles causas y qué hacer')

    def test_reporte_viejo_solo_muestra_el_consejo_de_conexion(self):
        # Aunque el último reporte tenga todo alto, si es viejo lo relevante es que el equipo no reporta.
        self._crear_reporte(cpu=99, memoria=99, disco=99, hace=timedelta(minutes=10))

        respuesta = self._ver_detalle()

        consejos = respuesta.context['consejos']
        self.assertEqual(len(consejos), 1)
        self.assertIn('no ha presentado reporte', consejos[0])
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Equipo.Estado.INACTIVO)
        self.assertTrue(Alerta.objects.filter(equipo=self.equipo, tipo=Alerta.Tipo.SIN_RESPUESTA).exists())

    def test_equipo_en_mantenimiento_sin_reportes_conserva_su_estado_y_no_alerta(self):
        Equipo.objects.filter(pk=self.equipo.pk).update(estado=Equipo.Estado.MANTENIMIENTO)

        respuesta = self._ver_detalle()

        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(respuesta.context['metricas'])
        self.assertEqual(respuesta.context['consejos'], [])
        self.assertContains(respuesta, 'En mantenimiento')
        self.assertContains(respuesta, 'Sin conexión')
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Equipo.Estado.MANTENIMIENTO)
        self.assertFalse(Alerta.objects.exists())

    def test_equipo_activo_sin_reportes_pasa_a_inactivo_y_alerta(self):
        respuesta = self._ver_detalle()

        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, 'Inactivo')
        self.equipo.refresh_from_db()
        self.assertEqual(self.equipo.estado, Equipo.Estado.INACTIVO)
        self.assertEqual(Alerta.objects.filter(tipo=Alerta.Tipo.SIN_RESPUESTA).count(), 1)

    def test_reporte_reciente_se_muestra_activo_y_conectado(self):
        self._crear_reporte(cpu=10, memoria=10, disco=10)

        respuesta = self._ver_detalle()

        self.assertContains(respuesta, 'badge badge-ok">Activo')
        self.assertContains(respuesta, 'badge badge-ok">Conectado')
        self.assertFalse(Alerta.objects.exists())

    def _dar_permisos(self, *codenames):
        permisos = Permission.objects.filter(
            content_type__app_label='monitoreo', content_type__model='equipo', codename__in=codenames,
        )
        self.usuario.user_permissions.set(permisos)

    def _equipo_con_datos_sensibles_y_emparejado(self):
        Equipo.objects.filter(pk=self.equipo.pk).update(
            numero_serie='SN-12345', direccion_ip='192.168.1.50', token='token-valido',
        )

    def test_muestra_el_formulario_para_detener_programas(self):
        self._dar_permisos('change_equipo')

        respuesta = self._ver_detalle()

        self.assertContains(respuesta, 'Detener un programa en esta PC')
        self.assertContains(respuesta, 'value="brave.exe"')

    def test_con_permiso_change_ve_desemparejar_y_botones_de_serie_e_ip(self):
        self._dar_permisos('change_equipo')
        self._equipo_con_datos_sensibles_y_emparejado()

        respuesta = self._ver_detalle()

        self.assertContains(respuesta, 'Desemparejar dispositivo')
        self.assertContains(respuesta, 'data-campo="numero_serie"')
        self.assertContains(respuesta, 'data-campo="direccion_ip"')
        # change_equipo no incluye eliminar.
        self.assertNotContains(respuesta, 'Eliminar equipo')

    def test_sin_permisos_no_ve_acciones_pero_si_estado_metricas_y_consejos(self):
        self._equipo_con_datos_sensibles_y_emparejado()
        Equipo.objects.filter(pk=self.equipo.pk).update(procesador='Intel i5')
        self._crear_reporte(cpu=95, memoria=10, disco=10)

        respuesta = self._ver_detalle()

        self.assertEqual(respuesta.status_code, 200)
        # Acciones ocultas.
        self.assertNotContains(respuesta, 'Detener un programa en esta PC')
        self.assertNotContains(respuesta, 'value="brave.exe"')
        self.assertNotContains(respuesta, reverse('equipo_detener_proceso', args=[self.equipo.pk]))
        self.assertNotContains(respuesta, 'Eliminar equipo')
        self.assertNotContains(respuesta, reverse('equipo_eliminar', args=[self.equipo.pk]))
        self.assertNotContains(respuesta, 'Desemparejar dispositivo')
        self.assertNotContains(respuesta, reverse('equipo_desemparejar', args=[self.equipo.pk]))
        self.assertNotContains(respuesta, 'data-campo="numero_serie"')
        self.assertNotContains(respuesta, 'data-campo="direccion_ip"')
        self.assertNotContains(respuesta, reverse('equipo_datos_sensibles', args=[self.equipo.pk]))
        # Información de solo lectura visible.
        self.assertContains(respuesta, 'badge badge-ok">Activo')
        self.assertContains(respuesta, 'badge badge-ok">Conectado')
        self.assertContains(respuesta, 'Dispositivo emparejado')
        self.assertContains(respuesta, 'Intel i5')
        self.assertContains(respuesta, 'class="metrica-valor metric-alto">95.0%')
        self.assertEqual(respuesta.context['metricas']['cpu_clase'], 'metric-alto')
        self.assertContains(respuesta, 'Posibles causas y qué hacer')
        self.assertEqual(len(respuesta.context['consejos']), 1)
        self.assertIn('CPU', respuesta.context['consejos'][0])

    def test_con_permiso_delete_ve_eliminar_equipo(self):
        self._dar_permisos('delete_equipo')

        respuesta = self._ver_detalle()

        self.assertContains(respuesta, 'Eliminar equipo')
        self.assertContains(respuesta, reverse('equipo_eliminar', args=[self.equipo.pk]))
        # delete_equipo no incluye las acciones de change_equipo.
        self.assertNotContains(respuesta, 'Detener un programa en esta PC')

    def test_los_mensajes_se_muestran_con_la_clase_de_su_nivel(self):
        permisos = Permission.objects.filter(content_type__app_label='monitoreo', codename='change_equipo')
        self.usuario.user_permissions.set(permisos)

        exito = self.client.post(
            reverse('equipo_detener_proceso', args=[self.equipo.pk]), {'proceso': 'brave.exe'}, follow=True,
        )
        error = self.client.post(
            reverse('equipo_detener_proceso', args=[self.equipo.pk]), {'proceso': 'explorer.exe'}, follow=True,
        )

        self.assertContains(exito, 'class="mensaje mensaje-success"')
        self.assertContains(error, 'class="mensaje mensaje-error"')


class AutorizacionEquipoViewsTests(TestCase):
    """
    equipo_eliminar/detener_proceso/desemparejar/datos_sensibles exigian
    solo sesion iniciada - cualquier cuenta podia borrar equipos o ver datos
    sensibles sin importar sus permisos. Confirma que ahora sí se exige el
    permiso correspondiente, y que una cuenta con el permiso sigue pudiendo.
    """

    def setUp(self):
        self.equipo = Equipo.objects.create(nombre="PC-01", token="token-valido")
        self.sin_permiso = User.objects.create_user('sin-permiso', password='clave-de-prueba-1')
        self.con_permiso = User.objects.create_user('con-permiso', password='clave-de-prueba-1')
        permisos = Permission.objects.filter(
            content_type__app_label='monitoreo', content_type__model='equipo',
            codename__in=['change_equipo', 'delete_equipo'],
        )
        self.con_permiso.user_permissions.set(permisos)

    def test_sin_permiso_no_puede_eliminar_equipo(self):
        self.client.force_login(self.sin_permiso)

        respuesta = self.client.post(reverse('equipo_eliminar', args=[self.equipo.pk]))

        self.assertEqual(respuesta.status_code, 403)
        self.assertTrue(Equipo.objects.filter(pk=self.equipo.pk).exists())

    def test_403_muestra_la_pagina_en_espanol_con_enlace_al_tablero(self):
        self.client.force_login(self.sin_permiso)

        respuesta = self.client.post(reverse('equipo_eliminar', args=[self.equipo.pk]))

        self.assertEqual(respuesta.status_code, 403)
        self.assertTemplateUsed(respuesta, '403.html')
        self.assertContains(respuesta, 'No tienes permiso para esta acción', status_code=403)
        self.assertContains(
            respuesta, f'href="{reverse("tablero")}" class="volver-link"', status_code=403,
        )
        self.assertContains(respuesta, 'Volver al tablero', status_code=403)
        self.assertTrue(Equipo.objects.filter(pk=self.equipo.pk).exists())

    def test_con_permiso_si_puede_eliminar_equipo(self):
        self.client.force_login(self.con_permiso)

        respuesta = self.client.post(reverse('equipo_eliminar', args=[self.equipo.pk]))

        self.assertEqual(respuesta.status_code, 302)
        self.assertFalse(Equipo.objects.filter(pk=self.equipo.pk).exists())

    def test_sin_permiso_no_puede_detener_proceso(self):
        self.client.force_login(self.sin_permiso)

        respuesta = self.client.post(
            reverse('equipo_detener_proceso', args=[self.equipo.pk]), {'proceso': 'brave.exe'},
        )

        self.assertEqual(respuesta.status_code, 403)
        self.assertFalse(ComandoRemoto.objects.exists())

    def test_sin_permiso_no_puede_desemparejar(self):
        self.client.force_login(self.sin_permiso)

        respuesta = self.client.post(reverse('equipo_desemparejar', args=[self.equipo.pk]))

        self.assertEqual(respuesta.status_code, 403)

    def test_sin_permiso_no_puede_ver_datos_sensibles(self):
        self.client.force_login(self.sin_permiso)

        respuesta = self.client.get(reverse('equipo_datos_sensibles', args=[self.equipo.pk]))

        self.assertEqual(respuesta.status_code, 403)

    def test_anonimo_se_redirige_a_login_no_403(self):
        respuesta = self.client.post(reverse('equipo_eliminar', args=[self.equipo.pk]))

        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse('login'), respuesta.url)


class AlertaViewsTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user('operador', password='clave-de-prueba-1')
        self.client.force_login(self.usuario)
        self.equipo = Equipo.objects.create(nombre="PC-01")
        self.alerta = Alerta.objects.create(
            equipo=self.equipo, tipo=Alerta.Tipo.CPU_ALTO, mensaje="CPU alto",
        )

    def test_atender_marca_la_alerta_como_atendida_y_deja_de_notificarse(self):
        respuesta = self.client.post(reverse('alerta_atender', args=[self.alerta.pk]))

        # Sin seguir la redirección: abrir el tablero crearía una alerta "sin respuesta" para este equipo de prueba.
        self.assertRedirects(respuesta, reverse('tablero'), fetch_redirect_response=False)
        self.alerta.refresh_from_db()
        self.assertTrue(self.alerta.atendida)
        self.assertEqual(self.client.get(reverse('alertas_no_leidas')).json()['total'], 0)

    def test_no_leidas_solo_cuenta_las_que_el_usuario_no_ha_visto(self):
        vista = Alerta.objects.create(equipo=self.equipo, tipo=Alerta.Tipo.DISCO_LLENO, mensaje="Disco lleno")
        vista.leida_por.add(self.usuario)

        datos = self.client.get(reverse('alertas_no_leidas')).json()

        self.assertEqual(datos['total'], 1)
        self.assertEqual(datos['alertas'][0]['id'], self.alerta.pk)

    def test_marcar_leidas_solo_aplica_al_usuario_que_abrio_la_campana(self):
        companero = User.objects.create_user('companero', password='clave-de-prueba-1')

        self.client.post(reverse('alertas_marcar_leidas'))

        self.assertEqual(self.client.get(reverse('alertas_no_leidas')).json()['total'], 0)
        self.client.force_login(companero)
        self.assertEqual(self.client.get(reverse('alertas_no_leidas')).json()['total'], 1)

    def test_las_vistas_del_tablero_exigen_inicio_de_sesion(self):
        self.client.logout()

        respuesta = self.client.get(reverse('tablero'))

        self.assertEqual(respuesta.status_code, 302)


class TableroViewTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user('operador', password='clave-de-prueba-1')
        self.client.force_login(self.usuario)

    def _equipo_desconectado(self, minutos=10):
        equipo = Equipo.objects.create(nombre="PC-01", estado=Equipo.Estado.ACTIVO)
        historial = HistorialEstado.objects.create(equipo=equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1)
        HistorialEstado.objects.filter(pk=historial.pk).update(timestamp=timezone.now() - timedelta(minutes=minutos))
        return equipo

    def test_la_alerta_sin_respuesta_atendida_no_reaparece_mientras_siga_desconectado(self):
        equipo = self._equipo_desconectado()
        self.client.get(reverse('tablero'))
        Alerta.objects.filter(equipo=equipo).update(atendida=True)

        self.client.get(reverse('tablero'))
        self.client.get(reverse('equipo_detalle', args=[equipo.pk]))

        self.assertEqual(Alerta.objects.filter(equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA).count(), 1)

    def test_una_nueva_desconexion_si_genera_otra_alerta(self):
        equipo = self._equipo_desconectado(minutos=30)
        self.client.get(reverse('tablero'))
        Alerta.objects.filter(equipo=equipo).update(atendida=True, timestamp=timezone.now() - timedelta(minutes=25))
        # El equipo volvió a reportar y luego se volvió a desconectar.
        historial = HistorialEstado.objects.create(equipo=equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1)
        HistorialEstado.objects.filter(pk=historial.pk).update(timestamp=timezone.now() - timedelta(minutes=10))

        self.client.get(reverse('tablero'))

        self.assertEqual(Alerta.objects.filter(equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA).count(), 2)

    def test_equipo_sin_reporte_reciente_se_marca_inactivo_y_genera_alerta(self):
        equipo = Equipo.objects.create(nombre="PC-01", estado=Equipo.Estado.ACTIVO)
        historial = HistorialEstado.objects.create(
            equipo=equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1,
        )
        # auto_now_add solo aplica al guardar desde la instancia; update() en
        # el queryset lo evita, asi se simula un reporte viejo.
        HistorialEstado.objects.filter(pk=historial.pk).update(
            timestamp=timezone.now() - timedelta(minutes=10)
        )

        respuesta = self.client.get(reverse('tablero'))

        self.assertEqual(respuesta.status_code, 200)
        equipo.refresh_from_db()
        self.assertEqual(equipo.estado, Equipo.Estado.INACTIVO)
        self.assertTrue(
            Alerta.objects.filter(equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA).exists()
        )

    def test_el_contexto_refleja_los_tipos_de_falla_sin_atender(self):
        equipo = Equipo.objects.create(nombre="PC-01")
        Alerta.objects.create(equipo=equipo, tipo=Alerta.Tipo.MEMORIA_ALTA, mensaje="x")
        Alerta.objects.create(
            equipo=equipo, tipo=Alerta.Tipo.DISCO_LLENO, mensaje="x", atendida=True,
        )

        respuesta = self.client.get(reverse('tablero'))

        equipo_en_contexto = respuesta.context['equipos'][0]
        self.assertEqual(equipo_en_contexto.tipos_falla, 'memoria_alta')

    def _crear_equipos_conectados(self, cantidad, desde=0):
        for i in range(desde, desde + cantidad):
            equipo = Equipo.objects.create(
                nombre=f"PC-{i:03d}", estado=Equipo.Estado.ACTIVO, ubicacion=f"Sala {i % 3}",
            )
            for minutos in (2, 1, 0):
                historial = HistorialEstado.objects.create(
                    equipo=equipo, cpu_porcentaje=10, memoria_porcentaje=10, disco_porcentaje=10,
                )
                HistorialEstado.objects.filter(pk=historial.pk).update(
                    timestamp=timezone.now() - timedelta(minutes=minutos, seconds=1)
                )

    def _contar_consultas_del_tablero(self):
        with CaptureQueriesContext(connection) as consultas:
            respuesta = self.client.get(reverse('tablero'))
        self.assertEqual(respuesta.status_code, 200)
        return len(consultas), respuesta

    def test_numero_de_consultas_no_crece_con_la_cantidad_de_equipos(self):
        self._crear_equipos_conectados(3)
        # Primera visita aparte: calienta caches de sesión/permisos para que no sesguen la comparación.
        self.client.get(reverse('tablero'))
        consultas_con_3, respuesta_3 = self._contar_consultas_del_tablero()
        self.assertEqual(len(respuesta_3.context['equipos']), 3)

        self._crear_equipos_conectados(27, desde=3)
        consultas_con_30, respuesta_30 = self._contar_consultas_del_tablero()
        self.assertEqual(len(respuesta_30.context['equipos']), 30)

        self.assertEqual(consultas_con_3, consultas_con_30)
        # Todos reportaron hace poco y conectados: no debe generarse ninguna alerta.
        self.assertFalse(Alerta.objects.exists())
        self.assertTrue(all(e.estado_visible == Equipo.Estado.ACTIVO for e in respuesta_30.context['equipos']))
        self.assertTrue(all(e.conectado_visible for e in respuesta_30.context['equipos']))

    def test_muestra_activo_al_conectado_e_inactivo_al_que_no_reporta(self):
        conectado = Equipo.objects.create(nombre="PC-Conectada", estado=Equipo.Estado.ACTIVO)
        # Un reporte viejo desconectado y uno reciente conectado: debe tomarse el más reciente.
        viejo = HistorialEstado.objects.create(
            equipo=conectado, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1, conectado=False,
        )
        HistorialEstado.objects.filter(pk=viejo.pk).update(timestamp=timezone.now() - timedelta(minutes=30))
        HistorialEstado.objects.create(
            equipo=conectado, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1, conectado=True,
        )
        sin_reporte_reciente = Equipo.objects.create(nombre="PC-Vieja", estado=Equipo.Estado.ACTIVO)
        historial = HistorialEstado.objects.create(
            equipo=sin_reporte_reciente, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1,
        )
        HistorialEstado.objects.filter(pk=historial.pk).update(timestamp=timezone.now() - timedelta(minutes=10))
        sin_reportes = Equipo.objects.create(nombre="PC-Nueva", estado=Equipo.Estado.ACTIVO)

        respuesta = self.client.get(reverse('tablero'))

        equipos = {e.pk: e for e in respuesta.context['equipos']}
        self.assertEqual(equipos[conectado.pk].estado_visible, Equipo.Estado.ACTIVO)
        self.assertTrue(equipos[conectado.pk].conectado_visible)
        for equipo in (sin_reporte_reciente, sin_reportes):
            self.assertEqual(equipos[equipo.pk].estado_visible, Equipo.Estado.INACTIVO)
            self.assertFalse(equipos[equipo.pk].conectado_visible)
            equipo.refresh_from_db()
            self.assertEqual(equipo.estado, Equipo.Estado.INACTIVO)
            self.assertTrue(Alerta.objects.filter(equipo=equipo, tipo=Alerta.Tipo.SIN_RESPUESTA).exists())
        self.assertFalse(Alerta.objects.filter(equipo=conectado).exists())
        html = respuesta.content.decode()
        self.assertRegex(html, r'data-nombre="pc-conectada"\s+data-estado="activo"')
        self.assertRegex(html, r'data-nombre="pc-vieja"\s+data-estado="inactivo"')
        self.assertRegex(html, r'data-nombre="pc-nueva"\s+data-estado="inactivo"')

    def test_reporte_reciente_desconectado_se_muestra_sin_conexion(self):
        equipo = Equipo.objects.create(nombre="PC-01", estado=Equipo.Estado.ACTIVO)
        HistorialEstado.objects.create(
            equipo=equipo, cpu_porcentaje=1, memoria_porcentaje=1, disco_porcentaje=1, conectado=False,
        )

        respuesta = self.client.get(reverse('tablero'))

        en_contexto = respuesta.context['equipos'][0]
        self.assertFalse(en_contexto.conectado_visible)
        self.assertEqual(en_contexto.estado_visible, Equipo.Estado.ACTIVO)


class PermisosTests(TestCase):
    def test_traducir_permisos_reescribe_los_nombres_en_espanol(self):
        permiso = Permission.objects.get(
            content_type__app_label='monitoreo',
            codename=f'view_{Equipo._meta.model_name}',
        )
        permiso.name = 'Can view equipo'
        permiso.save(update_fields=['name'])

        traducir_permisos()

        permiso.refresh_from_db()
        self.assertTrue(permiso.name.startswith('Puede ver'))
