"""Integración con Geoapify (API externa REST con API key) para los envíos a domicilio.

Usamos 3 endpoints de Geoapify, todos con requests.get:
    1. Geocoding  -> convierte "Av. Cabildo 1540, CABA" en coordenadas (lat, lon).
    2. Routing    -> distancia (km) y tiempo (min) en auto desde la sucursal hasta el domicilio.
    3. Static Map -> imagen PNG del mapa con dos marcadores (sucursal y cliente).

Seguridad:
    - La key (GEOAPIFY_API_KEY en .env) viaja en el query string (?apiKey=...), así que
      vive SOLO en el servidor. El navegador nunca habla directo con Geoapify: el mapa
      pasa por una ruta de Flask que hace de proxy (logistica.mapa).
    - Las excepciones de requests traen la URL completa (¡con la key!) en su texto.
      Por eso NUNCA mostramos str(exc): armamos mensajes propios con ErrorGeo.

Créditos: el plan Free da 3.000 por día. Para no gastarlos cada vez que alguien
recarga la página de Envíos guardamos en memoria (un dict) las direcciones, rutas
y mapas ya calculados.

OJO con el orden de las coordenadas (el bug típico):
    - Routing usa "lat,lon".
    - Static Maps y el bias del Geocoding usan "lon,lat".
"""
import requests
from flask import current_app

URL_GEOCODING = "https://api.geoapify.com/v1/geocode/search"
URL_ROUTING = "https://api.geoapify.com/v1/routing"
URL_MAPA = "https://maps.geoapify.com/v1/staticmap"

TIMEOUT = 8  # segundos: si Geoapify tarda más, cortamos y avisamos (la página no se cuelga)
CONFIANZA_MINIMA = 0.5  # por debajo de esto avisamos que la dirección puede estar mal ubicada
MAPAS_EN_CACHE = 100  # cada PNG pesa ~100 KB: como mucho ~10 MB de memoria

# Cache en memoria: se pierde al reiniciar el servidor (alcanza para la demo)
_cache_direcciones = {}  # "av. cabildo 1540, caba" -> {"lat", "lon", "direccion_formateada", "confianza"}
_cache_rutas = {}  # (lat, lon) redondeados -> {"km", "minutos"}
_cache_mapas = {}  # (lat, lon, ancho, alto) -> bytes del PNG


