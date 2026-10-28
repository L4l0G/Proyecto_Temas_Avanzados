"""
Compila los .po de esta carpeta a .mo sin necesitar GNU gettext (que en Windows no viene instalado,
y por eso "manage.py compilemessages" no funciona). Solo entiende el formato sencillo de nuestros .po:
msgid / msgid_plural / msgstr / msgstr[n], con cadenas de una o varias lineas entre comillas.

Uso (desde la carpeta django/):  python locale/compilar.py
"""
import ast
import struct
from pathlib import Path


def leer_po(ruta):
    entradas, actual, campo = [], {}, None
    for linea in ruta.read_text(encoding="utf-8").splitlines() + [""]:
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            if "msgid" in actual:
                entradas.append(actual)
            actual, campo = {}, None
            continue
        if linea.startswith('"'):
            actual[campo] += ast.literal_eval(linea)
            continue
        campo, valor = linea.split(" ", 1)
        actual[campo] = ast.literal_eval(valor)
    return entradas


def escribir_mo(entradas, ruta):
    catalogo = {}
    for e in entradas:
        if "msgid_plural" in e:
            clave = e["msgid"] + "\0" + e["msgid_plural"]
            valor = "\0".join(e[f"msgstr[{i}]"] for i in range(2))
        else:
            clave, valor = e["msgid"], e["msgstr"]
        catalogo[clave.encode("utf-8")] = valor.encode("utf-8")

    claves = sorted(catalogo)
    inicio_tablas = 7 * 4
    inicio_datos = inicio_tablas + 16 * len(claves)
    datos, tabla_ids, tabla_strs = b"", [], []
    for clave in claves:
        tabla_ids.append((len(clave), inicio_datos + len(datos)))
        datos += clave + b"\0"
    for clave in claves:
        valor = catalogo[clave]
        tabla_strs.append((len(valor), inicio_datos + len(datos)))
        datos += valor + b"\0"

    cabecera = struct.pack(
        "<7I", 0x950412DE, 0, len(claves), inicio_tablas, inicio_tablas + 8 * len(claves), 0, 0
    )
    tablas = b"".join(struct.pack("<2I", *par) for par in tabla_ids + tabla_strs)
    ruta.write_bytes(cabecera + tablas + datos)


if __name__ == "__main__":
    for po in Path(__file__).resolve().parent.rglob("*.po"):
        escribir_mo(leer_po(po), po.with_suffix(".mo"))
        print(f"Compilado: {po.with_suffix('.mo')}")
