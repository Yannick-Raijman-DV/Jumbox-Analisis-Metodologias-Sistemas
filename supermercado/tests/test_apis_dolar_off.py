"""Pruebas de DolarAPI (cambio bajo demanda en el POS).

La red SIEMPRE está mockeada: una guardia hace fallar el test si algo sale a internet.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.extensions import db
from app.models import Producto, Venta, VentaCotizacion
from app.services import dolar
from config import TestingConfig

DOLAR_OK = {"moneda": "USD", "casa": "oficial", "nombre": "Oficial", "compra": 1400, "venta": 1450,
            "fechaActualizacion": "2026-11-16T14:30:00.000Z"}


def resp(status=200, datos=None, contenido=b"", tipo="application/json"):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = datos
    r.content = contenido
    r.headers = {"content-type": tipo}
    return r


@pytest.fixture()
def app():
    aplicacion = create_app(TestingConfig)
    dolar.limpiar_cache()
    with aplicacion.app_context():
        yield aplicacion
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def logueado(app):
    c = app.test_client()
    c.post("/login", data={"username": "administrador", "password": "1234"}, follow_redirects=True)
    return c


@pytest.fixture(autouse=True)
def guardia_red():
    with patch("app.services.dolar.requests.get", side_effect=AssertionError("¡Request real a DolarAPI!")):
        yield


def test_cotizacion_ok_y_cache(app):
    with patch("app.services.dolar.requests.get", return_value=resp(datos=DOLAR_OK)) as m:
        a = dolar.cotizacion()
        b = dolar.cotizacion()
    assert a["venta"] == 1450 and a["tipo"] == "oficial" and not a["desactualizada"]
    assert b == a and m.call_count == 1
    assert m.call_args.args[0] == "https://dolarapi.com/v1/dolares/oficial"


def test_pesos_a_usd():
    assert dolar.pesos_a_usd(2900, 1450) == 2.0
    assert dolar.pesos_a_usd(1000, 1450) == 0.69


@pytest.mark.parametrize("falla", [requests.Timeout(), requests.ConnectionError(), resp(status=500),
                                   resp(status=429), resp(datos={"x": 1}), resp(datos={"venta": 0})])
def test_cotizacion_errores_son_amigables(app, falla):
    kw = {"side_effect": falla} if isinstance(falla, Exception) else {"return_value": falla}
    with patch("app.services.dolar.requests.get", **kw):
        with pytest.raises(dolar.ErrorDolar):
            dolar.cotizacion()
        assert dolar.cotizacion_o_none() is None


def test_cotizacion_vieja_se_usa_si_la_api_cae(app):
    with patch("app.services.dolar.requests.get", return_value=resp(datos=DOLAR_OK)):
        dolar.cotizacion()
    app.config["DOLAR_CACHE_SEGUNDOS"] = 0
    with patch("app.services.dolar.requests.get", side_effect=requests.ConnectionError()):
        cot = dolar.cotizacion()
    assert cot["venta"] == 1450 and cot["desactualizada"] is True


def test_tipo_invalido_cae_en_oficial(app):
    app.config["DOLAR_TIPO"] = "../../etc"
    assert dolar.tipo_configurado() == "oficial"


def test_api_dolar_endpoint(logueado):
    with patch("app.services.dolar.requests.get", return_value=resp(datos=DOLAR_OK)):
        r = logueado.get("/pos/api/dolar")
    assert r.status_code == 200 and r.get_json()["venta"] == 1450
    with patch("app.services.dolar.requests.get", side_effect=requests.Timeout()):
        dolar.limpiar_cache()
        r = logueado.get("/pos/api/dolar")
    assert r.status_code == 502 and r.get_json()["ok"] is False


def _vender(cliente, en_usd=True):
    prod = next(p for p in Producto.query.all() if p.stock_gondola > 0 and p.estado_vencimiento != "vencido")
    return cliente.post("/pos/api/venta", json={
        "items": [{"producto_id": prod.id, "cantidad": 1}],
        "metodo_pago": "Efectivo",
        "en_usd": en_usd,
    }), prod


def test_venta_en_pesos_no_consulta_dolar_ni_guarda_cotizacion(logueado):
    # Por defecto (en_usd=False), ni siquiera llama a DolarAPI
    r, _ = _vender(logueado, en_usd=False)
    datos = r.get_json()
    assert r.status_code == 201 and datos["ok"] and datos["total_usd"] is None
    assert VentaCotizacion.query.count() == 0
    html = logueado.get(datos["ticket_url"]).get_data(as_text=True)
    assert "US$" not in html


def test_venta_con_usd_guarda_la_cotizacion_y_el_ticket_la_muestra(logueado):
    with patch("app.services.dolar.requests.get", return_value=resp(datos=DOLAR_OK)):
        r, prod = _vender(logueado, en_usd=True)
    datos = r.get_json()
    assert r.status_code == 201 and datos["ok"]
    cot = VentaCotizacion.query.filter_by(venta_id=datos["venta_id"]).one()
    assert cot.valor_venta == 1450 and cot.total_usd == round(datos["total"] / 1450, 2)
    assert datos["total_usd"] == cot.total_usd
    html = logueado.get(datos["ticket_url"]).get_data(as_text=True)
    assert "US$" in html and "1450.00" in html


def test_venta_sigue_andando_si_dolarapi_cae(logueado):
    with patch("app.services.dolar.requests.get", side_effect=requests.ConnectionError()):
        r, _ = _vender(logueado, en_usd=True)
    datos = r.get_json()
    assert r.status_code == 201 and datos["ok"] and datos["total_usd"] is None
    assert VentaCotizacion.query.count() == 0
    assert db.session.get(Venta, datos["venta_id"]) is not None
    assert "US$" not in logueado.get(datos["ticket_url"]).get_data(as_text=True)
