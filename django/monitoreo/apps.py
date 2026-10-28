from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _traducir_permisos_tras_migrar(sender, **kwargs):
    from .permisos import traducir_permisos

    traducir_permisos()


class MonitoreoConfig(AppConfig):
    name = 'monitoreo'

    def ready(self):
        post_migrate.connect(_traducir_permisos_tras_migrar, sender=self)
