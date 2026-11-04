# JUMBOX — Sistema de Gestión Integral para Supermercados (Segundo Parcial)

> **Materia:** Análisis y Metodología de Sistemas (Escuela DaVinci — 4° Cuatrimestre)  
> **Cátedra:** Prof. Gamaliel Quiroz  
> **Arquitectura:** Flask Modular (`create_app` + 8 Blueprints) + SQLite/SQLAlchemy + **AWS S3** + **LLM Local (Qwen2.5-0.5B-Instruct)** + **Google OAuth 2.0**  
> **APIs externas (REST con `requests`):** **Geoapify** (mapa, distancia y costo en Envíos) + **Mercado Pago Checkout Pro** vía Orders API (cobro del ticket en Caja, con credenciales de prueba)

---

## 1. Requisitos e Instalación Local (Entorno Virtual)

Ejecutar desde una terminal posicionada en `Jumbox-Analisis-Metodologias-Sistemas/supermercado`:

```powershell
# 1. Crear y activar el entorno virtual
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # En Linux/macOS: source .venv/bin/activate

# 2. Instalar dependencias (incluye PyTorch CPU, Transformers, Boto3, Authlib y Flask)
pip install -r requirements.txt -r requirements-dev.txt

# 3. Configurar variables de entorno
Copy-Item .env.example .env    # Editar .env con credenciales de Google, AWS S3, LLM, Geoapify y Mercado Pago

# 4. Iniciar la aplicación (único punto de entrada)
python run.py
```

Abrí `http://127.0.0.1:5000`. Al iniciar por primera vez se crea automáticamente `instance/proyecto.db` y se siembran las tablas migradas de `migracion/supermercado.sql` (vía `migracion/seed.json`).

---

## 2. Usuarios Migrados de Prueba

Todas las cuentas migradas del sistema Java/MySQL utilizan la contraseña **`1234`** (los hashes `$2a$10$...` de jBCrypt de `operario` y `cajero` se verifican directamente con `Flask-Bcrypt`, y la contraseña de `administrador` se hashea automáticamente al sembrar):

| Usuario | Correo | Contraseña | Rol | Permisos habilitados |
|---|---|---|---|---|
| `administrador` | `admin@gmail.com` | `1234` | **Admin** | Acceso total + gestión de usuarios (`/usuarios`) |
| `operario` | `operario@gmail.com` | `1234` | **Operario** | Stock, mermas, traslados, proveedores, envíos e IA (`/ia/reposicion`) |
| `cajero` | `cajero@gmail.com` | `1234` | **Cajero** | Punto de Venta POS (`/pos`), tickets, clientes y envíos |

Si un usuario nuevo inicia sesión con **Google OAuth**, el sistema lo redirige a `/auth/elegir-rol` para que seleccione su puesto (**Operario** o **Cajero**).

---

## 3. Comandos Útiles y Pruebas Automatizadas

```powershell
# Re-convertir el dump MySQL original a seed.json + extraer fotos BLOB
python migracion/convertir_dump.py

# Reiniciar y volver a sembrar la base SQLite desde cero
flask --app run seed --reset

# Verificar el bucket de AWS S3 de punta a punta (sube, URL prefirmada, privado, borra)
flask --app run s3-check

# Ejecutar la suite de tests automatizados (migración, auth, POS/stock, S3, LLM singleton, Geoapify y Mercado Pago)
pytest -v
```

La suite tiene 77 tests: `tests/test_jumbox.py` (7), `tests/test_geoapify.py` (41) y `tests/test_mercadopago.py` (29). Los de Geoapify y Mercado Pago **nunca salen a internet**: `requests` está siempre mockeado, así que corren sin keys.

---

## 4. Despliegue en Render

1. **Root Directory:** `supermercado`
2. **Build Command:** `pip install -r requirements.txt`
3. **Start Command:** `gunicorn run:app`
4. **Variables de entorno en Render:**
   - `PYTHON_VERSION` = `3.12.6`
   - `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
   - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `S3_BUCKET`
   - `GEOAPIFY_API_KEY` (key gratis de <https://myprojects.geoapify.com>)
   - `MP_ACCESS_TOKEN` = Access Token **de PRUEBA** de Mercado Pago (empieza con `APP_USR-`; nunca el de producción)
   - Opcionales, con valores por defecto: `SUCURSAL_NOMBRE`, `SUCURSAL_LAT`, `SUCURSAL_LON` (punto de partida de los envíos) y `ENVIO_COSTO_BASE`, `ENVIO_COSTO_KM` (costo = base + km × $/km)
   - `LLM_ENABLED` = `false` (en el plan Free de 512 MB de RAM se activa automáticamente el motor determinístico de reglas; en ejecución local usar `LLM_ENABLED=true`).
5. Los valores van **sin comillas**. Después de cambiarlos: **Save, rebuild, and deploy**.

Sin `GEOAPIFY_API_KEY` o sin `MP_ACCESS_TOKEN` la app funciona igual: Envíos y el ticket muestran un aviso de cómo configurarlas. Las keys van solo en `.env` y en Render, nunca en el código ni en git.

---

## 5. Documentación

- [`docs/DOCUMENTACION.md`](docs/DOCUMENTACION.md): documentación técnica completa para el informe PDF.
- [`docs/GUIA_APIS_EXTERNAS.md`](docs/GUIA_APIS_EXTERNAS.md): cómo sacar las keys de Geoapify y Mercado Pago, dónde cargarlas, errores comunes y **guion de prueba de 5 minutos** para el profesor.
- [`docs/GUIA_BUCKET_S3.md`](docs/GUIA_BUCKET_S3.md): cómo crear y conectar el bucket privado de AWS S3 y verificarlo con `flask --app run s3-check`.
