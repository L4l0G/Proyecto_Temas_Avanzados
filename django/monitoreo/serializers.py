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