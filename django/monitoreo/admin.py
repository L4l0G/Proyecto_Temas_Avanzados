from django.conf import settings
from collections import Counter

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.actions import delete_selected
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm, UserCreationForm
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


# De cada tipo de objeto relacionado se muestran solo estos en la confirmación de borrado.
MAXIMO_POR_TIPO = 5


class ConfirmacionDeBorradoMixin:
    # La página de confirmación de borrado de Django lista cada objeto relacionado, uno por uno. Con el historial
    # (1,440 reportes por equipo al día) eso daba miles de líneas. Aquí se ajusta esa lista:
    # - se quitan las tablas intermedias internas (grupos y permisos de un usuario, lecturas de alertas...): se
    #   borran solas y tienen nombres en inglés que no se pueden traducir ("Relación alerta - user");
    # - de cada tipo se muestran los primeros MAXIMO_POR_TIPO y una línea con cuántos más hay.
    #   El total exacto de cada tipo sigue en el "Resumen" de la misma página.
    def get_deleted_objects(self, objs, request):
        a_borrar, conteo, permisos_faltantes, protegidos = super().get_deleted_objects(objs, request)
        intermedias = [m._meta for m in apps.get_models(include_auto_created=True) if m._meta.auto_created]
        prefijos = tuple(f"{capfirst(meta.verbose_name)}: " for meta in intermedias)
        plurales = {meta.verbose_name_plural for meta in intermedias}

        def recortar(elementos):
            resultado, vistos, ocultos = [], Counter(), Counter()
            anterior_visible = True
            for elemento in elementos:
                if isinstance(elemento, list):
                    # Una sublista detalla al elemento anterior: solo se muestra si ese elemento se mostró.
                    sublista = recortar(elemento) if anterior_visible else []
                    if sublista:
                        resultado.append(sublista)
                    continue
                texto = str(elemento)
                if texto.startswith(prefijos):
                    anterior_visible = False
                    continue
                tipo = texto.split(':', 1)[0]
                vistos[tipo] += 1
                anterior_visible = vistos[tipo] <= MAXIMO_POR_TIPO
                if anterior_visible:
                    resultado.append(elemento)
                else:
                    ocultos[tipo] += 1
            resultado.extend(f'… y {total:,} más ({tipo})' for tipo, total in ocultos.items())
            return resultado

        conteo = {nombre: total for nombre, total in conteo.items() if nombre not in plurales}
        return recortar(a_borrar), conteo, permisos_faltantes, protegidos


class CambioContrasenaForm(AdminPasswordChangeForm):
    # Django ofrece "desactivar la autenticación basada en contraseña" pensando en sistemas con SSO o LDAP.
    # Aquí no hay otro método de inicio de sesión: desactivarla dejaría al usuario sin forma de entrar.
    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields.pop('usable_password', None)
        self.fields['password1'].required = True
        self.fields['password2'].required = True


class UserAdminPersonalizado(ConfirmacionDeBorradoMixin, UserAdmin):
    actions = [accion_eliminar('Eliminar usuarios seleccionados')]
    # Mismo motivo que CambioContrasenaForm: al crear un usuario no se ofrece la opción de dejarlo sin contraseña.
    add_form = UserCreationForm
    add_fieldsets = (
        (None, {'classes': ('wide',), 'fields': ('username', 'password1', 'password2')}),
    )
    change_password_form = CambioContrasenaForm

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


class GroupAdminPersonalizado(ConfirmacionDeBorradoMixin, GroupAdmin):
    actions = [accion_eliminar('Eliminar grupos seleccionados')]


admin.site.unregister(Group)
admin.site.register(Group, GroupAdminPersonalizado)

class EquipoAdmin(ConfirmacionDeBorradoMixin, admin.ModelAdmin):
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

class HistorialEstadoAdmin(ConfirmacionDeBorradoMixin, admin.ModelAdmin):
    actions = [accion_eliminar('Eliminar registros del historial seleccionados')]
    list_display = ('id', 'equipo', 'cpu_porcentaje', 'memoria_porcentaje', 'conectado', 'timestamp')
    list_filter = ('equipo', 'equipo__ubicacion', 'conectado')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

admin.site.register(HistorialEstado, HistorialEstadoAdmin)

class AlertaAdmin(ConfirmacionDeBorradoMixin, admin.ModelAdmin):
    actions = [accion_eliminar('Eliminar alertas seleccionadas')]
    list_display = ('id', 'equipo', 'nivel', 'atendida', 'timestamp')
    list_filter = ('nivel','atendida')

admin.site.register(Alerta, AlertaAdmin)

class ComandoRemotoAdmin(ConfirmacionDeBorradoMixin, admin.ModelAdmin):
    actions = [accion_eliminar('Eliminar comandos seleccionados')]
    list_display = ('id', 'equipo', 'proceso', 'estado', 'creado', 'enviado')
    list_filter = ('estado', 'proceso', 'equipo')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

admin.site.register(ComandoRemoto, ComandoRemotoAdmin)