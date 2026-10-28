import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def static_v(path):
    """
    Igual que {% static %}, pero le agrega ?v=<fecha de modificación del
    archivo> a la URL. Así, cada vez que se edita un CSS/JS, la URL cambia
    y el navegador lo descarga de nuevo en vez de usar la copia en caché
    (el problema que nos hizo perder tiempo varias veces en el tablero).
    """
    url = static(path)
    archivo_real = finders.find(path)
    if archivo_real:
        try:
            version = int(os.path.getmtime(archivo_real))
            return f'{url}?v={version}'
        except OSError:
            pass
    return url
