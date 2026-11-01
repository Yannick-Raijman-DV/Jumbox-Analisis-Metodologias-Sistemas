"""Blueprint de Proveedores, Pedidos de reposición y Pagos de deuda (con comprobante S3)."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..decorators import rol_requerido
from ..extensions import db
from ..models import ROL_ADMIN, ROL_OPERARIO, PedidoProveedor, Proveedor
from ..services import s3
from ..services.inventario import (
    ErrorNegocio,
    registrar_pago_proveedor,
    registrar_pedido_proveedor,
    ultimo_precio_compra,
)

bp = Blueprint("proveedores", __name__)


@bp.route("/", methods=["GET", "POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def index():
    if request.method == "POST":
        nombre = (request.form.get("nombre") or "").strip()
        cuit = (request.form.get("cuit") or "").strip()
        if not nombre or not cuit:
            flash("Nombre y CUIT del proveedor son obligatorios.", "error")
        elif Proveedor.query.filter_by(cuit=cuit).first():
            flash("Ya existe un proveedor con ese CUIT.", "error")
        else:
            db.session.add(Proveedor(nombre=nombre[:45], cuit=cuit[:45], deuda=0.0))
            db.session.commit()
            flash(f"Proveedor «{nombre}» agregado.", "success")
            return redirect(url_for("proveedores.index"))

    proveedores = Proveedor.query.order_by(Proveedor.deuda.desc(), Proveedor.nombre).all()
    ultimos_pedidos = PedidoProveedor.query.order_by(PedidoProveedor.fecha.desc()).limit(10).all()
    deuda_total = sum(p.deuda or 0 for p in proveedores)
    return render_template(
        "proveedores/proveedores.html",
        proveedores=proveedores,
        ultimos_pedidos=ultimos_pedidos,
        deuda_total=deuda_total,
    )


@bp.route("/<int:proveedor_id>")
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def detalle(proveedor_id: int):
    proveedor = db.session.get(Proveedor, proveedor_id)
    if proveedor is None:
        abort(404)
    precios_sugeridos = {p.id: ultimo_precio_compra(p) for p in proveedor.productos}
    return render_template(
        "proveedores/detalle.html",
        proveedor=proveedor,
        precios_sugeridos=precios_sugeridos,
    )


@bp.route("/<int:proveedor_id>/pedido", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def crear_pedido(proveedor_id: int):
    proveedor = db.session.get(Proveedor, proveedor_id)
    if proveedor is None:
        abort(404)

    items = []
    for prod in proveedor.productos:
        cant = request.form.get(f"cantidad_{prod.id}")
        precio = request.form.get(f"precio_{prod.id}")
        if cant and str(cant).strip() not in ("", "0"):
            items.append({"producto_id": prod.id, "cantidad": cant, "precio_unitario": precio})

    try:
        pedido = registrar_pedido_proveedor(usuario=current_user, proveedor_id=proveedor.id, items=items)
        flash(
            f"Pedido #{pedido.id} registrado por ${pedido.total:,.2f}. Se sumó el stock al Depósito Central.",
            "success",
        )
    except ErrorNegocio as err:
        flash(str(err), "error")
    return redirect(url_for("proveedores.detalle", proveedor_id=proveedor.id))


@bp.route("/<int:proveedor_id>/pago", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def registrar_pago(proveedor_id: int):
    proveedor = db.session.get(Proveedor, proveedor_id)
    if proveedor is None:
        abort(404)

    comprobante = request.files.get("comprobante")
    comprobante_key = None
    if comprobante and comprobante.filename:
        if not s3.extension_valida(comprobante.filename, permitidas=s3.EXTENSIONES_COMPROBANTE):
            flash("El comprobante debe ser una imagen o PDF.", "error")
            return redirect(url_for("proveedores.detalle", proveedor_id=proveedor.id))
        comprobante_key = s3.subir_archivo(comprobante, comprobante.filename, carpeta="comprobantes")

    try:
        pago = registrar_pago_proveedor(
            usuario=current_user,
            proveedor_id=proveedor.id,
            monto=request.form.get("monto"),
            comprobante_key=comprobante_key,
            observaciones=request.form.get("observaciones"),
        )
        flash(f"Pago de ${pago.monto:,.2f} registrado a {proveedor.nombre}.", "success")
    except ErrorNegocio as err:
        if comprobante_key:
            s3.borrar(comprobante_key)
        flash(str(err), "error")
    return redirect(url_for("proveedores.detalle", proveedor_id=proveedor.id))
