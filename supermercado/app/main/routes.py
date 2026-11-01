"""Blueprint principal: Dashboard y buscador global (Cmd+K)."""
from flask import Blueprint, render_template, request
from flask_login import login_required

from ..models import Cliente, Producto, Proveedor, Venta
from ..services.inventario import resumen_dashboard

bp = Blueprint("main", __name__)


@bp.route("/")
@login_required
def index():
    datos = resumen_dashboard()
    ultimas_ventas = Venta.query.order_by(Venta.fecha.desc()).limit(5).all()
    return render_template("main/index.html", **datos, ultimas_ventas=ultimas_ventas)


@bp.route("/buscador")
@login_required
def buscador():
    q = (request.args.get("q") or "").strip()
    productos, clientes, proveedores, ventas = [], [], [], []
    if q:
        patron = f"%{q}%"
        productos = (
            Producto.query.filter(
                (Producto.nombre.ilike(patron)) | (Producto.codigo_barras.ilike(patron))
            )
            .order_by(Producto.nombre)
            .all()
        )
        clientes = (
            Cliente.query.filter(
                (Cliente.nombre.ilike(patron))
                | (Cliente.apellido.ilike(patron))
                | (Cliente.dni.ilike(patron))
            )
            .order_by(Cliente.nombre)
            .all()
        )
        proveedores = (
            Proveedor.query.filter(
                (Proveedor.nombre.ilike(patron)) | (Proveedor.cuit.ilike(patron))
            )
            .order_by(Proveedor.nombre)
            .all()
        )
        if q.isdigit():
            v = Venta.query.filter_by(id=int(q)).first()
            if v:
                ventas = [v]
    return render_template(
        "main/buscador.html",
        q=q,
        productos=productos,
        clientes=clientes,
        proveedores=proveedores,
        ventas=ventas,
    )
