"""Blueprint de Logística y Envíos a domicilio."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from ..models import ESTADOS_ENVIO, Envio
from ..services.inventario import ErrorNegocio, cambiar_estado_envio

bp = Blueprint("logistica", __name__)


@bp.route("/")
@login_required
def index():
    estado = request.args.get("estado", "")
    consulta = Envio.query
    if estado in ESTADOS_ENVIO:
        consulta = consulta.filter_by(estado=estado)
    envios = consulta.order_by(Envio.fecha_creacion.desc()).all()

    todos = Envio.query.all()
    contadores = {
        "todos": len(todos),
        "preparacion": sum(1 for e in todos if e.estado == "En preparación"),
        "camino": sum(1 for e in todos if e.estado == "En camino"),
        "entregado": sum(1 for e in todos if e.estado == "Entregado"),
    }
    return render_template(
        "logistica/envio.html",
        envios=envios,
        estados=ESTADOS_ENVIO,
        estado_actual=estado,
        contadores=contadores,
    )


@bp.route("/<int:envio_id>/estado", methods=["POST"])
@login_required
def actualizar_estado(envio_id: int):
    try:
        envio = cambiar_estado_envio(
            envio_id=envio_id,
            estado=request.form.get("estado"),
            repartidor=request.form.get("repartidor"),
        )
        flash(f"Envío #{envio.id} actualizado a «{envio.estado}».", "success")
    except ErrorNegocio as err:
        flash(str(err), "error")
    return redirect(request.referrer or url_for("logistica.index"))
