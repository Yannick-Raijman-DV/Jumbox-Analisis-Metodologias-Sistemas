"""Blueprint Punto de Venta (POS): terminal split-screen 60/40, cobro y tickets."""
from flask import Blueprint, abort, jsonify, render_template, request, url_for
from flask_login import current_user

from ..decorators import rol_requerido
from ..extensions import db
from ..models import ROL_ADMIN, ROL_CAJERO, Categoria, Cliente, Producto, Venta
from ..services.inventario import METODOS_PAGO, ErrorNegocio, registrar_venta

bp = Blueprint("pos", __name__)


@bp.route("/")
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def venta():
    productos = Producto.query.order_by(Producto.nombre).all()
    categorias = Categoria.query.order_by(Categoria.nombre).all()
    clientes = Cliente.query.order_by(Cliente.nombre).all()
    ultimas_ventas = Venta.query.order_by(Venta.fecha.desc()).limit(8).all()
    return render_template(
        "pos/venta.html",
        productos=productos,
        categorias=categorias,
        clientes=clientes,
        metodos_pago=METODOS_PAGO,
        ultimas_ventas=ultimas_ventas,
    )


@bp.route("/api/venta", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def api_registrar_venta():
    datos = request.get_json(silent=True) or {}
    try:
        nueva = registrar_venta(
            usuario=current_user,
            items=datos.get("items") or [],
            cliente_id=datos.get("cliente_id"),
            metodo_pago=datos.get("metodo_pago") or "Efectivo",
            envio=datos.get("envio"),
        )
    except ErrorNegocio as err:
        return jsonify({"ok": False, "error": str(err)}), 400

    return jsonify(
        {
            "ok": True,
            "venta_id": nueva.id,
            "total": nueva.total,
            "ticket_url": url_for("pos.ticket", venta_id=nueva.id),
        }
    ), 201


@bp.route("/ticket/<int:venta_id>")
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def ticket(venta_id: int):
    venta_obj = db.session.get(Venta, venta_id)
    if venta_obj is None:
        abort(404)
    return render_template("pos/ticket.html", venta=venta_obj)
