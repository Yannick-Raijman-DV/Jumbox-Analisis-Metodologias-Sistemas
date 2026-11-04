"""Pruebas de la integración con Mercado Pago (Checkout Pro vía Orders API).

La red está SIEMPRE mockeada: se parchean requests.post / requests.get dentro de
app.services.mercadopago, así nunca se le pega a la API real.
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.extensions import db
from app.models import PagoMP
from app.services import mercadopago as mp
from config import TestingConfig

TOKEN = "APP_USR-1234567890-token-de-prueba-SECRETO"
ORDER_ID = "ORD01JTESTJUMBOX"
CHECKOUT_URL = "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=ORD01JTESTJUMBOX"
ORDEN_CREADA = {
    "id": ORDER_ID,
    "status": "created",
    "status_detail": "created",
    "checkout_url": CHECKOUT_URL,
    "total_amount": "20.00",
}


@pytest.fixture()
def app():
    class TestCfg(TestingConfig):
        MP_ACCESS_TOKEN = TOKEN

    aplicacion = create_app(TestCfg)
    with aplicacion.app_context():
        yield aplicacion
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login_como(client, username: str, password: str = "1234"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def respuesta(status: int = 200, datos=None):
    """Respuesta HTTP falsa con el status y el JSON indicados."""
    resp = MagicMock()
    resp.status_code = status
    if datos is None:
        resp.json.side_effect = ValueError("no es JSON")
    else:
        resp.json.return_value = datos
    return resp


def orden_consultada(status: str, detalle: str, pagado: str = "0.00") -> dict:
    return {"id": ORDER_ID, "status": status, "status_detail": detalle,
            "total_paid_amount": pagado, "external_reference": "JBX-1"}


def vender(client, metodo: str = "Mercado Pago") -> int:
    """Registra una venta por el POS: 2 Pepsi a $10 = $20."""
    r = client.post("/pos/api/venta", json={"items": [{"producto_id": 5, "cantidad": 2}], "metodo_pago": metodo})
    assert r.status_code == 201
    return r.get_json()["venta_id"]


def cobrar(client, venta_id: int):
    """Toca "Pagar con Mercado Pago" con la creación de la order mockeada."""
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(201, ORDEN_CREADA)) as post:
        r = client.post(f"/pos/mp/cobrar/{venta_id}")
    return r, post


# ---------------------------------------------------------------------------
# 1. Servicio: armado del request, consulta, estados y errores
# ---------------------------------------------------------------------------

def test_crear_cobro_arma_body_y_headers(app):
    venta = SimpleNamespace(id=42, total=1234.5)
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(201, ORDEN_CREADA)) as post:
        cobro_local = mp.crear_cobro(venta, "http://localhost:5000/pos/mp/retorno")
        mp.crear_cobro(venta, "https://jumbox.onrender.com/pos/mp/retorno")

    assert cobro_local == {"id": ORDER_ID, "checkout_url": CHECKOUT_URL, "estado": "created"}
    assert post.call_count == 2

    # Primer llamado (localhost, http): sin config.online
    args, kwargs = post.call_args_list[0]
    assert args[0] == "https://api.mercadopago.com/v1/orders"
    assert kwargs["timeout"] == 15
    body = kwargs["json"]
    assert body["type"] == "online"
    assert body["processing_mode"] == "manual"
    assert body["total_amount"] == "1234.50"  # string con 2 decimales
    assert body["external_reference"] == "JBX-42"
    assert body["description"] == "Jumbox - Ticket #42"
    assert body["items"] == [{"title": "Compra Jumbox ticket #42", "quantity": 1, "unit_price": "1234.50"}]
    assert "config" not in body
    assert "allowed_user_type" not in json.dumps(body)

    headers = kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    uuid.UUID(headers["X-Idempotency-Key"])  # tiene que ser un uuid válido

    # Segundo llamado (Render, https): con back URLs y auto_return
    _args, kwargs_https = post.call_args_list[1]
    online = kwargs_https["json"]["config"]["online"]
    assert online == {
        "success_url": "https://jumbox.onrender.com/pos/mp/retorno",
        "failure_url": "https://jumbox.onrender.com/pos/mp/retorno",
        "pending_url": "https://jumbox.onrender.com/pos/mp/retorno",
        "auto_return": "approved",
    }
    assert "allowed_user_type" not in json.dumps(kwargs_https["json"])
    # Una clave de idempotencia nueva por intento (si se repite, MP responde 409)
    assert kwargs_https["headers"]["X-Idempotency-Key"] != headers["X-Idempotency-Key"]


def test_crear_cobro_valida_respuesta_y_monto(app):
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(201, {"id": ORDER_ID})) as post:
        with pytest.raises(mp.ErrorMP, match="link de pago"):
            mp.crear_cobro(SimpleNamespace(id=1, total=10.0), None)
        with pytest.raises(mp.ErrorMP, match="monto"):
            mp.crear_cobro(SimpleNamespace(id=2, total=0.0), None)
    assert post.call_count == 1  # la venta de $0 ni siquiera llega a la API


def test_consultar_cobro(app):
    with patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("processed", "accredited", "20.00"))) as get:
        estado = mp.consultar_cobro(ORDER_ID)

    assert estado == {"estado": "processed", "detalle": "accredited", "pagado": True, "monto": "20.00"}
    args, kwargs = get.call_args
    assert args[0] == f"https://api.mercadopago.com/v1/orders/{ORDER_ID}"
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "X-Idempotency-Key" not in kwargs["headers"]
    assert kwargs["timeout"] == 15


def test_estado_legible_colores():
    assert mp.estado_legible("processed", "accredited") == ("Pagado", "#006c49")
    assert mp.estado_legible("created", "created") == ("Pendiente", "#6c4300")
    assert mp.estado_legible("processing", "in_review") == ("Pendiente", "#6c4300")
    assert mp.estado_legible("action_required", "waiting_payment") == ("Pendiente", "#6c4300")
    assert mp.estado_legible("failed", "rejected_by_issuer") == ("Rechazado", "#ba1a1a")
    assert mp.estado_legible("canceled", "canceled") == ("Cancelado", "#ba1a1a")
    assert mp.estado_legible("expired", "expired") == ("Cancelado", "#ba1a1a")
    assert mp.estado_legible("refunded", "refunded")[1] == "#ba1a1a"
    assert mp.estado_legible(None) == ("Sin cobrar", "#00375e")


def test_detalle_legible_en_castellano():
    assert mp.detalle_legible("accredited") == "acreditado"
    assert mp.detalle_legible("rejected_by_issuer") == "lo rechazó el banco"
    assert mp.detalle_legible("un_codigo_nuevo_de_mp") is None  # desconocido: no se muestra
    assert mp.detalle_legible(None) is None


def test_configurado_y_formato_del_token(app):
    assert mp.configurado() is True
    assert mp.es_token_de_prueba() is True

    app.config["MP_ACCESS_TOKEN"] = "TEST-1234-app-vieja"
    assert mp.es_token_de_prueba() is True
    app.config["MP_ACCESS_TOKEN"] = "cualquier-cosa"
    assert mp.es_token_de_prueba() is False

    app.config["MP_ACCESS_TOKEN"] = None
    assert mp.configurado() is False
    assert mp.es_token_de_prueba() is False
    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get") as get:
        with pytest.raises(mp.ErrorMP, match="MP_ACCESS_TOKEN"):
            mp.crear_cobro(SimpleNamespace(id=1, total=10.0), None)
        with pytest.raises(mp.ErrorMP, match="MP_ACCESS_TOKEN"):
            mp.consultar_cobro(ORDER_ID)
    post.assert_not_called()
    get.assert_not_called()


@pytest.mark.parametrize(
    "status, datos, fragmento",
    [
        (400, {"errors": [{"code": "order_items_total_amount_mismatch",
                           "message": "The total amount does not match the items"}]}, "does not match"),
        (400, None, "sin detalle"),
        (401, {"errors": [{"code": "invalid_credentials", "message": "Test credentials are not supported"}]},
         "Access Token de PRUEBA"),
        (404, {}, "no encontró"),
        (409, {}, "duplicado"),
        (429, {}, "Esperá un minuto"),
        (503, {}, "con problemas"),
    ],
)
def test_errores_http_con_mensajes_amigables(app, status, datos, fragmento):
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(status, datos)):
        with pytest.raises(mp.ErrorMP) as error:
            mp.crear_cobro(SimpleNamespace(id=7, total=99.9), None)
    assert fragmento in error.value.mensaje
    assert error.value.codigo == status
    assert TOKEN not in error.value.mensaje


def test_errores_de_red_no_muestran_url_ni_token(app):
    # El texto de las excepciones de requests trae la URL completa: nunca se tiene que ver
    texto_crudo = f"HTTPSConnectionPool(host='api.mercadopago.com', port=443): Max retries (Bearer {TOKEN})"
    with patch("app.services.mercadopago.requests.get", side_effect=requests.ConnectionError(texto_crudo)):
        with pytest.raises(mp.ErrorMP) as error:
            mp.consultar_cobro(ORDER_ID)
    assert "conexión" in error.value.mensaje
    assert TOKEN not in error.value.mensaje and "api.mercadopago.com" not in error.value.mensaje
    assert error.value.codigo is None

    with patch("app.services.mercadopago.requests.post", side_effect=requests.Timeout(texto_crudo)):
        with pytest.raises(mp.ErrorMP, match="tardó demasiado") as error:
            mp.crear_cobro(SimpleNamespace(id=1, total=10.0), None)
    assert TOKEN not in error.value.mensaje


# ---------------------------------------------------------------------------
# 2. Flujo completo con el test client (cajero en el POS)
# ---------------------------------------------------------------------------

def test_flujo_completo_pago_aprobado(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)

    # Ticket antes de cobrar: badge "Sin cobrar", botón de pago y ayuda de la tarjeta de prueba
    r = client.get(f"/pos/ticket/{venta_id}")
    html = r.get_data(as_text=True)
    assert "Estado MP:" in html and "Sin cobrar" in html
    assert "Pagar con Mercado Pago" in html
    assert "5031 7557 3453 0604" in html and "email: cualquiera válido" in html
    assert "abrí Jumbox en una ventana de incógnito" in html
    assert f"/pos/mp/verificar/{venta_id}" not in html  # todavía no hay cobro para verificar
    assert TOKEN not in html
    # Al volver del checkout con "Atrás" el ticket se pide de nuevo (no la copia vieja)
    assert r.headers["Cache-Control"] == "no-store"
    assert "pageshow" in html

    # "Pagar con Mercado Pago" -> se crea la order y se redirige al checkout de MP
    r, post = cobrar(client, venta_id)
    assert r.status_code == 302
    assert r.headers["Location"] == CHECKOUT_URL
    post.assert_called_once()
    body = post.call_args.kwargs["json"]
    assert body["total_amount"] == "20.00"
    assert body["external_reference"] == f"JBX-{venta_id}"
    assert "config" not in body  # el test client es http://localhost: no se mandan back URLs

    pago = PagoMP.query.filter_by(venta_id=venta_id).one()
    assert pago.order_id == ORDER_ID
    assert pago.estado == "created"
    assert pago.checkout_url == CHECKOUT_URL

    html = client.get(f"/pos/ticket/{venta_id}").get_data(as_text=True)
    assert "Pendiente" in html and "Verificar pago" in html and ORDER_ID in html

    # MP vuelve a Jumbox: el backend re-consulta la order y recién ahí lo da por pagado
    with patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("processed", "accredited", "20.00"))) as get:
        r = client.get(f"/pos/mp/retorno?order_id={ORDER_ID}&status=approved&external_reference=JBX-{venta_id}",
                       follow_redirects=True)
    get.assert_called_once()
    assert get.call_args.args[0].endswith(f"/v1/orders/{ORDER_ID}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert f"Ticket #{venta_id}" in html
    assert "Mercado Pago: Pagado (acreditado)." in html  # flash, con el detalle en castellano
    assert "accredited" not in html  # el código crudo de MP no llega a la pantalla
    assert "Pagar con Mercado Pago" not in html  # ya está pagado: no se ofrece pagar de nuevo
    assert TOKEN not in html

    pago = db.session.get(PagoMP, pago.id)
    assert pago.estado == "processed" and pago.detalle == "accredited"
    assert pago.fecha_actualizacion is not None

    # Si ya está pagado, no se crea otra order (ni hace falta volver a consultar)
    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get") as get:
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)
    post.assert_not_called()
    get.assert_not_called()
    assert "ya está pagado" in r.get_data(as_text=True)
    assert PagoMP.query.filter_by(venta_id=venta_id).count() == 1


def test_retorno_rechazado_no_confia_en_el_status_de_la_url(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)
    cobrar(client, venta_id)

    # La URL dice status=approved, pero la API dice failed: manda la API.
    # Además se busca el cobro por external_reference (sin order_id en la URL).
    with patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("failed", "rejected_by_issuer"))):
        r = client.get(f"/pos/mp/retorno?status=approved&external_reference=JBX-{venta_id}", follow_redirects=True)

    html = r.get_data(as_text=True)
    assert "Rechazado" in html
    assert "Pagado" not in html
    assert "Pagar con Mercado Pago" in html  # se puede reintentar
    assert PagoMP.query.filter_by(venta_id=venta_id).one().estado == "failed"

    # Un regreso que no corresponde a ningún cobro no rompe nada
    with patch("app.services.mercadopago.requests.get") as get:
        r = client.get("/pos/mp/retorno?order_id=ORD-INEXISTENTE&external_reference=JBX-abc", follow_redirects=True)
    get.assert_not_called()
    assert "No encontramos el cobro" in r.get_data(as_text=True)


def test_verificar_pago_pendiente(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)

    # Sin cobro creado todavía: avisa y no consulta la API
    with patch("app.services.mercadopago.requests.get") as get:
        r = client.post(f"/pos/mp/verificar/{venta_id}", follow_redirects=True)
    get.assert_not_called()
    assert "todavía no tiene un cobro" in r.get_data(as_text=True)

    cobrar(client, venta_id)
    with patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("processing", "in_process"))) as get:
        r = client.post(f"/pos/mp/verificar/{venta_id}", follow_redirects=True)
    get.assert_called_once()
    html = r.get_data(as_text=True)
    assert "Mercado Pago: Pendiente (en revisión)." in html
    assert PagoMP.query.filter_by(venta_id=venta_id).one().estado == "processing"


# ---------------------------------------------------------------------------
# 2b. Tocar "Pagar" de nuevo nunca cobra dos veces el mismo ticket
# ---------------------------------------------------------------------------

def test_pagar_de_nuevo_si_el_cliente_ya_pago_no_crea_otra_order(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)
    cobrar(client, venta_id)  # order creada; el cliente paga, pero nadie toca "Verificar pago"

    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("processed", "accredited", "20.00"))) as get:
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)

    get.assert_called_once()  # primero le pregunta a MP por la order anterior...
    assert get.call_args.args[0].endswith(f"/v1/orders/{ORDER_ID}")
    post.assert_not_called()  # ...y como ya está pagada, no abre otra
    html = r.get_data(as_text=True)
    assert "ya está pagado" in html
    assert "Pagado" in html and "Pagar con Mercado Pago" not in html
    assert PagoMP.query.filter_by(venta_id=venta_id).one().estado == "processed"


def test_pagar_de_nuevo_si_sigue_pendiente_reusa_el_checkout(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)
    cobrar(client, venta_id)

    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("created", "created"))):
        r = client.post(f"/pos/mp/cobrar/{venta_id}")
    post.assert_not_called()
    assert r.status_code == 302 and r.headers["Location"] == CHECKOUT_URL  # el MISMO link de pago
    assert PagoMP.query.filter_by(venta_id=venta_id).count() == 1

    # Si no se puede consultar a MP, tampoco se arriesga a abrir otra order
    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get", side_effect=requests.ConnectionError("x")):
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)
    post.assert_not_called()
    assert "No hay conexión con Mercado Pago" in r.get_data(as_text=True)
    assert PagoMP.query.filter_by(venta_id=venta_id).count() == 1


def test_pagar_de_nuevo_despues_de_un_rechazo_crea_otra_order(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)
    cobrar(client, venta_id)

    segunda = {**ORDEN_CREADA, "id": "ORD02JTESTJUMBOX", "checkout_url": CHECKOUT_URL + "-2"}
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(201, segunda)) as post, \
         patch("app.services.mercadopago.requests.get",
               return_value=respuesta(200, orden_consultada("failed", "rejected_by_issuer"))):
        r = client.post(f"/pos/mp/cobrar/{venta_id}")
    post.assert_called_once()  # la anterior terminó rechazada: ahí sí se abre una nueva
    assert r.status_code == 302 and r.headers["Location"] == CHECKOUT_URL + "-2"
    estados = [(p.order_id, p.estado) for p in PagoMP.query.filter_by(venta_id=venta_id).order_by(PagoMP.id)]
    assert estados == [(ORDER_ID, "failed"), ("ORD02JTESTJUMBOX", "created")]


def test_rechazo_real_de_orders_se_ve_rechazado_y_reintenta_en_el_mismo_checkout(app, client):
    """Respuesta REAL de MP (sandbox, titular OTHE): la order queda abierta y el rechazo está en el pago."""
    rechazada = {
        "id": ORDER_ID, "status": "action_required", "status_detail": "waiting_retry",
        "total_paid_amount": None, "external_reference": "JBX-1",
        "transactions": {"payments": [{"id": "PAY01TEST", "status": "failed", "status_detail": "rejected_by_issuer"}]},
    }
    with patch("app.services.mercadopago.requests.get", return_value=respuesta(200, rechazada)):
        resultado = mp.consultar_cobro(ORDER_ID)
    assert resultado == {"estado": "rejected", "detalle": "rejected_by_issuer", "pagado": False, "monto": None}
    assert mp.estado_legible("rejected", "rejected_by_issuer") == ("Rechazado", mp.ROJO)

    login_como(client, "cajero")
    venta_id = vender(client)
    cobrar(client, venta_id)
    with patch("app.services.mercadopago.requests.get", return_value=respuesta(200, rechazada)):
        r = client.post(f"/pos/mp/verificar/{venta_id}", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Mercado Pago: Rechazado (lo rechazó el banco)." in html
    assert "Pagar con Mercado Pago" in html  # se puede reintentar

    # Reintentar NO abre otra order: vuelve al mismo checkout (MP lo dejó esperando otro intento)
    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get", return_value=respuesta(200, rechazada)):
        r = client.post(f"/pos/mp/cobrar/{venta_id}")
    post.assert_not_called()
    assert r.status_code == 302 and r.headers["Location"] == CHECKOUT_URL
    assert PagoMP.query.filter_by(venta_id=venta_id).count() == 1

    # Si en el reintento paga con APRO, la misma order pasa a processed y el ticket queda Pagado
    aprobada = {**rechazada, "status": "processed", "status_detail": "accredited", "total_paid_amount": "20.00",
                "transactions": {"payments": [rechazada["transactions"]["payments"][0],
                                              {"id": "PAY02TEST", "status": "processed", "status_detail": "accredited"}]}}
    with patch("app.services.mercadopago.requests.get", return_value=respuesta(200, aprobada)):
        r = client.post(f"/pos/mp/verificar/{venta_id}", follow_redirects=True)
    assert "Mercado Pago: Pagado (acreditado)." in r.get_data(as_text=True)
    assert PagoMP.query.filter_by(venta_id=venta_id).one().pagado


@pytest.mark.parametrize("referencia", ["JBX-%C2%B2", "JBX-999999999999999999999999999999", "JBX-", "JBX-12a"])
def test_retorno_con_referencia_rara_no_rompe(app, client, referencia):
    # "²".isdigit() es True e int("²") explota; un número gigante desborda SQLite: los dos daban 500
    login_como(client, "cajero")
    with patch("app.services.mercadopago.requests.get") as get:
        r = client.get(f"/pos/mp/retorno?external_reference={referencia}", follow_redirects=True)
    get.assert_not_called()
    assert r.status_code == 200
    assert "No encontramos el cobro" in r.get_data(as_text=True)


def test_en_https_manda_back_urls(app, client):
    # En Render el proxy avisa que es https (ProxyFix): ahí sí se mandan las back URLs
    login_como(client, "cajero")
    venta_id = vender(client)
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(201, ORDEN_CREADA)) as post:
        r = client.post(f"/pos/mp/cobrar/{venta_id}", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 302
    online = post.call_args.kwargs["json"]["config"]["online"]
    assert online["success_url"] == "https://localhost/pos/mp/retorno"
    assert online["auto_return"] == "approved"


def test_sin_token_avisa_y_no_hace_requests(app, client):
    app.config["MP_ACCESS_TOKEN"] = None
    login_como(client, "cajero")
    venta_id = vender(client)

    html = client.get(f"/pos/ticket/{venta_id}").get_data(as_text=True)
    assert "Mercado Pago no está configurado" in html
    assert "Pagar con Mercado Pago" not in html  # sin botones rotos

    with patch("app.services.mercadopago.requests.post") as post:
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)
    post.assert_not_called()
    assert "falta MP_ACCESS_TOKEN" in r.get_data(as_text=True)
    assert PagoMP.query.count() == 0


def test_credenciales_invalidas_401_y_token_oculto(app, client):
    login_como(client, "cajero")
    venta_id = vender(client)

    error_401 = {"errors": [{"code": "invalid_credentials",
                             "message": "Test credentials are not supported. Use test users with production credentials"}]}
    with patch("app.services.mercadopago.requests.post", return_value=respuesta(401, error_401)):
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "rechazó las credenciales (401)" in html
    assert "Access Token de PRUEBA" in html
    assert TOKEN not in html
    assert PagoMP.query.count() == 0

    # Error de red con la URL y el token en el texto crudo: el cajero ve un mensaje propio
    crudo = f"HTTPSConnectionPool(host='api.mercadopago.com'): Bearer {TOKEN}"
    with patch("app.services.mercadopago.requests.post", side_effect=requests.ConnectionError(crudo)):
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "No hay conexión con Mercado Pago" in html
    assert TOKEN not in html and "HTTPSConnectionPool" not in html


def test_venta_que_no_es_mercado_pago(app, client):
    login_como(client, "cajero")
    venta_id = vender(client, metodo="Efectivo")

    html = client.get(f"/pos/ticket/{venta_id}").get_data(as_text=True)
    assert "Pagar con Mercado Pago" not in html
    assert "Estado MP:" not in html

    with patch("app.services.mercadopago.requests.post") as post:
        r = client.post(f"/pos/mp/cobrar/{venta_id}", follow_redirects=True)
    post.assert_not_called()
    assert "no se cobra con Mercado Pago" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# 3. Permisos: Admin y Cajero pueden cobrar; Operario no
# ---------------------------------------------------------------------------

def test_permisos_operario_403_admin_ok(app, client):
    login_como(client, "operario")
    with patch("app.services.mercadopago.requests.post") as post, \
         patch("app.services.mercadopago.requests.get") as get:
        assert client.post("/pos/mp/cobrar/1").status_code == 403
        assert client.get(f"/pos/mp/retorno?order_id={ORDER_ID}").status_code == 403
        assert client.post("/pos/mp/verificar/1").status_code == 403
        assert client.get("/pos/ticket/1").status_code == 403
    post.assert_not_called()
    get.assert_not_called()
    client.get("/logout")

    login_como(client, "administrador")
    venta_id = vender(client)
    r, _post = cobrar(client, venta_id)
    assert r.status_code == 302 and r.headers["Location"] == CHECKOUT_URL

    # Sin sesión: redirige al login (no llega a Mercado Pago)
    client.get("/logout")
    with patch("app.services.mercadopago.requests.post") as post:
        r = client.post(f"/pos/mp/cobrar/{venta_id}")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    post.assert_not_called()
