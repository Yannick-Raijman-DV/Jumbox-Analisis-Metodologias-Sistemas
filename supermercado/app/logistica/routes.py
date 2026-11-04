"""Blueprint de Logística y Envíos a domicilio (con mapa, distancia y costo vía Geoapify)."""
from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import login_required

from ..extensions import db
from ..models import ESTADOS_ENVIO, Envio
from ..services import geo
from ..services.inventario import ErrorNegocio, cambiar_estado_envio

bp = Blueprint("logistica", __name__)

LARGO_MAXIMO_DIRECCION = 200  # igual que la columna envios.direccion

# Imagen de reemplazo cuando Geoapify no puede dar el mapa: la página no muestra un ícono roto
MAPA_NO_DISPONIBLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300" viewBox="0 0 600 300">'
    '<rect width="600" height="300" fill="#eff4ff"/>'
    '<text x="300" y="158" text-anchor="middle" font-family="sans-serif" font-size="22" fill="#42474f">'
    "Mapa no disponible</text></svg>"
)


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
        geo_configurada=geo.configurada(),  # si es False la página muestra cómo configurar la key
        sucursal_nombre=current_app.config["SUCURSAL_NOMBRE"],
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


# ---------------------------------------------------------------------------
# Geoapify: cotizar una dirección, ver la ruta de un envío y el mapa (proxy)
# ---------------------------------------------------------------------------

def _con_mapa(resultado: dict) -> dict:
    """Agrega la URL del mapa (nuestra ruta proxy, sin la key) si la cotización salió bien."""
    if resultado["ok"]:
        # 6 decimales = precisión de ~10 cm: alcanza y deja la URL prolija
        resultado["mapa_url"] = url_for("logistica.mapa", lat=round(resultado["lat"], 6), lon=round(resultado["lon"], 6))
    else:
        resultado["mapa_url"] = None
    return resultado


@bp.route("/api/cotizar", methods=["POST"])
@login_required
def api_cotizar():
    """Cuadro "Probar dirección": recibe {"direccion": "..."} y devuelve km, minutos, costo y mapa."""
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        datos = {}
    direccion = str(datos.get("direccion") or "").strip()

    if not direccion:
        return jsonify({"ok": False, "error": "Escribí una dirección para calcular el envío."}), 400
    if len(direccion) > LARGO_MAXIMO_DIRECCION:
        return jsonify({"ok": False, "error": f"La dirección no puede superar los {LARGO_MAXIMO_DIRECCION} caracteres."}), 400

    return jsonify(_con_mapa(geo.cotizar(direccion)))


@bp.route("/<int:envio_id>/ruta")
@login_required
def ruta_envio(envio_id: int):
    """Botón "Ver ruta" de cada tarjeta: cotiza la dirección guardada en el envío."""
    envio = db.session.get(Envio, envio_id)
    if envio is None:
        return jsonify({"ok": False, "error": "El envío no existe."}), 404
    return jsonify(_con_mapa(geo.cotizar(envio.direccion)))


@bp.route("/mapa.png")
@login_required
def mapa():
    """Proxy del mapa estático: el navegador pide la imagen a Flask y Flask a Geoapify con la key.

    Así la key nunca aparece en el HTML (<img src> apunta acá, no a Geoapify).
    """
    lat = request.args.get("lat", type=float)  # None si falta o no es un número
    lon = request.args.get("lon", type=float)
    # Solo coordenadas dentro de Argentina (aprox.): así nadie usa esta ruta como proxy
    # gratis a Geoapify para mapas de otros lugares (y gastando nuestros créditos).
    if lat is None or lon is None or not (-56 <= lat <= -21 and -74 <= lon <= -53):
        abort(400)

    try:
        png = geo.mapa_png(lat, lon)
    except geo.ErrorGeo:
        return Response(MAPA_NO_DISPONIBLE_SVG, status=502, mimetype="image/svg+xml")

    respuesta = Response(png, mimetype="image/png")
    respuesta.headers["Cache-Control"] = "private, max-age=3600"  # el navegador lo guarda 1 hora: menos créditos
    return respuesta
