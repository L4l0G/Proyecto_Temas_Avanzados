from django.conf import settings

from .models import Alerta


def alertas_no_leidas(request):
    if not request.user.is_authenticated:
        return {}
    return {
        'alertas_no_leidas_count': Alerta.objects.filter(leida=False).count(),
        'alertas_sin_atender_count': Alerta.objects.filter(atendida=False).count(),
        'alertas_pendientes': Alerta.objects.filter(atendida=False).select_related('equipo')[:10],
    }


def nombre_sistema(request):
    return {'nombre_sistema': settings.NOMBRE_SISTEMA}
