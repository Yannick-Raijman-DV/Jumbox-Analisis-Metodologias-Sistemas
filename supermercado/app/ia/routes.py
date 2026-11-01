"""Blueprint de Inteligencia Artificial: sugerencias de reposición con LLM local pequeña."""
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
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
    """Renderiza la vista de inmediato. El informe de la LLM se solicita por fetch a /ia/api/recomendacion."""
    candidatos = calcular_candidatos_reposicion()

    # Agrupar los ítems con acción "Reponer" por proveedor para generar el pedido en 1 clic
    pedidos_por_proveedor: dict[int, dict] = {}
    for c in candidatos:
        if c["accion"] == "Reponer":
            grupo = pedidos_por_proveedor.setdefault(
                c["proveedor_id"],
                {"proveedor_id": c["proveedor_id"], "proveedor": c["proveedor"], "productos": [], "total": 0.0},
            )
            subtotal = round(c["cantidad_sugerida"] * c["precio_compra"], 2)
            grupo["productos"].append({**c, "subtotal": subtotal})
            grupo["total"] = round(grupo["total"] + subtotal, 2)

    # Si LLM_ENABLED=false el motor de reglas es instantáneo (<1 ms), lo pasamos listo;
    # si LLM_ENABLED=true la página abre al instante con "Generando informe..." y lo pide por AJAX.
    recomendacion_inicial = None if current_app.config["LLM_ENABLED"] else llm.generar_recomendacion(candidatos)

    return render_template(
        "ia/reposicion.html",
        candidatos=candidatos,
        recomendacion=recomendacion_inicial,
        modelo=current_app.config["LLM_MODEL"],
        llm_habilitada=current_app.config["LLM_ENABLED"],
        pedidos_por_proveedor=list(pedidos_por_proveedor.values()),
    )


@bp.route("/api/recomendacion")
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def api_recomendacion():
    """Endpoint JSON asíncrono que ejecuta la LLM local y devuelve el informe redactado."""
    candidatos = calcular_candidatos_reposicion()
    return jsonify(llm.generar_recomendacion(candidatos))


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
