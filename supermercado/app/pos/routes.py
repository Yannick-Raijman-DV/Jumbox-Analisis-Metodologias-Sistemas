"""Blueprint Punto de Venta (POS): terminal split-screen 60/40, cobro, tickets y Mercado Pago."""
import re

from flask import Blueprint, abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user

from ..decorators import rol_requerido
from ..extensions import db
from ..models import ROL_ADMIN, ROL_CAJERO, Categoria, Cliente, PagoMP, Producto, Venta, ahora
from ..services import mercadopago as mp
from ..services.inventario import METODOS_PAGO, ErrorNegocio, registrar_venta

bp = Blueprint("pos", __name__)

METODO_MP = "Mercado Pago"  # mismo texto que en METODOS_PAGO


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
    venta_obj = _venta_o_404(venta_id)
    pago_mp = _pago_mp_de(venta_obj.id)
    respuesta = make_response(render_template(
        "pos/ticket.html",
        venta=venta_obj,
        pago_mp=pago_mp,
        mp_configurado=mp.configurado(),
        mp_token_prueba=mp.es_token_de_prueba(),
        mp_estado=mp.estado_legible(pago_mp.estado, pago_mp.detalle) if pago_mp else mp.estado_legible(None),
        mp_detalle=mp.detalle_legible(pago_mp.detalle) if pago_mp else None,
    ))
    # Al volver del checkout con "Atrás", el navegador tiene que pedir el ticket de nuevo
    # (si no, muestra la copia vieja "Sin cobrar" y sin el botón "Verificar pago").
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


# ---------------------------------------------------------------------------
# Mercado Pago (Checkout Pro): cobrar, volver del checkout y verificar
# ---------------------------------------------------------------------------

@bp.route("/mp/cobrar/<int:venta_id>", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def mp_cobrar(venta_id: int):
    """Crea el cobro en Mercado Pago y manda al cliente a pagar a mercadopago.com.ar."""
    venta_obj = _venta_o_404(venta_id)
    volver = redirect(url_for("pos.ticket", venta_id=venta_obj.id))

    if venta_obj.metodo_pago != METODO_MP:
        flash("Esta venta no se cobra con Mercado Pago.", "error")
        return volver
    pago_actual = _pago_mp_de(venta_obj.id)
    if pago_actual and not pago_actual.pagado:
        # Antes de crear otra order le preguntamos a MP por la anterior: en localhost nadie
        # actualiza el estado guardado, y el cliente pudo haber pagado sin que se tocara
        # "Verificar pago". Sin esto, se le podía cobrar dos veces el mismo ticket.
        try:
            _refrescar(pago_actual)
        except mp.ErrorMP as err:
            flash(err.mensaje, "error")
            return volver
    if pago_actual and pago_actual.pagado:
        flash("Este ticket ya está pagado con Mercado Pago.", "warning")
        return volver
    if pago_actual and pago_actual.pendiente:
        # Sigue esperando el pago: volvemos al MISMO checkout en vez de abrir uno nuevo,
        # así nunca quedan dos links de pago abiertos para la misma venta.
        return redirect(pago_actual.checkout_url)

    # Solo si no hubo intentos o el último terminó mal (rechazado, cancelado, vencido)
    # url_for(_external=True) sale https en Render (ProxyFix) y http en localhost.
    # crear_cobro() solo la manda a MP si es https.
    url_retorno = url_for("pos.mp_retorno", _external=True)
    try:
        cobro = mp.crear_cobro(venta_obj, url_retorno)
    except mp.ErrorMP as err:
        flash(err.mensaje, "error")
        return volver

    db.session.add(PagoMP(venta_id=venta_obj.id, order_id=cobro["id"], estado=cobro["estado"][:30],
                          checkout_url=cobro["checkout_url"][:500], fecha=ahora()))
    db.session.commit()
    return redirect(cobro["checkout_url"])


@bp.route("/mp/retorno")
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def mp_retorno():
    """Mercado Pago vuelve acá después del checkout (?order_id=...&status=...&external_reference=JBX-<id>)."""
    order_id = (request.args.get("order_id") or "").strip()
    pago = PagoMP.query.filter_by(order_id=order_id).first() if order_id else None

    # Plan B: buscar la venta por la referencia externa que mandamos al crear el cobro.
    # re.ASCII y hasta 9 dígitos: "JBX-²" o un número gigante no rompen la página (500).
    referencia = re.fullmatch(r"JBX-(\d{1,9})", request.args.get("external_reference") or "", flags=re.ASCII)
    if pago is None and referencia:
        pago = _pago_mp_de(int(referencia.group(1)))

    if pago is None:
        flash("No encontramos el cobro de Mercado Pago de este regreso.", "error")
        return redirect(url_for("pos.venta"))
    # Ignoramos el ?status= de la URL (se puede editar a mano): re-consultamos a la API.
    return _actualizar_y_volver(pago)


@bp.route("/mp/verificar/<int:venta_id>", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def mp_verificar(venta_id: int):
    """Botón "Verificar pago": vuelve a preguntarle a Mercado Pago (sirve en localhost, sin back URL)."""
    venta_obj = _venta_o_404(venta_id)
    # Alcanza con el último intento: mp_cobrar nunca abre una order nueva mientras
    # la anterior siga pendiente, así que no puede haber dos sin terminar.
    pago = _pago_mp_de(venta_obj.id)
    if pago is None:
        flash("Esta venta todavía no tiene un cobro de Mercado Pago para verificar.", "warning")
        return redirect(url_for("pos.ticket", venta_id=venta_obj.id))
    return _actualizar_y_volver(pago)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _venta_o_404(venta_id: int) -> Venta:
    venta_obj = db.session.get(Venta, venta_id)
    if venta_obj is None:
        abort(404)
    return venta_obj


def _pago_mp_de(venta_id: int) -> PagoMP | None:
    """Cobro de MP que importa de una venta: el pagado si hay alguno; si no, el último intento."""
    pagos = PagoMP.query.filter_by(venta_id=venta_id).order_by(PagoMP.id.desc()).all()
    for pago in pagos:
        if pago.pagado:
            return pago
    return pagos[0] if pagos else None


def _refrescar(pago: PagoMP) -> None:
    """Le pregunta a Mercado Pago cómo está la order y guarda el estado (puede lanzar ErrorMP)."""
    resultado = mp.consultar_cobro(pago.order_id)
    pago.estado = resultado["estado"][:30]
    pago.detalle = str(resultado["detalle"])[:60] if resultado["detalle"] else None
    pago.fecha_actualizacion = ahora()
    db.session.commit()


def _actualizar_y_volver(pago: PagoMP):
    """Consulta la order en Mercado Pago, guarda el estado y vuelve al ticket con un aviso."""
    try:
        _refrescar(pago)
    except mp.ErrorMP as err:
        flash(err.mensaje, "error")
        return redirect(url_for("pos.ticket", venta_id=pago.venta_id))

    texto, color = mp.estado_legible(pago.estado, pago.detalle)
    categoria = {mp.VERDE: "success", mp.ROJO: "error"}.get(color, "warning")
    detalle = mp.detalle_legible(pago.detalle)
    extra = f" ({detalle})" if detalle else ""
    flash(f"Mercado Pago: {texto}{extra}.", categoria)
    return redirect(url_for("pos.ticket", venta_id=pago.venta_id))
