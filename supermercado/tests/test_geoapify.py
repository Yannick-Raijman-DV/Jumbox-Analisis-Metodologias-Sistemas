"""Pruebas de la integración con Geoapify (geocoding + routing + mapa estático) en Envíos.

Nunca se hace un request real: `requests.get` está SIEMPRE mockeado. Hay una "guardia"
(fixture autouse) que hace fallar el test si algo intenta salir a internet sin mock.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from app import create_app
from app.extensions import db
from app.models import Envio
from app.services import geo
from config import TestingConfig

CLAVE = "clave-geoapify-secreta-123"  # key falsa: verificamos que NUNCA aparezca en pantalla

GEOCODE_OK = {
    "results": [{
        "lat": -34.5631,
        "lon": -58.4566,
        "formatted": "Avenida Cabildo 1540, C1426 Buenos Aires, Argentina",
        "rank": {"confidence": 0.95},
    }]
}
RUTA_OK = {"features": [{"properties": {"distance": 7843, "time": 1090}}]}  # 7,8 km y 18 min
PNG_FALSO = b"\x89PNG\r\n\x1a\n-mapa-de-prueba"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    class TestCfg(TestingConfig):
        # Valores fijos para que los tests no dependan de lo que haya en .env
        GEOAPIFY_API_KEY = CLAVE
        SUCURSAL_LAT = -34.6037
        SUCURSAL_LON = -58.3816
        ENVIO_COSTO_BASE = 1500.0
        ENVIO_COSTO_KM = 400.0

    aplicacion = create_app(TestCfg)
    geo.limpiar_cache()
    with aplicacion.app_context():
        yield aplicacion
        db.session.remove()
        db.drop_all()
    geo.limpiar_cache()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def logueado(client):
    client.post("/login", data={"username": "administrador", "password": "1234"}, follow_redirects=True)
    return client


@pytest.fixture(autouse=True)
def guardia_red():
    """Si algún test llama a Geoapify sin mockear, falla en vez de salir a internet."""
    with patch("app.services.geo.requests.get", side_effect=AssertionError("¡Request real a Geoapify!")) as guardia:
        yield guardia


def respuesta(status=200, datos=None, contenido=b"", tipo="application/json"):
    """Arma una respuesta falsa de requests."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = datos
    r.content = contenido
    r.headers = {"content-type": tipo}
    return r


def geoapify_falso(url, params=None, timeout=None):
    """Simula los 3 endpoints de Geoapify según la URL."""
    if url == geo.URL_GEOCODING:
        return respuesta(datos=GEOCODE_OK)
    if url == geo.URL_ROUTING:
        return respuesta(datos=RUTA_OK)
    if url == geo.URL_MAPA:
        return respuesta(contenido=PNG_FALSO, tipo="image/png")
    raise AssertionError(f"URL inesperada: {url}")


# ---------------------------------------------------------------------------
# 1. Servicio app/services/geo.py
# ---------------------------------------------------------------------------

def test_cotizar_ok_calcula_distancia_tiempo_y_costo(app):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        res = geo.cotizar("Av. Cabildo 1540, CABA")

    assert res["ok"] is True
    assert res["error"] is None and res["aviso"] is None
    assert res["direccion_formateada"].startswith("Avenida Cabildo 1540")
    assert (res["lat"], res["lon"]) == (-34.5631, -58.4566)
    assert res["km"] == 7.8
    assert res["minutos"] == 18
    assert res["costo"] == round(1500 + 7.8 * 400) == 4620  # base + km * costo_km

    llamada_geocode, llamada_ruta = mock_get.call_args_list
    p = llamada_geocode.kwargs["params"]
    assert llamada_geocode.args[0] == geo.URL_GEOCODING
    assert p["text"] == "Av. Cabildo 1540, CABA, Buenos Aires, Argentina"
    assert p["filter"] == "countrycode:ar"
    assert p["bias"] == "proximity:-58.3816,-34.6037"  # proximity va lon,lat
    assert (p["lang"], p["format"], p["limit"]) == ("es", "json", 1)
    assert p["apiKey"] == CLAVE
    assert llamada_geocode.kwargs["timeout"] > 0

    r = llamada_ruta.kwargs["params"]
    assert llamada_ruta.args[0] == geo.URL_ROUTING
    assert r["waypoints"] == "-34.6037,-58.3816|-34.5631,-58.4566"  # routing va lat,lon
    assert r["mode"] == "drive"
    assert llamada_ruta.kwargs["timeout"] > 0


