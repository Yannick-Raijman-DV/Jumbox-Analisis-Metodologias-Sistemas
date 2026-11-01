"""Modelos SQLAlchemy de Jumbox.

Se conservan los nombres de tablas y columnas del dump MySQL original
(migracion/supermercado.sql) para que la migración sea trazable. En Python
cada columna tiene un nombre corto, por ejemplo:
    nombre = db.Column("nombre_producto", ...)

Agregados respecto del modelo original (todos aditivos):
    - usuarios: google_id, foto_url (login con Google)
    - productos: foto_producto (BLOB) -> foto_key (key del objeto en S3), codigo_barras
    - clientes: telefono, direccion (para envíos)
    - ventas: total, metodo_pago, tipo_entrega
    - pedidos_proveedor: total, estado
    - bajas_productos: fk_usuario, ubicacion
    - tablas nuevas: envios, pagos_proveedor
"""
from datetime import date, datetime

from flask import current_app
from flask_login import UserMixin

from .extensions import db

# Roles del sistema (tabla roles_usuarios)
ROL_ADMIN = "Admin"
ROL_OPERARIO = "Operario"
ROL_CAJERO = "Cajero"
ROLES_AUTOREGISTRO = (ROL_OPERARIO, ROL_CAJERO)  # los que se pueden elegir al entrar con Google

# Tipos de movimiento de stock (historial_movimientos.tipo_movimiento)
MOV_INGRESO_COMPRA = "INGRESO_COMPRA"
MOV_TRASLADO_GONDOLA = "TRASLADO_GONDOLA"
MOV_EGRESO_VENTA = "EGRESO_VENTA"
MOV_BAJA_MERMA = "BAJA_MERMA"

# Estados de envío
ESTADOS_ENVIO = ("En preparación", "En camino", "Entregado")

# Motivos de merma
MOTIVOS_MERMA = ("Vencimiento", "Rotura", "Robo / faltante", "Mal estado", "Otro")


def ahora() -> datetime:
    """Hora local sin zona (igual que los datetime del dump original)."""
    return datetime.now().replace(microsecond=0)


class RolUsuario(db.Model):
    __tablename__ = "roles_usuarios"

    id = db.Column("id_rol_usuario", db.Integer, primary_key=True)
    nombre = db.Column("rol_usuario", db.String(45), unique=True, nullable=False)

    usuarios = db.relationship("Usuario", back_populates="rol")

    def __repr__(self):
        return f"<Rol {self.nombre}>"


class Usuario(UserMixin, db.Model):
    __tablename__ = "usuarios"

    id = db.Column("id_usuario", db.Integer, primary_key=True)
    nombre = db.Column("nombre_usuario", db.String(45), nullable=False)
    contrasena = db.Column(db.String(255))  # hash bcrypt; None si solo entra con Google
    correo = db.Column("correo_usuario", db.String(120), unique=True, nullable=False)
    rol_id = db.Column("fk_roles_usuarios", db.Integer, db.ForeignKey("roles_usuarios.id_rol_usuario"), nullable=False)
    google_id = db.Column(db.String(64), unique=True)
    foto_url = db.Column(db.String(500))

    rol = db.relationship("RolUsuario", back_populates="usuarios")

    @property
    def rol_nombre(self) -> str:
        return self.rol.nombre if self.rol else ""

    def tiene_rol(self, *roles: str) -> bool:
        return self.rol_nombre in roles

    @property
    def iniciales(self) -> str:
        partes = (self.nombre or "?").split()
        return "".join(p[0] for p in partes[:2]).upper()

    def __repr__(self):
        return f"<Usuario {self.nombre} ({self.rol_nombre})>"


class Categoria(db.Model):
    __tablename__ = "categorias_productos"

    id = db.Column("id_categoria_producto", db.Integer, primary_key=True)
    nombre = db.Column("nombre_categoria", db.String(45), unique=True, nullable=False)

    productos = db.relationship("Producto", back_populates="categoria")


