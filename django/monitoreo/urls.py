from django.urls import path
from .views import (
    alerta_atender,
    alertas_marcar_leidas,
    alertas_no_leidas,
    equipo_datos_sensibles,
    equipo_desemparejar,
    equipo_detalle,
    equipo_detener_proceso,
    equipo_eliminar,
    equipo_reclamar,
    listar_equipos,
    reportar_estado,
    tablero,
)

urlpatterns = [
    path('', tablero, name='tablero'),
    path('alerta/<int:pk>/atender/', alerta_atender, name='alerta_atender'),
    path('api/alertas/no-leidas/', alertas_no_leidas, name='alertas_no_leidas'),
    path('api/alertas/marcar-leidas/', alertas_marcar_leidas, name='alertas_marcar_leidas'),
    path('equipo/<int:pk>/', equipo_detalle, name='equipo_detalle'),
    path('equipo/<int:pk>/eliminar/', equipo_eliminar, name='equipo_eliminar'),
    path('equipo/<int:pk>/desemparejar/', equipo_desemparejar, name='equipo_desemparejar'),
    path('equipo/<int:pk>/datos-sensibles/', equipo_datos_sensibles, name='equipo_datos_sensibles'),
    path('equipo/<int:pk>/detener/', equipo_detener_proceso, name='equipo_detener_proceso'),
    path('api/equipos/', listar_equipos, name='listar_equipos'),
    path('api/equipos/<int:pk>/reclamar/', equipo_reclamar, name='equipo_reclamar'),
    path('api/reportar/', reportar_estado, name='reportar_estado'),
]