def test_no_agrega_argentina_dos_veces(app):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        geo.geocodificar("Florida 100, Buenos Aires, Argentina")
    assert mock_get.call_args.kwargs["params"]["text"] == "Florida 100, Buenos Aires, Argentina"


def test_costo_envio_redondea_a_pesos(app):
    assert geo.costo_envio(0) == 1500
    assert geo.costo_envio(2.345) == 2438  # 1500 + 938


def test_sin_key_avisa_y_no_hace_requests(app, guardia_red):
    app.config["GEOAPIFY_API_KEY"] = None
    assert geo.configurada() is False

    res = geo.cotizar("Av. Cabildo 1540, CABA")
    assert res["ok"] is False
    assert res["error"] == "Falta configurar GEOAPIFY_API_KEY en .env"
    guardia_red.assert_not_called()


def test_direccion_sin_resultados(app):
    with patch("app.services.geo.requests.get", return_value=respuesta(datos={"results": []})):
        res = geo.cotizar("asdfgh 99999")
    assert res["ok"] is False
    assert res["error"].startswith("No encontramos esa dirección")


def test_sin_camino_en_auto(app):
    def falso(url, params=None, timeout=None):
        return respuesta(datos=GEOCODE_OK if url == geo.URL_GEOCODING else {"features": []})

    with patch("app.services.geo.requests.get", side_effect=falso):
        res = geo.cotizar("Av. Cabildo 1540, CABA")
    assert res["ok"] is False
    assert "camino" in res["error"]


def test_aviso_si_la_confianza_es_baja(app):
    dudosa = {"results": [{**GEOCODE_OK["results"][0], "rank": {"confidence": 0.3}}]}

    def falso(url, params=None, timeout=None):
        return respuesta(datos=dudosa if url == geo.URL_GEOCODING else RUTA_OK)

    with patch("app.services.geo.requests.get", side_effect=falso):
        res = geo.cotizar("Cabildo")
    assert res["ok"] is True
    assert res["confianza"] == 0.3
    assert "no está seguro" in res["aviso"]


@pytest.mark.parametrize("status, esperado", [
    (401, "inválida"),
    (403, "inválida"),
    (429, "límite"),
    (500, "caído"),
    (503, "caído"),
    (400, "código 400"),
])
def test_errores_http_dan_mensajes_amigables(app, status, esperado):
    # Aunque Geoapify devuelva la key en su mensaje, nosotros no la mostramos
    cuerpo = {"statusCode": status, "message": f"Invalid apiKey {CLAVE}"}
    with patch("app.services.geo.requests.get", return_value=respuesta(status=status, datos=cuerpo)):
        res = geo.cotizar("Av. Cabildo 1540, CABA")
    assert res["ok"] is False
    assert esperado in res["error"]
    assert CLAVE not in json.dumps(res)


@pytest.mark.parametrize("excepcion, esperado", [
    (requests.Timeout, "tardó demasiado"),
    (requests.ConnectionError, "No hay conexión"),
    (requests.RequestException, "No se pudo consultar"),
])
def test_timeout_y_sin_conexion_no_filtran_la_key(app, excepcion, esperado):
    # Las excepciones reales de requests traen la URL completa, ¡con la key!
    url_con_key = f"https://api.geoapify.com/v1/geocode/search?text=x&apiKey={CLAVE}"
    error = excepcion(f"HTTPSConnectionPool(host='api.geoapify.com'): Max retries exceeded with url: {url_con_key}")

    with patch("app.services.geo.requests.get", side_effect=error):
        res = geo.cotizar("Av. Cabildo 1540, CABA")
        with pytest.raises(geo.ErrorGeo) as info:
            geo.geocodificar("Av. Cabildo 1540, CABA")

    assert res["ok"] is False
    assert esperado in res["error"]
    assert CLAVE not in json.dumps(res)
    assert CLAVE not in str(info.value)
    assert info.value.__suppress_context__  # `from None`: el traceback no arrastra la URL


