from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.test import TestCase
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
        self.assertTrue(sin_respuesta.leida)
        # Las demas fallas no dependen de la conexion: se siguen atendiendo a mano.
        self.assertFalse(Alerta.objects.get(tipo=Alerta.Tipo.DISCO_LLENO).atendida)

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

    def test_atender_marca_la_alerta_como_atendida_y_leida(self):
        respuesta = self.client.post(reverse('alerta_atender', args=[self.alerta.pk]))

        self.assertRedirects(respuesta, reverse('tablero'))
        self.alerta.refresh_from_db()
        self.assertTrue(self.alerta.atendida)
        self.assertTrue(self.alerta.leida)

    def test_no_leidas_solo_cuenta_las_que_no_se_han_visto(self):
        Alerta.objects.create(
            equipo=self.equipo, tipo=Alerta.Tipo.DISCO_LLENO, mensaje="Disco lleno", leida=True,
        )

        datos = self.client.get(reverse('alertas_no_leidas')).json()

        self.assertEqual(datos['total'], 1)
        self.assertEqual(datos['alertas'][0]['id'], self.alerta.pk)

    def test_marcar_leidas_las_marca_todas(self):
        self.client.post(reverse('alertas_marcar_leidas'))

        self.assertFalse(Alerta.objects.filter(leida=False).exists())

    def test_las_vistas_del_tablero_exigen_inicio_de_sesion(self):
        self.client.logout()

        respuesta = self.client.get(reverse('tablero'))

        self.assertEqual(respuesta.status_code, 302)


class TableroViewTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user('operador', password='clave-de-prueba-1')
        self.client.force_login(self.usuario)

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
