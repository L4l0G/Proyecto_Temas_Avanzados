import json
import platform
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psutil
import requests

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = BASE_DIR / "config_agente.json"
LOG_PATH = BASE_DIR / "agente.log"
SERVIDOR_BASE_POR_DEFECTO = "http://127.0.0.1:8000"
NOMBRE_AGENTE = "Agente de Monitoreo"
NOMBRE_TAREA = NOMBRE_AGENTE
NOMBRE_EXE_AGENTE = "AgenteMonitoreo.exe"
INTERVALO_MINUTOS_POR_DEFECTO = 1

# CREATE_NO_WINDOW evita que se abra una consola negra al llamar a schtasks desde el .exe compilado con --noconsole.
CREATE_NO_WINDOW = 0x08000000


def registrar_log(mensaje):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} - {mensaje}\n")


def cargar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def guardar_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def obtener_equipos(servidor_base):
    respuesta = requests.get(f"{servidor_base}/api/equipos/", timeout=5)
    respuesta.raise_for_status()
    return respuesta.json()


def reclamar_equipo(servidor_base, equipo_id, codigo):
    respuesta = requests.post(
        f"{servidor_base}/api/equipos/{equipo_id}/reclamar/",
        json={"codigo": codigo},
        timeout=5,
    )
    if not respuesta.ok:
        try:
            detalle = respuesta.json().get("detail", respuesta.text)
        except ValueError:
            detalle = respuesta.text
        raise RuntimeError(detalle)
    return respuesta.json()["token"]


def ejecutar_asistente_configuracion():
    import tkinter as tk
    from tkinter import ttk

    resultado = {}
    equipos_disponibles = []

    ventana = tk.Tk()
    ventana.title(f"Configuración del {NOMBRE_AGENTE}")
    ventana.geometry("420x340")
    ventana.resizable(False, False)

    tk.Label(ventana, text="Dirección del servidor:").pack(anchor="w", padx=16, pady=(16, 4))
    entrada_servidor = tk.Entry(ventana, width=45)
    entrada_servidor.insert(0, SERVIDOR_BASE_POR_DEFECTO)
    entrada_servidor.pack(padx=16)

    tk.Label(ventana, text="Selecciona esta computadora:").pack(anchor="w", padx=16, pady=(16, 4))
    combo_equipos = ttk.Combobox(ventana, state="readonly", width=43)
    combo_equipos.pack(padx=16)

    tk.Label(ventana, text="Código de emparejamiento (te lo da el administrador):").pack(anchor="w", padx=16, pady=(16, 4))
    entrada_codigo = tk.Entry(ventana, width=45)
    entrada_codigo.pack(padx=16)

    estado_label = tk.Label(ventana, text="", fg="#555555", wraplength=380, justify="left")
    estado_label.pack(padx=16, pady=(8, 0))

    def conectar():
        nonlocal equipos_disponibles
        servidor_base = entrada_servidor.get().strip().rstrip("/")
        estado_label.config(text="Conectando...")
        ventana.update()
        try:
            equipos_disponibles = obtener_equipos(servidor_base)
        except requests.exceptions.RequestException as error:
            estado_label.config(text=f"No se pudo conectar: {error}")
            return
        if not equipos_disponibles:
            estado_label.config(text="El servidor no tiene equipos registrados todavía.")
            return
        combo_equipos["values"] = [
            f"{equipo['id']} - {equipo['nombre']} ({equipo.get('ubicacion') or 'sin ubicación'})"
            for equipo in equipos_disponibles
        ]
        combo_equipos.current(0)
        estado_label.config(text=f"Se encontraron {len(equipos_disponibles)} equipos.")

    def guardar():
        if not equipos_disponibles or not combo_equipos.get():
            estado_label.config(text="Primero presiona \"Conectar\" y selecciona un equipo.")
            return
        codigo = entrada_codigo.get().strip()
        if not codigo:
            estado_label.config(text="Ingresa el código de emparejamiento que te dio el administrador.")
            return

        equipo = equipos_disponibles[combo_equipos.current()]
        servidor_base = entrada_servidor.get().strip().rstrip("/")

        estado_label.config(text="Verificando código...")
        ventana.update()
        try:
            token = reclamar_equipo(servidor_base, equipo["id"], codigo)
        except requests.exceptions.RequestException as error:
            estado_label.config(text=f"No se pudo conectar: {error}")
            return
        except RuntimeError as error:
            estado_label.config(text=f"No se pudo emparejar: {error}")
            return

        resultado["equipo_id"] = equipo["id"]
        resultado["servidor_base"] = servidor_base
        resultado["token"] = token
        ventana.destroy()

    botones = tk.Frame(ventana)
    botones.pack(pady=16)
    tk.Button(botones, text="Conectar", command=conectar).pack(side="left", padx=8)
    tk.Button(botones, text="Guardar", command=guardar).pack(side="left", padx=8)

    ventana.mainloop()
    return resultado


