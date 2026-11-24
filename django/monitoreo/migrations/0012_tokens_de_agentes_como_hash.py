import hashlib

from django.db import migrations


def tokens_a_hash(apps, schema_editor):
    # Los tokens existentes estaban en texto plano: se reemplazan por su hash SHA-256 (igual que hash_token en
    # models.py). Los agentes ya instalados siguen enviando el mismo token, así que no hay que volver a emparejarlos.
    Equipo = apps.get_model('monitoreo', 'Equipo')
    for equipo in Equipo.objects.exclude(token=''):
        equipo.token = hashlib.sha256(equipo.token.encode()).hexdigest()
        equipo.save(update_fields=['token'])


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0011_nombres_en_espanol'),
    ]

    # No es reversible: un hash no se puede convertir de vuelta en el token original. Si se revierte,
    # los equipos afectados se vuelven a emparejar con un código nuevo.
    operations = [
        migrations.RunPython(tokens_a_hash, migrations.RunPython.noop),
    ]