def test_cache_evita_repetir_requests(app):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        primero = geo.cotizar("Av. Cabildo 1540, CABA")
        segundo = geo.cotizar("  av. cabildo   1540, caba ")  # misma dirección normalizada
        assert mock_get.call_count == 2  # 1 geocoding + 1 routing, la segunda vez sale de la cache
        assert primero == segundo

        geo.limpiar_cache()
        geo.cotizar("Av. Cabildo 1540, CABA")
        assert mock_get.call_count == 4


def test_mapa_png_usa_lonlat_y_pide_png(app):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        imagen = geo.mapa_png(-34.5631, -58.4566)
    assert imagen == PNG_FALSO

    p = mock_get.call_args.kwargs["params"]
    assert mock_get.call_args.args[0] == geo.URL_MAPA
    assert p["marker"] == (
        "lonlat:-58.3816,-34.6037;color:#00375e;size:medium"  # sucursal (azul)
        "|lonlat:-58.4566,-34.5631;color:#006c49;size:medium"  # domicilio (verde): OJO lon,lat
    )
    assert (p["style"], p["width"], p["height"], p["format"]) == ("osm-bright", 600, 300, "png")
    assert p["apiKey"] == CLAVE


def test_mapa_png_usa_cache_y_tiene_tope(app):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        assert geo.mapa_png(-34.5631, -58.4566) == geo.mapa_png(-34.5631, -58.4566) == PNG_FALSO
        assert mock_get.call_count == 1  # el mismo mapa no se vuelve a pedir a Geoapify

        with patch.object(geo, "MAPAS_EN_CACHE", 2):
            geo.mapa_png(-34.60, -58.40)
            geo.mapa_png(-34.61, -58.41)
        assert len(geo._cache_mapas) == 2  # no crece sin límite: se fue el más viejo
        assert (-34.5631, -58.4566, 600, 300) not in geo._cache_mapas


def test_direccion_con_caracteres_invalidos_no_rompe(app, guardia_red):
    # Un "\ud800" suelto llega bien por JSON, pero requests explota al codificarlo (no es RequestException)
    res = geo.cotizar("Cabildo \ud800 1540")
    assert res["ok"] is False
    assert "caracteres inválidos" in res["error"]
    guardia_red.assert_not_called()


def test_mapa_png_rechaza_respuesta_que_no_es_imagen(app):
    with patch("app.services.geo.requests.get", return_value=respuesta(datos={"error": "x"})):
        with pytest.raises(geo.ErrorGeo, match="imagen"):
            geo.mapa_png(-34.5631, -58.4566)


# ---------------------------------------------------------------------------
# 2. Rutas del Blueprint logistica
# ---------------------------------------------------------------------------

def test_api_cotizar_devuelve_json_con_mapa(logueado):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso):
        resp = logueado.post("/envios/api/cotizar", json={"direccion": "Av. Cabildo 1540, CABA"})

    assert resp.status_code == 200
    datos = resp.get_json()
    assert datos["ok"] is True
    assert (datos["km"], datos["minutos"], datos["costo"]) == (7.8, 18, 4620)
    assert datos["mapa_url"].startswith("/envios/mapa.png?")
    assert "lat=-34.5631" in datos["mapa_url"] and "lon=-58.4566" in datos["mapa_url"]
    assert CLAVE not in resp.get_data(as_text=True)


@pytest.mark.parametrize("cuerpo", [
    {},
    {"direccion": "   "},
    {"direccion": "x" * 201},
    ["no", "es", "un", "dict"],
])
def test_api_cotizar_valida_la_direccion(logueado, guardia_red, cuerpo):
    resp = logueado.post("/envios/api/cotizar", json=cuerpo)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    guardia_red.assert_not_called()


