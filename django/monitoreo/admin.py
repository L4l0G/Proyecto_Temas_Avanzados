from django.conf import settings
from django.contrib import admin
from django.contrib.admin.actions import delete_selected
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group, User
from django.utils.text import capfirst
from .models import ComandoRemoto, Equipo, HistorialEstado, Alerta

admin.site.site_header = settings.NOMBRE_SISTEMA
admin.site.site_title = "Panel de administración"
admin.site.index_title = "Administración"


def accion_eliminar(descripcion):
    # La accion de borrado de Django usa una sola frase para todos los modelos ("Eliminar ... seleccionados/as"
    # en es-MX). Al llamarse igual (delete_selected), esta la reemplaza en cada modelo con el genero correcto.
    @admin.action(permissions=['delete'], description=descripcion)
    def delete_selected_con_descripcion(modeladmin, request, queryset):
        return delete_selected(modeladmin, request, queryset)

    delete_selected_con_descripcion.__name__ = 'delete_selected'
    return delete_selected_con_descripcion


class OcultarRelacionesInternasMixin:
    # Al borrar usuarios o grupos, la confirmacion de Django tambien lista las tablas intermedias de sus
    # permisos y grupos ("Relación user - permission: User_user_permissions object (1)"...). Son enlaces
    # internos que se borran solos, con nombres en ingles que no se pueden traducir: se quitan de la lista.
    def get_deleted_objects(self, objs, request):
        a_borrar, conteo, permisos_faltantes, protegidos = super().get_deleted_objects(objs, request)
        # Relaciones muchos-a-muchos en ambos sentidos: User.groups aparece al borrar un usuario y al borrar un grupo.
        relaciones = [campo.remote_field for campo in self.model._meta.many_to_many] + [
            rel for rel in self.model._meta.related_objects if rel.many_to_many
        ]
        intermedias = [rel.through._meta for rel in relaciones if rel.through._meta.auto_created]
        prefijos = tuple(f"{capfirst(meta.verbose_name)}: " for meta in intermedias)
        plurales = {meta.verbose_name_plural for meta in intermedias}

        def filtrar(elementos):
            resultado = []
            for elemento in elementos:
                if isinstance(elemento, list):
                    sublista = filtrar(elemento)
                    if sublista:
                        resultado.append(sublista)
                elif not str(elemento).startswith(prefijos):
                    resultado.append(elemento)
            return resultado

        conteo = {nombre: total for nombre, total in conteo.items() if nombre not in plurales}
        return filtrar(a_borrar), conteo, permisos_faltantes, protegidos


class UserAdminPersonalizado(OcultarRelacionesInternasMixin, UserAdmin):
    actions = [accion_eliminar('Eliminar usuarios seleccionados')]

    # El form de Django trae un texto explicando el algoritmo/salt/hash de la
    # contraseña, que para el personal no aporta nada util: se quita
    # dejando solo el boton de "Restablecer contraseña" (la plantilla que
    # dibuja ese boton se sobreescribio en templates/auth/widgets/).
    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if 'password' in form.base_fields:
            form.base_fields['password'].help_text = ''
        return form


admin.site.unregister(User)
admin.site.register(User, UserAdminPersonalizado)


class GroupAdminPersonalizado(OcultarRelacionesInternasMixin, GroupAdmin):
    actions = [accion_eliminar('Eliminar grupos seleccionados')]


admin.site.unregister(Group)
admin.site.register(Group, GroupAdminPersonalizado)

class EquipoAdmin(admin.ModelAdmin):
    list_display = ('id', 'nombre', 'ubicacion', 'estado', 'esta_emparejado', 'fecha_registro')
    list_filter = ('estado',)
    readonly_fields = (
        'token', 'codigo_emparejamiento', 'direccion_ip', 'numero_serie',
        'procesador', 'sistema_operativo', 'ram_total_mb', 'disco_total_gb',
    )
    actions = [accion_eliminar('Eliminar equipos seleccionados'), 'generar_codigo_de_emparejamiento']

    @admin.display(boolean=True, description='Emparejado')
    def esta_emparejado(self, obj):
        return bool(obj.token)

    @admin.action(description='Generar código de emparejamiento (invalida el token actual)')
    def generar_codigo_de_emparejamiento(self, request, queryset):
        for equipo in queryset:
            codigo = equipo.generar_codigo_emparejamiento()
            self.message_user(request, f'{equipo.nombre}: código de emparejamiento = {codigo}')

admin.site.register(Equipo, EquipoAdmin)

class HistorialEstadoAdmin(admin.ModelAdmin):
    actions = [accion_eliminar('Eliminar registros del historial seleccionados')]
    list_display = ('id', 'equipo', 'cpu_porcentaje', 'memoria_porcentaje', 'conectado', 'timestamp')
    list_filter = ('equipo', 'equipo__ubicacion', 'conectado')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

admin.site.register(HistorialEstado, HistorialEstadoAdmin)

class AlertaAdmin(admin.ModelAdmin):
    actions = [accion_eliminar('Eliminar alertas seleccionadas')]
    list_display = ('id', 'equipo', 'nivel', 'atendida', 'timestamp')
    list_filter = ('nivel','atendida')

admin.site.register(Alerta, AlertaAdmin)

class ComandoRemotoAdmin(admin.ModelAdmin):
    actions = [accion_eliminar('Eliminar comandos seleccionados')]
    list_display = ('id', 'equipo', 'proceso', 'estado', 'creado', 'enviado')
    list_filter = ('estado', 'proceso', 'equipo')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

admin.site.register(ComandoRemoto, ComandoRemotoAdmin)