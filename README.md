# Sistema de Monitoreo de Equipos

Aplicación web en Django para monitorear equipos, con un agente de Windows (`agente/`) que se instala en cada equipo monitoreado.

```
django/   Aplicación web (proyecto sistemamonitoreo, app monitoreo)
agente/   Agente de Windows y su desinstalador (se empaquetan con PyInstaller)
assets/   Íconos SVG
```

## Configuración inicial

Se hace una sola vez, al clonar el repositorio. Requiere Python 3.14 y Git.

### 1. Clonar y crear el entorno virtual

```powershell
git clone https://github.com/L4l0G/Proyecto_Temas_Avanzados.git
cd Proyecto_Temas_Avanzados
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r django\requirements.txt pyinstaller
```

En VS Code, selecciona el intérprete del entorno: **Ctrl+Shift+P → Python: Select Interpreter → `.venv`**.

### 2. Crear tu archivo `.env`

El `.env` tiene contraseñas, así que **no se sube al repositorio**: cada quien tiene el suyo.

```powershell
copy django\.env.example django\.env
```

Abre `django\.env` y llénalo:

- **`DJANGO_SECRET_KEY`**: genera una propia, no copies la de otra persona.
  ```powershell
  .venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(50))"
  ```
- **Base de datos**: cada integrante usa **su propia base**, para que las pruebas de uno no afecten a los demás. Elige una opción:
  - **Supabase (recomendado, no instala nada)**: crea un proyecto gratis en [supabase.com](https://supabase.com). En **Connect**, copia la cadena del **Session pooler** (puerto 5432) y ponla en `DATABASE_URL`. Agrega `?sslmode=require` al final y reemplaza `[YOUR-PASSWORD]` por tu contraseña, sin corchetes.
  - **PostgreSQL local**: crea un usuario y una base `monitoreo` y llena las variables `DB_*`. Deja `DATABASE_URL` comentada.

> Si la contraseña tiene caracteres especiales, escríbelos codificados en `DATABASE_URL`: `@` → `%40`, `#` → `%23`, `/` → `%2F`, `:` → `%3A`.

### 3. Crear las tablas y tu usuario administrador

```powershell
cd django
..\.venv\Scripts\python.exe manage.py migrate
..\.venv\Scripts\python.exe manage.py createsuperuser
```

## Uso diario

Desde la carpeta `django`:

```powershell
..\.venv\Scripts\python.exe manage.py runserver          # http://127.0.0.1:8000
..\.venv\Scripts\python.exe manage.py test monitoreo     # pruebas
```

Después de hacer `git pull`, si alguien agregó migraciones o dependencias:

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
..\.venv\Scripts\python.exe manage.py migrate
```

## Compilar el agente

Desde la carpeta `agente`:

```powershell
..\.venv\Scripts\pyinstaller.exe AgenteMonitoreo.spec
..\.venv\Scripts\pyinstaller.exe DesinstalarAgente.spec
```

Los ejecutables quedan en `agente\dist\`, que no se sube al repositorio.

## Reglas del equipo

- **Nunca subas** `.env`, contraseñas ni cadenas de conexión. Si necesitas compartir una, hazlo por un medio privado.
- Si cambias un modelo, crea la migración (`manage.py makemigrations`) y súbela junto con el cambio.
- Si agregas una dependencia, añádela a `django/requirements.txt`.
- Si agregas una variable de entorno nueva, añádela también a `django/.env.example` (sin el valor real).

## Despliegue

`django/Procfile` corre las migraciones y `collectstatic` en cada despliegue, y luego levanta `gunicorn`. En el servidor hay que definir como variables de entorno:

- `DJANGO_SECRET_KEY`: una clave exclusiva de producción. Sin ella, el sistema no arranca.
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS`: el dominio del sitio.
- `DATABASE_URL`: la base de producción. No uses la de desarrollo de nadie.
