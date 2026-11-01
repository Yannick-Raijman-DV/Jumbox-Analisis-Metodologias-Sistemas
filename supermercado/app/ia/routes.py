"""Blueprint de Inteligencia Artificial: sugerencias de reposición con LLM local pequeña."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..decorators import rol_requerido
from ..models import ROL_ADMIN, ROL_OPERARIO
from ..services import llm
from ..services.inventario import (
    ErrorNegocio,
    calcular_candidatos_reposicion,
    registrar_pedido_proveedor,
    trasladar_a_gondola,
)

bp = Blueprint("ia", __name__)


@bp.route("/reposicion")
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def reposicion():
    candidatos = calcular_candidatos_reposicion()
    recomendacion = llm.generar_recomendacion(candidatos)

    # Agrupar los ítems con acción "Reponer" por proveedor para generar el pedido en 1 clic
    pedidos_por_proveedor: dict[int, dict] = {}
    for c in candidatos:
        if c["accion"] == "Reponer":
            grupo = pedidos_por_proveedor.setdefault(
                c["proveedor_id"],
                {"proveedor_id": c["proveedor_id"], "proveedor": c["proveedor"], "items": [], "total": 0.0},
            )
            subtotal = round(c["cantidad_sugerida"] * c["precio_compra"], 2)
            grupo["items"].append({**c, "subtotal": subtotal})
            grupo["total"] = round(grupo["total"] + subtotal, 2)

    return render_template(
        "ia/reposicion.html",
        candidatos=candidatos,
        recomendacion=recomendacion,
        pedidos_por_proveedor=list(pedidos_por_proveedor.values()),
    )


@bp.route("/reposicion/generar-pedido", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def generar_pedido_sugerido():
    proveedor_id = request.form.get("proveedor_id", type=int)
    candidatos = [
        c for c in calcular_candidatos_reposicion()
        if c["accion"] == "Reponer" and c["proveedor_id"] == proveedor_id
    ]
    items = [
        {
            "producto_id": c["producto_id"],
            "cantidad": c["cantidad_sugerida"],
            "precio_unitario": c["precio_compra"],
        }
        for c in candidatos
    ]
    try:
        pedido = registrar_pedido_proveedor(usuario=current_user, proveedor_id=proveedor_id, items=items)
        flash(
            f"Pedido automático #{pedido.id} generado para {pedido.proveedor.nombre} (${pedido.total:,.2f}).",
            "success",
        )
    except ErrorNegocio as err:
        flash(str(err), "error")
    return redirect(url_for("ia.reposicion"))


@bp.route("/reposicion/trasladar", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def trasladar_sugerido():
    try:
        prod = trasladar_a_gondola(
            usuario=current_user,
            producto_id=request.form.get("producto_id"),
            cantidad=request.form.get("cantidad"),
        )
        flash(f"Traslado sugerido aplicado para «{prod.nombre}».", "success")
    except ErrorNegocio as err:
        flash(str(err), "error")
    return redirect(url_for("ia.reposicion"))
