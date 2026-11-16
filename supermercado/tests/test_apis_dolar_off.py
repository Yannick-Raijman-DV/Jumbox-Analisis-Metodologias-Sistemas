"""Pruebas de DolarAPI (cambio en el POS) y Open Food Facts (foto/proveedor por código de barras).

La red SIEMPRE está mockeada: una guardia hace fallar el test si algo sale a internet.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.extensions import db
from app.models import Producto, Proveedor, Venta, VentaCotizacion
from app.services import dolar
from app.services import openfoodfacts as off
from config import TestingConfig

DOLAR_OK = {"moneda": "USD", "casa": "oficial", "nombre": "Oficial", "compra": 1400, "venta": 1450,
            "fechaActualizacion": "2026-11-16T14:30:00.000Z"}
OFF_OK = {"status": 1, "product": {
    "product_name": "Bon o Bon", "brands": "Arcor, Otra", "manufacturing_places": "Argentina",
    "quantity": "15 g", "image_front_url": "https://images.openfoodfacts.org/images/products/779/front.jpg"}}


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
    off.limpiar_cache()
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
    with patch("app.services.dolar.requests.get", side_effect=AssertionError("¡Request real a DolarAPI!")), \
         patch("app.services.openfoodfacts.requests.get", side_effect=AssertionError("¡Request real a OFF!")):
        yield


# --------------------------- DolarAPI ---------------------------

def test_cotizacion_ok_y_cache(app):
    with patch("app.services.dolar.requests.get", return_value=resp(datos=DOLAR_OK)) as m:
        a = dolar.cotizacion()
        b = dolar.cotizacion()
    assert a["venta"] == 1450 and a["tipo"] == "oficial" and not a["desactualizada"]
    assert b == a and m.call_count == 1  # la segunda salió de la cache
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
    app.config["DOLAR_CACHE_SEGUNDOS"] = 0  # fuerza reconsulta
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


def _vender(cliente):
    prod = next(p for p in Producto.query.all() if p.stock_gondola > 0 and p.estado_vencimiento != "vencido")
    return cliente.post("/pos/api/venta", json={"items": [{"producto_id": prod.id, "cantidad": 1}],
                                                "metodo_pago": "Efectivo"}), prod


def test_venta_guarda_la_cotizacion_y_el_ticket_la_muestra(logueado):
    with patch("app.services.dolar.requests.get", return_value=resp(datos=DOLAR_OK)):
        r, prod = _vender(logueado)
    datos = r.get_json()
    assert r.status_code == 201 and datos["ok"]
    cot = VentaCotizacion.query.filter_by(venta_id=datos["venta_id"]).one()
    assert cot.valor_venta == 1450 and cot.total_usd == round(datos["total"] / 1450, 2)
    assert datos["total_usd"] == cot.total_usd
    html = logueado.get(datos["ticket_url"]).get_data(as_text=True)
    assert "US$" in html and "1450.00" in html


def test_venta_sigue_andando_si_dolarapi_cae(logueado):
    with patch("app.services.dolar.requests.get", side_effect=requests.ConnectionError()):
        r, _ = _vender(logueado)
    datos = r.get_json()
    assert r.status_code == 201 and datos["ok"] and datos["total_usd"] is None
    assert VentaCotizacion.query.count() == 0
    assert db.session.get(Venta, datos["venta_id"]) is not None
    assert "US$" not in logueado.get(datos["ticket_url"]).get_data(as_text=True)


# --------------------------- Open Food Facts ---------------------------

def test_buscar_por_codigo_ok(app):
    with patch("app.services.openfoodfacts.requests.get", return_value=resp(datos=OFF_OK)) as m:
        p = off.buscar_por_codigo("7790040000011")
        off.buscar_por_codigo("7790040000011")
    assert p["nombre"] == "Bon o Bon" and p["marca"] == "Arcor" and p["fabricante"] == "Argentina"
    assert p["imagen_url"].startswith("https://images.openfoodfacts.org/")
    assert m.call_count == 1
    assert "User-Agent" in m.call_args.kwargs["headers"]


@pytest.mark.parametrize("codigo", ["", "abc", "123", "7790040000011; DROP", "1" * 20])
def test_codigo_invalido_no_llama_a_la_api(app, codigo):
    with pytest.raises(off.ErrorOFF):
        off.buscar_por_codigo(codigo)


@pytest.mark.parametrize("falla", [resp(datos={"status": 0}), resp(status=404), resp(status=429),
                                   resp(status=500), requests.Timeout(), requests.ConnectionError()])
def test_off_errores_son_amigables(app, falla):
    kw = {"side_effect": falla} if isinstance(falla, Exception) else {"return_value": falla}
    with patch("app.services.openfoodfacts.requests.get", **kw):
        with pytest.raises(off.ErrorOFF):
            off.buscar_por_codigo("7790040000011")


def test_sugerir_proveedor(app):
    provs = Proveedor.query.all()
    nombre = provs[0].nombre
    assert off.sugerir_proveedor(nombre.upper(), provs).id == provs[0].id
    assert off.sugerir_proveedor("Marca Inexistente XYZ", provs) is None
    assert off.sugerir_proveedor(None, provs) is None

    class P:  # proveedor falso: la marca de OFF trae guion y el nombre cargado no
        def __init__(self, nombre):
            self.nombre = nombre
    assert off.sugerir_proveedor("Coca-Cola", [P("CocaCola Company")]).nombre == "CocaCola Company"
    assert off.sugerir_proveedor("Nestlé", [P("NESTLE S.A.")]).nombre == "NESTLE S.A."
    assert off.sugerir_proveedor("---", [P("Arcor")]) is None


def test_endpoint_off_sugiere_proveedor(logueado):
    prov = Proveedor.query.first()
    datos = {"status": 1, "product": {**OFF_OK["product"], "brands": prov.nombre}}
    with patch("app.services.openfoodfacts.requests.get", return_value=resp(datos=datos)):
        r = logueado.get("/stock/api/off/7790040000011")
    j = r.get_json()
    assert r.status_code == 200 and j["proveedor_id"] == prov.id
    with patch("app.services.openfoodfacts.requests.get", return_value=resp(datos={"status": 0})):
        r = logueado.get("/stock/api/off/7790040000099")
    assert r.status_code == 404 and r.get_json()["ok"] is False


def test_descargar_imagen_solo_de_off(app):
    for url in ["http://images.openfoodfacts.org/a.jpg", "https://evil.com/a.jpg",
                "https://openfoodfacts.org.evil.com/a.jpg", "https://127.0.0.1/a.jpg", ""]:
        with pytest.raises(off.ErrorOFF):
            off.descargar_imagen(url)
    ok = resp(contenido=b"\xff\xd8jpg", tipo="image/jpeg")
    with patch("app.services.openfoodfacts.requests.get", return_value=ok):
        archivo, ext = off.descargar_imagen("https://images.openfoodfacts.org/a.jpg")
    assert ext == ".jpg" and archivo.read() == b"\xff\xd8jpg"
    with patch("app.services.openfoodfacts.requests.get", return_value=resp(contenido=b"<html>", tipo="text/html")):
        with pytest.raises(off.ErrorOFF):
            off.descargar_imagen("https://images.openfoodfacts.org/a.jpg")


def test_nuevo_producto_guarda_la_foto_de_off(logueado, app, tmp_path):
    from app.models import Categoria
    prov, cat = Proveedor.query.first(), Categoria.query.first()
    foto = resp(contenido=b"\xff\xd8jpg", tipo="image/jpeg")
    with patch("app.services.openfoodfacts.requests.get", return_value=foto), \
         patch("app.services.s3.subir_archivo", return_value="productos/abc.jpg") as sube:
        r = logueado.post("/stock/productos/nuevo", data={
            "nombre": "Bon o Bon Test", "codigo_barras": "7790040999999", "precio": "100", "stock_gondola": "1",
            "stock_deposito": "1", "stock_minimo": "1", "categoria_id": cat.id, "proveedor_id": prov.id,
            "fecha_vencimiento": "2030-01-01", "off_imagen_url": "https://images.openfoodfacts.org/x.jpg",
        }, follow_redirects=True)
    assert r.status_code == 200 and sube.called
    assert Producto.query.filter_by(codigo_barras="7790040999999").one().foto_key == "productos/abc.jpg"