class Proveedor(db.Model):
    __tablename__ = "proveedores"

    id = db.Column("id_proveedor", db.Integer, primary_key=True)
    nombre = db.Column("nombre_proveedor", db.String(45), nullable=False)
    cuit = db.Column("cuit_proveedor", db.String(45), unique=True, nullable=False)
    deuda = db.Column("deuda_proveedor", db.Numeric(10, 2, asdecimal=False), default=0.0, nullable=False)

    productos = db.relationship("Producto", back_populates="proveedor")
    pedidos = db.relationship("PedidoProveedor", back_populates="proveedor", order_by="desc(PedidoProveedor.fecha)")
    pagos = db.relationship("PagoProveedor", back_populates="proveedor", order_by="desc(PagoProveedor.fecha)")

    @property
    def deuda_critica(self) -> bool:
        return (self.deuda or 0) >= current_app.config["DEUDA_CRITICA"]


class Deposito(db.Model):
    __tablename__ = "depositos"

    id = db.Column("id_deposito", db.Integer, primary_key=True)
    nombre = db.Column("nombre_deposito", db.String(45), unique=True, nullable=False)

    stocks = db.relationship("ProductoDeposito", back_populates="deposito")


class Producto(db.Model):
    __tablename__ = "productos"

    id = db.Column("id_producto", db.Integer, primary_key=True)
    nombre = db.Column("nombre_producto", db.String(45), nullable=False)
    precio = db.Column("precio_producto", db.Float, nullable=False)
    stock_gondola = db.Column("stock_actual_producto", db.Integer, nullable=False, default=0)
    stock_minimo = db.Column("stock_minimo_producto", db.Integer, nullable=False, default=0)
    fecha_vencimiento = db.Column("fecha_vencimiento_producto", db.Date, nullable=False)
    foto_key = db.Column(db.String(255))  # antes: foto_producto LONGBLOB
    codigo_barras = db.Column(db.String(30), unique=True)
    categoria_id = db.Column("fk_categoria_producto", db.Integer, db.ForeignKey("categorias_productos.id_categoria_producto"), nullable=False)
    proveedor_id = db.Column("fk_proveedor", db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)

    categoria = db.relationship("Categoria", back_populates="productos")
    proveedor = db.relationship("Proveedor", back_populates="productos")
    stocks_deposito = db.relationship("ProductoDeposito", back_populates="producto", cascade="all, delete-orphan")

    @property
    def stock_deposito(self) -> int:
        return sum(s.stock for s in self.stocks_deposito)

    @property
    def stock_total(self) -> int:
        return (self.stock_gondola or 0) + self.stock_deposito

    @property
    def dias_para_vencer(self) -> int:
        return (self.fecha_vencimiento - date.today()).days

    @property
    def estado_vencimiento(self) -> str:
        """Semáforo de caducidad: vencido (rojo), por_vencer (naranja) u ok (verde)."""
        dias = self.dias_para_vencer
        if dias < 0:
            return "vencido"
        if dias <= current_app.config["DIAS_ALERTA_VENCIMIENTO"]:
            return "por_vencer"
        return "ok"

    @property
    def bajo_minimo(self) -> bool:
        return self.stock_gondola <= self.stock_minimo

    @property
    def sku(self) -> str:
        return self.codigo_barras or f"JBX-{self.id:05d}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "stock_gondola": self.stock_gondola,
            "stock_deposito": self.stock_deposito,
            "stock_minimo": self.stock_minimo,
            "fecha_vencimiento": self.fecha_vencimiento.isoformat(),
            "estado_vencimiento": self.estado_vencimiento,
            "codigo_barras": self.codigo_barras,
            "sku": self.sku,
            "categoria": self.categoria.nombre if self.categoria else None,
            "proveedor": self.proveedor.nombre if self.proveedor else None,
        }


