import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Crea el primer superusuario a partir de variables de entorno "
        "(DJANGO_SUPERUSER_USERNAME/PASSWORD/EMAIL). Pensado para plataformas "
        "sin acceso a shell (ej. Render en el plan gratis), donde no se puede "
        "correr 'createsuperuser' a mano. No hace nada si ya existe un "
        "usuario con ese username, asi que es seguro dejarlo en el build de "
        "cada deploy."
    )

    def handle(self, *args, **options):
        username = os.environ.get('DJANGO_SUPERUSER_USERNAME')
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
        email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')

        if not username or not password:
            self.stdout.write(
                'DJANGO_SUPERUSER_USERNAME/DJANGO_SUPERUSER_PASSWORD no estan '
                'definidas - se omite la creacion del superusuario.'
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(f'El usuario "{username}" ya existe, no se crea de nuevo.')
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f'Superusuario "{username}" creado.'))