def asegurar_configuracion():
    if CONFIG_PATH.exists():
        return cargar_config(), False

    config = ejecutar_asistente_configuracion()
    if "equipo_id" not in config:
        registrar_log("Configuración cancelada: no se seleccionó ningún equipo.")
        sys.exit(1)

    guardar_config(config)
    return config, True


def crear_tarea_programada(intervalo_minutos=INTERVALO_MINUTOS_POR_DEFECTO):
    if not getattr(sys, "frozen", False):
        registrar_log("No se creó la tarea programada: solo aplica cuando corre como .exe.")
        return False

    ruta_exe = str(Path(sys.executable).resolve()).replace("'", "''")
    ruta_carpeta = str(BASE_DIR).replace("'", "''")
    nombre_tarea = NOMBRE_TAREA.replace("'", "''")
    # Corre como SYSTEM en lugar de iniciarse únicamente en el usuario que realizó la configuración,
    # permtiendo hacer reportes aunque la PC se quede en la pantalla de bloqueo o inicie sesión otro usuario.
    # Dos triggers que se repiten cada intervalo: uno "al iniciar el sistema" y otro que arranca en un minuto.
    # Con solo el de inicio, la tarea quedaba registrada pero no corría hasta el siguiente reinicio de la PC.
    script_ps = (
        f"$action = New-ScheduledTaskAction -Execute '{ruta_exe}' -WorkingDirectory '{ruta_carpeta}'; "
        f"$repetido = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {intervalo_minutos}) "
        f"-RepetitionDuration (New-TimeSpan -Days 3650); "
        f"$alIniciar = New-ScheduledTaskTrigger -AtStartup; "
        f"$alIniciar.Repetition = $repetido.Repetition; "
        f"$desdeAhora = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1); "
        f"$desdeAhora.Repetition = $repetido.Repetition; "
        f"$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest; "
        f"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew "
        f"-ExecutionTimeLimit (New-TimeSpan -Minutes 5); "
        f"Register-ScheduledTask -TaskName '{nombre_tarea}' -Action $action "
        f"-Trigger $alIniciar, $desdeAhora -Principal $principal -Settings $settings -Force "
        f"-ErrorAction Stop | Out-Null"
    )
    try:
        resultado = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script_ps],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
        )
    except OSError as error:
        registrar_log(f"No se pudo crear la tarea programada: {error}")
        return False
    except subprocess.TimeoutExpired:
        registrar_log("Crear la tarea programada tardó demasiado (PowerShell colgado); se abortó.")
        return False

    if resultado.returncode != 0:
        registrar_log(
            f"PowerShell falló al crear la tarea: {resultado.stderr.strip()} "
            "(¿se ejecutó el programa como administrador? Se requiere para registrar "
            "la tarea como SYSTEM)."
        )
        return False

    registrar_log(
        f"Tarea programada '{NOMBRE_TAREA}' creada (cada {intervalo_minutos} min, como SYSTEM, "
        f"al iniciar el sistema) en {ruta_exe}, sin restricción de batería."
    )
    return True