def test_api_cotizar_con_surrogate_suelto_no_da_500(logueado, guardia_red):
    resp = logueado.post("/envios/api/cotizar", data='{"direccion": "Cabildo \\ud800 1540"}',
                         content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    assert "caracteres inválidos" in resp.get_json()["error"]
    guardia_red.assert_not_called()


def test_api_cotizar_error_de_red_no_muestra_la_key(logueado):
    error = requests.ConnectionError(f"Max retries exceeded with url: /v1/geocode/search?apiKey={CLAVE}")
    with patch("app.services.geo.requests.get", side_effect=error):
        resp = logueado.post("/envios/api/cotizar", json={"direccion": "Av. Cabildo 1540, CABA"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    assert resp.get_json()["mapa_url"] is None
    assert CLAVE not in resp.get_data(as_text=True)


def test_rutas_geo_requieren_login(client):
    for resp in (
        client.post("/envios/api/cotizar", json={"direccion": "Av. Cabildo 1540"}),
        client.get("/envios/1/ruta"),
        client.get("/envios/mapa.png?lat=-34.6&lon=-58.4"),
    ):
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


def test_ruta_de_un_envio(logueado):
    envio = Envio.query.first()  # el seed trae 3 envíos
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        resp = logueado.get(f"/envios/{envio.id}/ruta")

    assert resp.status_code == 200
    datos = resp.get_json()
    assert datos["ok"] is True and datos["costo"] == 4620
    assert datos["mapa_url"].startswith("/envios/mapa.png?")
    assert envio.direccion in mock_get.call_args_list[0].kwargs["params"]["text"]
    assert CLAVE not in resp.get_data(as_text=True)

    no_existe = logueado.get("/envios/99999/ruta")
    assert no_existe.status_code == 404
    assert no_existe.get_json()["ok"] is False


def test_mapa_proxy_devuelve_png(logueado):
    with patch("app.services.geo.requests.get", side_effect=geoapify_falso) as mock_get:
        resp = logueado.get("/envios/mapa.png?lat=-34.5631&lon=-58.4566")
        otra = logueado.get("/envios/mapa.png?lat=-34.5631&lon=-58.4566")  # recarga / otro usuario
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == PNG_FALSO
    assert resp.headers["Cache-Control"] == "private, max-age=3600"
    assert otra.data == PNG_FALSO
    assert mock_get.call_count == 1  # la segunda sale de la cache del servidor: no gasta créditos


@pytest.mark.parametrize("query", [
    "",                         # sin parámetros
    "lat=-34.6",                # falta lon
    "lat=abc&lon=-58.4",        # no es número
    "lat=40.71&lon=-74.0",      # Nueva York: fuera de Argentina
    "lat=-34.6&lon=2.35",       # longitud de París
    "lat=nan&lon=nan",
    "lat=-34.6&lon=inf",
])
def test_mapa_proxy_valida_coordenadas(logueado, guardia_red, query):
    resp = logueado.get(f"/envios/mapa.png?{query}")
    assert resp.status_code == 400
    guardia_red.assert_not_called()  # no se gastó ningún crédito de Geoapify


def test_mapa_proxy_si_falla_devuelve_502_sin_romper(app, logueado):
    with patch("app.services.geo.requests.get", return_value=respuesta(status=500, datos={"message": CLAVE})):
        resp = logueado.get("/envios/mapa.png?lat=-34.5631&lon=-58.4566")
    assert resp.status_code == 502
    assert resp.mimetype == "image/svg+xml"
    assert "Mapa no disponible" in resp.get_data(as_text=True)
    assert CLAVE not in resp.get_data(as_text=True)

    app.config["GEOAPIFY_API_KEY"] = None
    assert logueado.get("/envios/mapa.png?lat=-34.5631&lon=-58.4566").status_code == 502


def test_pagina_envios_con_key(logueado, guardia_red):
    resp = logueado.get("/envios/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Probar dirección" in html and "Calcular envío" in html
    assert 'placeholder="Av. Cabildo 1540, CABA"' in html
    assert "Ver ruta" in html and "data-geo-auto" in html
    for envio in Envio.query.all():
        assert f"/envios/{envio.id}/ruta" in html
    assert "Powered by" in html and "Geoapify" in html and "OpenStreetMap</a> contributors" in html
    assert "Actualizar Estado" in html  # lo que ya hacía la página sigue estando
    assert CLAVE not in html
    guardia_red.assert_not_called()  # renderizar la página no consume créditos (todo es lazy por fetch)


def test_pagina_envios_sin_key(app, logueado):
    app.config["GEOAPIFY_API_KEY"] = None
    resp = logueado.get("/envios/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "GEOAPIFY_API_KEY" in html  # explica cómo configurarla
    assert "Calcular envío" not in html and "Ver ruta" not in html  # sin botones rotos
    assert "data-geo-caja" not in html
    assert "Actualizar Estado" in html
