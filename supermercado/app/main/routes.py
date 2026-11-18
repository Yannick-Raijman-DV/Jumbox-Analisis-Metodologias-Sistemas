"""Blueprint principal: Dashboard y buscador global (Cmd+K)."""
from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from ..models import ROL_ADMIN, ROL_CAJERO, ROL_OPERARIO, Cliente, Producto, Proveedor, Venta
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
    ver_comercial = current_user.tiene_rol(ROL_ADMIN, ROL_CAJERO)
    ver_operativo = current_user.tiene_rol(ROL_ADMIN, ROL_OPERARIO)

    if q:
        patron = f"%{q}%"
        productos = (
            Producto.query.filter(
                (Producto.nombre.ilike(patron)) | (Producto.codigo_barras.ilike(patron))
            )
            .order_by(Producto.nombre)
            .all()
        )
        if ver_comercial:
            clientes = (
                Cliente.query.filter(
                    (Cliente.nombre.ilike(patron))
                    | (Cliente.apellido.ilike(patron))
                    | (Cliente.dni.ilike(patron))
                )
                .order_by(Cliente.nombre)
                .all()
            )
            numero_ticket = q[1:].strip() if q.startswith("#") else q
            if numero_ticket.isdigit():
                v = Venta.query.filter_by(id=int(numero_ticket)).first()
                if v:
                    ventas = [v]
        if ver_operativo:
            proveedores = (
                Proveedor.query.filter(
                    (Proveedor.nombre.ilike(patron)) | (Proveedor.cuit.ilike(patron))
                )
                .order_by(Proveedor.nombre)
                .all()
            )
    return render_template(
        "main/buscador.html",
        q=q,
        productos=productos,
        clientes=clientes,
        proveedores=proveedores,
        ventas=ventas,
    )
