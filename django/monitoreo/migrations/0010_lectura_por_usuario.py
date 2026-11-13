from django.conf import settings
from django.db import migrations, models


def copiar_lecturas(apps, schema_editor):
    # Las alertas pendientes que ya estaban marcadas como leídas se dan por leídas para todos los usuarios
    # actuales, para que al desplegar no le vuelvan a aparecer a todo el mundo como nuevas.
    Alerta = apps.get_model('monitoreo', 'Alerta')
    Usuario = apps.get_model(*settings.AUTH_USER_MODEL.split('.'))
    usuarios = list(Usuario.objects.all())
    for alerta in Alerta.objects.filter(leida=True, atendida=False):
        alerta.leida_por.set(usuarios)


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0009_indice_historial_equipo_timestamp'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='alerta',
            name='leida_por',
            field=models.ManyToManyField(blank=True, related_name='alertas_leidas', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(copiar_lecturas, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='alerta',
            name='leida',
        ),
    ]
