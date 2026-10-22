from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


class Producto(db.Model):
    __tablename__ = "productos"

    id = db.Column(db.Integer, primary_key=True)
    codigo_barras = db.Column(db.String(30), unique=True)  # opcional, pero no se puede repetir
    nombre = db.Column(db.String(120), nullable=False)
    categoria = db.Column(db.String(60), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)
    fecha_alta = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "codigo_barras": self.codigo_barras,
            "nombre": self.nombre,
            "categoria": self.categoria,
            "precio": self.precio,
            "stock": self.stock,
            "fecha_alta": self.fecha_alta.strftime("%Y-%m-%d %H:%M"),
        }


# UserMixin agrega lo que Flask-Login necesita: is_authenticated, get_id(), etc.
class Usuario(UserMixin, db.Model):
    __tablename__ = "usuarios"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)  # guarda el hash de bcrypt, nunca el texto plano
    rol = db.Column(db.String(40))


# ---------- Funciones de acceso a datos ----------

def agregar_producto(nombre, categoria, precio, stock=0, codigo_barras=None):
    producto = Producto(
        nombre=nombre,
        categoria=categoria,
        precio=precio,
        stock=stock,
        codigo_barras=codigo_barras,
    )
    db.session.add(producto)
    db.session.commit()
    return producto


def obtener_todos():
    return Producto.query.order_by(Producto.id).all()


def obtener_por_id(producto_id):
    return db.session.get(Producto, producto_id)


def existe_codigo(codigo_barras, ignorar_id=None):
    """True si ya hay OTRO producto con ese código de barras."""
    if not codigo_barras:
        return False
    q = Producto.query.filter_by(codigo_barras=codigo_barras)
    if ignorar_id is not None:
        q = q.filter(Producto.id != ignorar_id)
    return q.first() is not None


def eliminar_por_id(producto_id):
    producto = obtener_por_id(producto_id)
    if producto is not None:
        db.session.delete(producto)
        db.session.commit()
    return producto


def cargar_datos_iniciales():
    # Solo carga productos de ejemplo si la tabla está vacía
    if Producto.query.first() is None:
        agregar_producto("Leche entera 1L", "Lácteos", 1450.0, 60, "7790001000011")
        agregar_producto("Fideos tirabuzón 500g", "Almacén", 1200.0, 120, "7790001000028")
        agregar_producto("Gaseosa cola 2.25L", "Bebidas", 2900.0, 80, "7790001000035")
        agregar_producto("Manzana roja (kg)", "Frutas y verduras", 2400.0, 45)
        agregar_producto("Detergente 750ml", "Limpieza", 2100.0, 35, "7790001000042")