class ErrorGeo(Exception):
    """Error de Geoapify con un mensaje amigable (y sin la key) para mostrar en pantalla."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def configurada() -> bool:
    """True si hay una GEOAPIFY_API_KEY cargada en la configuración."""
    return bool(current_app.config.get("GEOAPIFY_API_KEY"))


def limpiar_cache() -> None:
    """Olvida las direcciones, rutas y mapas guardados (lo usan los tests)."""
    _cache_direcciones.clear()
    _cache_rutas.clear()
    _cache_mapas.clear()


def _consultar(url: str, params: dict) -> requests.Response:
    """Hace el GET a Geoapify agregando la key y traduce cualquier falla a un ErrorGeo.

    `from None` corta la cadena de excepciones: así la excepción original de requests
    (que tiene la URL con la key) no aparece ni en pantalla ni en el traceback.
    """
    if not configurada():
        raise ErrorGeo("Falta configurar GEOAPIFY_API_KEY en .env")

    params = {**params, "apiKey": current_app.config["GEOAPIFY_API_KEY"]}
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.Timeout:
        raise ErrorGeo("Geoapify tardó demasiado en responder. Probá de nuevo en unos segundos.") from None
    except requests.ConnectionError:
        raise ErrorGeo("No hay conexión con Geoapify. Revisá la conexión a internet del servidor.") from None
    except requests.RequestException:
        raise ErrorGeo("No se pudo consultar Geoapify.") from None

    if resp.status_code in (401, 403):
        raise ErrorGeo("La API key de Geoapify es inválida o no tiene permiso para este servicio (revisá GEOAPIFY_API_KEY).")
    if resp.status_code == 429:
        raise ErrorGeo("Se alcanzó el límite de consultas de Geoapify (plan Free: 3.000 por día y 5 por segundo). Probá más tarde.")
    if resp.status_code >= 500:
        raise ErrorGeo("El servicio de Geoapify está caído en este momento. Probá más tarde.")
    if resp.status_code != 200:
        raise ErrorGeo(f"Geoapify rechazó la consulta (código {resp.status_code}).")
    return resp


def _leer_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        raise ErrorGeo("Geoapify devolvió una respuesta inesperada.") from None


def _normalizar(direccion: str) -> str:
    """'  Av. Cabildo   1540 ' y 'av. cabildo 1540' son la misma dirección para la cache."""
    return " ".join(direccion.lower().split())


def geocodificar(direccion: str) -> dict:
    """Dirección escrita -> {"lat", "lon", "direccion_formateada", "confianza" (0 a 1)}."""
    direccion = (direccion or "").strip()
    if not direccion:
        raise ErrorGeo("Escribí una dirección para calcular el envío.")
    try:
        direccion.encode("utf-8")  # un "\ud800" suelto (llega por JSON) haría explotar a requests
    except UnicodeEncodeError:
        raise ErrorGeo("La dirección tiene caracteres inválidos.") from None

    clave = _normalizar(direccion)
    if clave in _cache_direcciones:
        return _cache_direcciones[clave]

    # "CABA" sola puede caer en cualquier lado: completamos la ciudad y el país
    texto = direccion if "argentina" in clave else f"{direccion}, Buenos Aires, Argentina"
    cfg = current_app.config
    datos = _leer_json(_consultar(URL_GEOCODING, {
        "text": texto,
        "filter": "countrycode:ar",  # solo resultados de Argentina
        "bias": f"proximity:{cfg['SUCURSAL_LON']},{cfg['SUCURSAL_LAT']}",  # preferir lo cercano a la sucursal (lon,lat)
        "lang": "es",
        "format": "json",
        "limit": 1,
    }))

    resultados = datos.get("results") or []
    if not resultados:
        raise ErrorGeo("No encontramos esa dirección. Probá con calle, altura y barrio (ej. «Av. Cabildo 1540, CABA»).")

    try:
        primero = resultados[0]
        lugar = {
            "lat": float(primero["lat"]),
            "lon": float(primero["lon"]),
            "direccion_formateada": primero.get("formatted") or direccion,
            "confianza": float((primero.get("rank") or {}).get("confidence") or 0),
        }
    except (KeyError, TypeError, ValueError):
        raise ErrorGeo("Geoapify devolvió una respuesta inesperada.") from None

    _cache_direcciones[clave] = lugar
    return lugar


def ruta(lat: float, lon: float) -> dict:
    """Ruta en auto sucursal -> domicilio: {"km": 7.8, "minutos": 18}."""
    clave = (round(lat, 5), round(lon, 5))  # ~1 metro de precisión
    if clave in _cache_rutas:
        return _cache_rutas[clave]

    cfg = current_app.config
    datos = _leer_json(_consultar(URL_ROUTING, {
        # Routing usa "lat,lon" y separa los puntos con "|"
        "waypoints": f"{cfg['SUCURSAL_LAT']},{cfg['SUCURSAL_LON']}|{lat},{lon}",
        "mode": "drive",
    }))

    try:
        propiedades = datos["features"][0]["properties"]
        trayecto = {
            "km": round(propiedades["distance"] / 1000, 1),  # viene en metros
            "minutos": int(round(propiedades["time"] / 60)),  # viene en segundos
        }
    except (KeyError, IndexError, TypeError):
        raise ErrorGeo("Geoapify no encontró un camino en auto hasta esa dirección.") from None

    _cache_rutas[clave] = trayecto
    return trayecto


def costo_envio(km: float) -> float:
    """Costo estimado = costo fijo + $ por km (se configuran en .env), redondeado a pesos enteros."""
    cfg = current_app.config
    return float(round(cfg["ENVIO_COSTO_BASE"] + km * cfg["ENVIO_COSTO_KM"]))


def cotizar(direccion: str) -> dict:
    """Todo junto para la pantalla: ubica la dirección, calcula la ruta y el costo.

    Nunca lanza excepciones: si algo falla devuelve ok=False y un `error` amigable,
    así la página de Envíos sigue funcionando aunque Geoapify no responda.
    """
    resultado = {
        "ok": False, "direccion_formateada": None, "lat": None, "lon": None, "km": None,
        "minutos": None, "costo": None, "confianza": None, "aviso": None, "error": None,
    }
    try:
        lugar = geocodificar(direccion)
        trayecto = ruta(lugar["lat"], lugar["lon"])
    except ErrorGeo as err:
        resultado["error"] = err.mensaje
        return resultado
    except requests.RequestException:  # red de seguridad: nunca mostramos str(exc) (trae la key)
        resultado["error"] = "No se pudo consultar Geoapify."
        return resultado

    resultado.update(lugar)
    resultado.update(trayecto)
    resultado["costo"] = costo_envio(trayecto["km"])
    resultado["ok"] = True
    if lugar["confianza"] < CONFIANZA_MINIMA:
        resultado["aviso"] = "Geoapify no está seguro de esta dirección: revisá en el mapa que el marcador verde esté en el lugar correcto."
    return resultado


def mapa_png(lat: float, lon: float, ancho: int = 600, alto: int = 300) -> bytes:
    """Imagen PNG del mapa con la sucursal (azul) y el domicilio (verde).

    Static Maps usa "lonlat:lon,lat" (al revés que Routing). Sin center ni zoom,
    Geoapify encuadra solo el mapa para que entren los dos marcadores.
    """
    clave = (round(lat, 5), round(lon, 5), ancho, alto)
    if clave in _cache_mapas:  # el mismo mapa pedido de nuevo no gasta créditos
        return _cache_mapas[clave]

    cfg = current_app.config
    marcadores = (
        f"lonlat:{cfg['SUCURSAL_LON']},{cfg['SUCURSAL_LAT']};color:#00375e;size:medium"
        f"|lonlat:{lon},{lat};color:#006c49;size:medium"
    )
    resp = _consultar(URL_MAPA, {
        "style": "osm-bright",
        "width": ancho,
        "height": alto,
        "format": "png",  # por defecto Geoapify devuelve jpeg
        "marker": marcadores,  # requests codifica "#" como %23
    })
    if not resp.headers.get("content-type", "").startswith("image/"):
        raise ErrorGeo("Geoapify no devolvió una imagen de mapa.")

    if len(_cache_mapas) >= MAPAS_EN_CACHE:
        _cache_mapas.pop(next(iter(_cache_mapas)))  # sacamos el más viejo (los dict guardan el orden)
    _cache_mapas[clave] = resp.content
    return resp.content