def _ejecutar_schtasks(*argumentos):
    try:
        return subprocess.run(
            ["schtasks", *argumentos],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        registrar_log(f"schtasks {' '.join(argumentos)} falló: {error}")
        return None


def existe_tarea_programada():
    resultado = _ejecutar_schtasks("/query", "/tn", NOMBRE_TAREA)
    return resultado is not None and resultado.returncode == 0


def eliminar_tarea_programada():
    # Primero se detiene la ejecución en curso (si la hay) y luego se borra la tarea.
    _ejecutar_schtasks("/end", "/tn", NOMBRE_TAREA)
    resultado = _ejecutar_schtasks("/delete", "/tn", NOMBRE_TAREA, "/f")
    if resultado is None or resultado.returncode != 0:
        if resultado is not None:
            registrar_log(f"No se pudo eliminar la tarea programada: {resultado.stderr.strip()}")
        return False
    return True


def detener_agentes_en_ejecucion():
    # PyInstaller --onefile crea dos procesos por ejecución (el cargador y el programa real), ambos con el
    # mismo nombre del .exe; se terminan todos menos este mismo proceso y su cargador.
    propios = {psutil.Process().pid}
    try:
        padre = psutil.Process().parent()
        if padre:
            propios.add(padre.pid)
    except psutil.Error:
        pass

    encontrados = []
    for proceso in psutil.process_iter(["name"]):
        if proceso.pid in propios:
            continue
        if (proceso.info["name"] or "").lower() == NOMBRE_EXE_AGENTE.lower():
            encontrados.append(proceso)

    for proceso in encontrados:
        try:
            proceso.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            registrar_log(f"Sin permiso para detener {NOMBRE_EXE_AGENTE} (PID {proceso.pid}).")
    _, vivos = psutil.wait_procs(encontrados, timeout=5)
    return len(encontrados) - len(vivos), len(vivos)


def detener_procesos(nombres_proceso):
    nombres_buscados = {nombre.lower() for nombre in nombres_proceso}
    detenidos = []
    for proceso in psutil.process_iter(["name"]):
        try:
            nombre = proceso.info["name"]
            if nombre and nombre.lower() in nombres_buscados:
                proceso.terminate()
                detenidos.append(nombre)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return detenidos


def obtener_ip_local():
    # No abre una conexión real: solo le pide al sistema operativo qué interfaz usaría para llegar a esa IP.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def _consultar_wmi(propiedad_cim):
    # Timeout corre en cada reporte, y en caso de que PowerShell/WMI se cuelga bajo SYSTEM sin este límite, el
    # agente se queda atorado para siempre y la tarea deja de reportar.
    try:
        resultado = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", propiedad_cim],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        registrar_log(f"Consulta WMI no respondió a tiempo ({propiedad_cim}): {error}")
        return ""
    if resultado.returncode != 0:
        return ""
    return resultado.stdout.strip()


def obtener_numero_serie():
    return _consultar_wmi("(Get-CimInstance -ClassName Win32_BIOS).SerialNumber")


def obtener_nombre_procesador():
    # El nombre comercial (ej. "AMD Ryzen 7 5700X") sale de Win32_Processor;
    # platform.processor() solo da la cadena técnica de familia/modelo/stepping.
    nombre = _consultar_wmi("(Get-CimInstance -ClassName Win32_Processor).Name")
    return nombre or platform.processor()


def detectar_especificaciones():
    try:
        ram_total_mb = psutil.virtual_memory().total // (1024 * 1024)
    except OSError:
        ram_total_mb = None
    try:
        disco_total_gb = psutil.disk_usage("C:\\").total // (1024 ** 3)
    except OSError:
        disco_total_gb = None
    return {
        "direccion_ip": obtener_ip_local(),
        "numero_serie": obtener_numero_serie(),
        "procesador": obtener_nombre_procesador(),
        "sistema_operativo": platform.platform(),
        "ram_total_mb": ram_total_mb,
        "disco_total_gb": disco_total_gb,
    }


def enviar_reporte(config):
    servidor_base = config.get("servidor_base", SERVIDOR_BASE_POR_DEFECTO)
    datos = {
        "equipo": config["equipo_id"],
        "token": config.get("token", ""),
        "cpu_porcentaje": psutil.cpu_percent(interval=1),
        "memoria_porcentaje": psutil.virtual_memory().percent,
        "disco_porcentaje": psutil.disk_usage("C:\\").percent,
        "conectado": True,
    }
    datos.update(detectar_especificaciones())
    try:
        respuesta = requests.post(f"{servidor_base}/api/reportar/", json=datos, timeout=5)
        if not respuesta.ok:
            registrar_log(f"El servidor respondió {respuesta.status_code}: {respuesta.text}")
            return False

        procesos_a_detener = respuesta.json().get("detener_procesos") or []
        if procesos_a_detener:
            detenidos = detener_procesos(procesos_a_detener)
            if detenidos:
                registrar_log(f"Procesos detenidos por orden remota: {', '.join(detenidos)}")

        return True
    except requests.exceptions.RequestException as error:
        registrar_log(f"No se pudo contactar al servidor: {error}")
        return False


def eliminar_archivos_generados():
    # Va al final de la desinstalación y sin registrar_log: escribir en el log después de borrarlo lo volvería a crear.
    no_eliminados = []
    for ruta in (CONFIG_PATH, LOG_PATH):
        try:
            ruta.unlink(missing_ok=True)
        except OSError:
            no_eliminados.append(ruta.name)
    return no_eliminados


def desinstalar():
    from tkinter import messagebox

    titulo = f"{NOMBRE_AGENTE} - Desinstalar"
    # La tarea va primero: si se matara el proceso antes, la tarea lo volvería a lanzar al minuto siguiente.
    tarea_existia = existe_tarea_programada()
    tarea_eliminada = eliminar_tarea_programada() if tarea_existia else True
    detenidos, sin_detener = detener_agentes_en_ejecucion()

    if not tarea_eliminada or sin_detener:
        messagebox.showwarning(
            titulo,
            "No se pudo desinstalar el agente por completo (falta de permisos).\n\n"
            "Cierra este programa y vuelve a abrirlo con clic derecho > "
            "\"Ejecutar como administrador\".",
        )
        return

    partes = []
    if tarea_existia:
        partes.append(f"Se eliminó la tarea programada \"{NOMBRE_TAREA}\".")
    else:
        partes.append(f"No había ninguna tarea programada \"{NOMBRE_TAREA}\".")
    if detenidos:
        partes.append(f"Se detuvieron {detenidos} procesos del agente que seguían corriendo.")

    no_eliminados = eliminar_archivos_generados()
    if no_eliminados:
        partes.append(
            f"No se pudieron borrar: {', '.join(no_eliminados)}. Bórralos a mano junto con esta carpeta."
        )
        messagebox.showwarning(titulo, "\n".join(partes))
        return

    partes.append("Se borraron la configuración y el registro del agente.")
    partes.append("Ya puedes borrar esta carpeta.")
    messagebox.showinfo(titulo, "\n".join(partes))


def reinstalar_tarea():
    from tkinter import messagebox

    if not CONFIG_PATH.exists():
        messagebox.showwarning(
            NOMBRE_AGENTE,
            "No hay configuración guardada en esta PC; no se puede reinstalar la tarea.",
        )
        return

    eliminar_tarea_programada()
    creada = crear_tarea_programada()
    if creada:
        messagebox.showinfo(
            NOMBRE_AGENTE,
            f"Tarea programada reinstalada: corre cada {INTERVALO_MINUTOS_POR_DEFECTO} "
            "minuto, como SYSTEM, desde el inicio del sistema.",
        )
    else:
        messagebox.showwarning(
            NOMBRE_AGENTE,
            "No se pudo reinstalar la tarea. Cierra este programa y vuelve a abrirlo con "
            "clic derecho > \"Ejecutar como administrador\", e inténtalo de nuevo.",
        )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--desinstalar":
        desinstalar()
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--reinstalar-tarea":
        reinstalar_tarea()
        return

    try:
        config, recien_configurado = asegurar_configuracion()
    except (OSError, json.JSONDecodeError) as error:
        registrar_log(f"No se pudo leer config_agente.json: {error}")
        return

    # Antes la tarea solo se intentaba crear justo después del asistente: si config_agente.json ya existía
    # (carpeta copiada de otra PC, o una primera ejecución sin permisos de administrador), nunca se volvía a
    # intentar. Ahora se crea siempre que falte; cuando la ejecuta la propia tarea, ya existe y no hace nada.
    tarea_faltaba = getattr(sys, "frozen", False) and not existe_tarea_programada()
    tarea_creada = crear_tarea_programada() if tarea_faltaba else False

    exito = enviar_reporte(config)

    if recien_configurado or tarea_faltaba:
        from tkinter import messagebox

        if exito:
            mensaje = "El reporte se envió correctamente."
        else:
            mensaje = "No se pudo enviar el reporte.\nRevisa agente.log y la conexión al servidor."
        if recien_configurado:
            mensaje = "Configuración guardada. " + mensaje
        if tarea_creada:
            mensaje += f"\n\nSe programó para ejecutarse solo cada {INTERVALO_MINUTOS_POR_DEFECTO} minutos."
        else:
            mensaje += (
                "\n\nNo se pudo crear la tarea programada automática. "
                "Cierra este programa y vuelve a abrirlo con clic derecho > "
                "\"Ejecutar como administrador\" (se necesita para programar la tarea "
                "como SYSTEM), o configúrala manualmente en el Programador de tareas de Windows."
            )
        if exito:
            messagebox.showinfo(NOMBRE_AGENTE, mensaje)
        else:
            messagebox.showwarning(NOMBRE_AGENTE, mensaje)


if __name__ == "__main__":
    main()
