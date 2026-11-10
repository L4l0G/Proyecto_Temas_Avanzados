from rest_framework import serializers
from .models import Equipo, HistorialEstado

class HistorialEstadoSerializer(serializers.ModelSerializer):
    class Meta:
        model = HistorialEstado
        fields = ['equipo', 'cpu_porcentaje', 'memoria_porcentaje', 'disco_porcentaje', 'conectado', 'timestamp']


class EquipoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Equipo
        fields = ['id', 'nombre', 'ubicacion']


class EspecificacionesSerializer(serializers.ModelSerializer):
    # Valida los datos de hardware que manda el agente junto con cada reporte (IP, RAM, etc.)
    # antes de guardar nada, para no dejar un reporte guardado a medias si alguno viene mal.
    class Meta:
        model = Equipo
        fields = ['direccion_ip', 'numero_serie', 'procesador', 'sistema_operativo', 'ram_total_mb', 'disco_total_gb']
        extra_kwargs = {campo: {'required': False} for campo in fields}