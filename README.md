# Supermercado - Avance TP (Flask + SQLite)

Módulo ABM/CRUD de **productos** con SQLite como base persistente. Es la base sobre la que luego se entrenará el modelo de aprendizaje automático.

## Instalar y correr

```powershell
pip install -r requirements.txt
python app.py
```

Abrir `http://127.0.0.1:5000`. La primera vez se crea `instance/supermercado.db` con las tablas `productos` y `usuarios`, y 5 productos de ejemplo. Para empezar de cero, borrar ese archivo.

## Estructura

- `models.py` — modelos `Producto` y `Usuario`, y funciones de acceso a datos.
- `app.py` — configuración (SECRET_KEY, Bcrypt, LoginManager), validaciones, vistas web (incluye login) y API REST.
- `templates/` — `index.html` (formulario + listado), `login.html`, `registro.html`, `protegido.html`.
- `static/style.css` — estilos.

## Vistas web de productos (formularios HTML)

| Método | URL | Descripción |
|---|---|---|
| GET | / | Formulario de alta + listado |
| POST | /productos/nuevo | Crea un producto |
| GET / POST | /productos/1/editar | Muestra el formulario con los datos / guarda los cambios |
| POST | /productos/1/eliminar | Elimina el producto |

## Autenticación (flujo)

1. `/registro` — guarda el usuario con la contraseña encriptada (`bcrypt.generate_password_hash`).
2. `/login` — valida con `bcrypt.check_password_hash`; si es correcto, `login_user(usuario)` guarda su id en `session`.
3. `/protegido` — tiene `@login_required`: sin sesión redirige a `/login`.
4. `/logout` — `logout_user()` borra la sesión.

## Tabla `productos`

| Campo | Tipo | Notas |
|---|---|---|
| id | INTEGER | PK autoincremental |
| codigo_barras | VARCHAR(30) | Opcional, único |
| nombre | VARCHAR(120) | Obligatorio |
| categoria | VARCHAR(60) | Obligatorio |
| precio | FLOAT | Obligatorio, >= 0 |
| stock | INTEGER | >= 0, por defecto 0 |
| fecha_alta | DATETIME | Se completa sola |

## Endpoints (para probar en Postman)

| Método | URL | Body (JSON) | Descripción |
|---|---|---|---|
| GET | /api/productos | - | Lista todos los productos |
| GET | /api/productos/1 | - | Obtiene el producto 1 |
| POST | /api/productos | `{"nombre": "Yerba 1kg", "categoria": "Almacén", "precio": 3500, "stock": 40, "codigo_barras": "7790001000059"}` | Crea un producto (nombre, categoria y precio obligatorios) |
| PUT | /api/productos/1 | `{"precio": 1600}` | Actualiza uno o varios campos |
| DELETE | /api/productos/1 | - | Elimina el producto 1 |

Errores: 400 (datos inválidos) y 404 (no existe), siempre con `{"error": "..."}`.
