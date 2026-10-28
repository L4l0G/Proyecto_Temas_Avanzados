from django.contrib.auth.models import Permission
from django.utils import translation

ACCIONES = {
    "add": "Puede agregar",
    "change": "Puede modificar",
    "delete": "Puede eliminar",
    "view": "Puede ver",
}


def traducir_permisos():
    with translation.override("es"):
        actualizados = 0
        for permiso in Permission.objects.select_related("content_type"):
            accion, _, resto = permiso.codename.partition("_")
            if accion not in ACCIONES or resto != permiso.content_type.model:
                continue
            nombre_nuevo = f"{ACCIONES[accion]} {permiso.content_type.name}"
            if permiso.name != nombre_nuevo:
                permiso.name = nombre_nuevo
                permiso.save(update_fields=["name"])
                actualizados += 1
    return actualizados