class ProductoDeposito(db.Model):
    __tablename__ = "productos_depositos"

    id = db.Column("id_producto_deposito", db.Integer, primary_key=True)
    stock = db.Column("stock_producto_deposito", db.Integer, nullable=False, default=0)
    producto_id = db.Column("fk_producto", db.Integer, db.ForeignKey("productos.id_producto"), nullable=False)
    deposito_id = db.Column("fk_deposito", db.Integer, db.ForeignKey("depositos.id_deposito"), nullable=False)

    producto = db.relationship("Producto", back_populates="stocks_deposito")
    deposito = db.relationship("Deposito", back_populates="stocks")


class Cliente(db.Model):
    __tablename__ = "clientes"

    id = db.Column("id_cliente", db.Integer, primary_key=True)
    nombre = db.Column("nombre_cliente", db.String(45), nullable=False)
    apellido = db.Column(db.String(45), nullable=False)
    dni = db.Column("dni_cliente", db.String(45), unique=True, nullable=False)
    puntos = db.Column("puntos_cliente", db.Integer, nullable=False, default=0)
    telefono = db.Column(db.String(30))
    direccion = db.Column(db.String(200))

    ventas = db.relationship("Venta", back_populates="cliente", order_by="desc(Venta.fecha)")

    @property
    def nombre_completo(self) -> str:
        return f"{self.nombre} {self.apellido}".strip()

    @property
    def total_comprado(self) -> float:
        return sum(v.total or 0 for v in self.ventas)


class Venta(db.Model):
    __tablename__ = "ventas"

    id = db.Column("id_venta", db.Integer, primary_key=True)
    fecha = db.Column("fecha_venta", db.DateTime, nullable=False, default=ahora)
    usuario_id = db.Column("fk_usuario", db.Integer, db.ForeignKey("usuarios.id_usuario"), nullable=False)
    cliente_id = db.Column("fk_cliente", db.Integer, db.ForeignKey("clientes.id_cliente"))
    total = db.Column(db.Float, nullable=False, default=0.0)
    metodo_pago = db.Column(db.String(20), nullable=False, default="Efectivo")
    tipo_entrega = db.Column(db.String(20), nullable=False, default="mostrador")  # mostrador | envio

    usuario = db.relationship("Usuario")
    cliente = db.relationship("Cliente", back_populates="ventas")
    items = db.relationship("VentaProducto", back_populates="venta", cascade="all, delete-orphan")
    envio = db.relationship("Envio", back_populates="venta", uselist=False, cascade="all, delete-orphan")


class VentaProducto(db.Model):
    __tablename__ = "ventas_productos"

    id = db.Column("id_venta_producto", db.Integer, primary_key=True)
    cantidad = db.Column(db.Integer, nullable=False)
    precio_unitario = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    venta_id = db.Column("fk_venta", db.Integer, db.ForeignKey("ventas.id_venta"), nullable=False)
    producto_id = db.Column("fk_producto", db.Integer, db.ForeignKey("productos.id_producto"), nullable=False)

    venta = db.relationship("Venta", back_populates="items")
    producto = db.relationship("Producto")


class Envio(db.Model):
    """Tabla nueva: despacho a domicilio de una venta del POS."""
    __tablename__ = "envios"

    id = db.Column("id_envio", db.Integer, primary_key=True)
    venta_id = db.Column("fk_venta", db.Integer, db.ForeignKey("ventas.id_venta"), unique=True, nullable=False)
    direccion = db.Column(db.String(200), nullable=False)
    telefono = db.Column(db.String(30))
    estado = db.Column(db.String(20), nullable=False, default=ESTADOS_ENVIO[0])
    repartidor = db.Column(db.String(60))
    observaciones = db.Column(db.String(255))
    fecha_creacion = db.Column(db.DateTime, nullable=False, default=ahora)
    fecha_entrega = db.Column(db.DateTime)

    venta = db.relationship("Venta", back_populates="envio")


