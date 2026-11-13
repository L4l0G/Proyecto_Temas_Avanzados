import secrets
import string

from django.conf import settings
from django.db import models


def _generar_codigo_aleatorio():
    alfabeto = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alfabeto) for _ in range(6))


# Lista blanca de programas que se pueden detener remotamente desde el tablero.
# Sólamente se añaden programas que se usan comúnmente en las computadoras de usuario, para evitar que se cierre algún proceso del sistema operativo.
PROGRAMAS_DETENIBLES = [
    ("brave.exe", "Brave"),
    ("msedge.exe", "Microsoft Edge"),
    ("POWERPNT.EXE", "PowerPoint"),
    ("EXCEL.EXE", "Excel"),
    ("WINWORD.EXE", "Word"),
    ("CyberCafe.Client", "CyberCafe"),
]


class Equipo(models.Model):
    class Estado(models.TextChoices):
        ACTIVO = "activo", "Activo"
        INACTIVO = "inactivo", "Inactivo"
        MANTENIMIENTO = "mantenimiento", "En mantenimiento"

    nombre = models.CharField(max_length=100)
    ubicacion = models.CharField(max_length=150, blank=True)
    direccion_ip = models.GenericIPAddressField(blank=True, null=True)
    numero_serie = models.CharField(max_length=100, blank=True)
    procesador = models.CharField(max_length=200, blank=True)
    sistema_operativo = models.CharField(max_length=200, blank=True)
    ram_total_mb = models.PositiveIntegerField(blank=True, null=True)
    disco_total_gb = models.PositiveIntegerField(blank=True, null=True)
    estado = models.CharField(
        max_length=20, choices=Estado.choices, default=Estado.INACTIVO
    )
    fecha_registro = models.DateTimeField(auto_now_add=True)
    token = models.CharField(max_length=64, blank=True, default="")
    codigo_emparejamiento = models.CharField(max_length=12, blank=True, default="")

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre

    def generar_codigo_emparejamiento(self):
        """Genera un código de un solo uso e invalida el token/emparejamiento anterior."""
        self.codigo_emparejamiento = _generar_codigo_aleatorio()
        self.token = ""
        self.save(update_fields=["codigo_emparejamiento", "token"])
        return self.codigo_emparejamiento


class HistorialEstado(models.Model):
    equipo = models.ForeignKey(
        Equipo, on_delete=models.CASCADE, related_name="historial"
    )
    cpu_porcentaje = models.FloatField()
    memoria_porcentaje = models.FloatField()
    disco_porcentaje = models.FloatField()
    conectado = models.BooleanField(default=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        # El tablero y el detalle siempre buscan el reporte más reciente de un equipo. Sin este índice, la
        # base tenía que ordenar todo el historial del equipo en cada consulta (crece 1,440 filas al día).
        indexes = [models.Index(fields=["equipo", "-timestamp"], name="historial_equipo_ts_idx")]

    def __str__(self):
        return f"{self.equipo.nombre} @ {self.timestamp:%Y-%m-%d %H:%M}"


class ComandoRemoto(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        ENVIADO = "enviado", "Enviado al equipo"

    equipo = models.ForeignKey(
        Equipo, on_delete=models.CASCADE, related_name="comandos"
    )
    proceso = models.CharField(max_length=100, choices=PROGRAMAS_DETENIBLES)
    estado = models.CharField(
        max_length=20, choices=Estado.choices, default=Estado.PENDIENTE
    )
    creado = models.DateTimeField(auto_now_add=True)
    enviado = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creado"]

    def __str__(self):
        return f"Detener {self.proceso} en {self.equipo.nombre}"


class Alerta(models.Model):
    class Nivel(models.TextChoices):
        INFO = "info", "Informativo"
        ADVERTENCIA = "advertencia", "Advertencia"
        CRITICO = "critico", "Crítico"

    class Tipo(models.TextChoices):
        SIN_RESPUESTA = "sin_respuesta", "Sin respuesta"
        CPU_ALTO = "cpu_alto", "CPU alto"
        MEMORIA_ALTA = "memoria_alta", "Memoria alta"
        DISCO_LLENO = "disco_lleno", "Disco lleno"


    equipo = models.ForeignKey(Equipo, on_delete=models.CASCADE, related_name="alertas")
    mensaje = models.TextField()
    nivel = models.CharField(max_length=20, choices=Nivel.choices, default=Nivel.INFO)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    atendida = models.BooleanField(default=False)
    # Cada usuario lleva su propio registro de lectura: abrir la campana marca las alertas como leídas
    # solo para quien la abrió. (Antes era un solo campo para todos.)
    leida_por = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="alertas_leidas")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["equipo", "tipo"],
                condition=models.Q(atendida=False),
                name="unica_alerta_sin_atender_por_equipo_y_tipo",
            ),
        ]

    def __str__(self):
        return f"[{self.nivel}] {self.equipo.nombre} @ {self.timestamp:%Y-%m-%d %H:%M}"

    @classmethod
    def no_leidas_por(cls, usuario):
        # Alertas pendientes que este usuario todavía no ha visto. Las atendidas ya no se notifican.
        return cls.objects.filter(atendida=False).exclude(leida_por=usuario)