class PedidoProveedor(db.Model):
    __tablename__ = "pedidos_proveedor"

    id = db.Column("id_pedido_proveedor", db.Integer, primary_key=True)
    fecha = db.Column("fecha_pedido", db.DateTime, nullable=False, default=ahora)
    usuario_id = db.Column("fk_usuario", db.Integer, db.ForeignKey("usuarios.id_usuario"), nullable=False)
    proveedor_id = db.Column("fk_proveedor", db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    total = db.Column(db.Float, nullable=False, default=0.0)
    estado = db.Column(db.String(20), nullable=False, default="Recibido")

    usuario = db.relationship("Usuario")
    proveedor = db.relationship("Proveedor", back_populates="pedidos")
    items = db.relationship("PedidoProveedorProducto", back_populates="pedido", cascade="all, delete-orphan")


class PedidoProveedorProducto(db.Model):
    __tablename__ = "pedidos_proveedor_productos"

    id = db.Column("id_pedido_proveedor_producto", db.Integer, primary_key=True)
    cantidad = db.Column(db.Integer, nullable=False)
    precio_unitario = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    pedido_id = db.Column("fk_pedido_proveedor", db.Integer, db.ForeignKey("pedidos_proveedor.id_pedido_proveedor"), nullable=False)
    producto_id = db.Column("fk_producto", db.Integer, db.ForeignKey("productos.id_producto"), nullable=False)

    pedido = db.relationship("PedidoProveedor", back_populates="items")
    producto = db.relationship("Producto")


class PagoProveedor(db.Model):
    """Tabla nueva: pagos de deuda a proveedores con comprobante en S3."""
    __tablename__ = "pagos_proveedor"

    id = db.Column("id_pago_proveedor", db.Integer, primary_key=True)
    proveedor_id = db.Column("fk_proveedor", db.Integer, db.ForeignKey("proveedores.id_proveedor"), nullable=False)
    usuario_id = db.Column("fk_usuario", db.Integer, db.ForeignKey("usuarios.id_usuario"), nullable=False)
    monto = db.Column(db.Float, nullable=False)
    fecha = db.Column("fecha_pago", db.DateTime, nullable=False, default=ahora)
    comprobante_key = db.Column(db.String(255))
    observaciones = db.Column(db.String(255))

    proveedor = db.relationship("Proveedor", back_populates="pagos")
    usuario = db.relationship("Usuario")


class BajaProducto(db.Model):
    """Mermas: productos dados de baja (vencidos, rotos, robados...)."""
    __tablename__ = "bajas_productos"

    id = db.Column("id_baja_producto", db.Integer, primary_key=True)
    producto_id = db.Column("fk_producto", db.Integer, db.ForeignKey("productos.id_producto"))
    motivo = db.Column("motivo_baja", db.String(100))
    cantidad = db.Column("cantidad_baja", db.Integer)
    fecha = db.Column("fecha_baja", db.DateTime, default=ahora)
    usuario_id = db.Column("fk_usuario", db.Integer, db.ForeignKey("usuarios.id_usuario"))
    ubicacion = db.Column(db.String(20), default="gondola")  # gondola | deposito

    producto = db.relationship("Producto")
    usuario = db.relationship("Usuario")


class HistorialMovimiento(db.Model):
    __tablename__ = "historial_movimientos"

    id = db.Column("id_movimiento", db.Integer, primary_key=True)
    fecha = db.Column("fecha_movimiento", db.DateTime, default=ahora)
    producto_id = db.Column("fk_producto", db.Integer, db.ForeignKey("productos.id_producto"), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    tipo = db.Column("tipo_movimiento", db.String(30), nullable=False)
    origen = db.Column(db.String(50), nullable=False)
    destino = db.Column(db.String(50), nullable=False)
    usuario_id = db.Column("fk_usuario", db.Integer, db.ForeignKey("usuarios.id_usuario"), nullable=False)

    producto = db.relationship("Producto")
    usuario = db.relationship("Usuario")
