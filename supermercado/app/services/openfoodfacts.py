"""Integración con Open Food Facts (API externa pública REST, sin API key): catálogo abierto de productos.

Dado el código de barras (EAN-13) de un producto —los argentinos empiezan con 779— devuelve:
    - nombre y presentación,
    - marca y fabricante / lugar de elaboración  -> sugiere el PROVEEDOR,
    - foto de frente del producto                -> se puede guardar en S3 como foto del producto.

Endpoint:
    GET https://world.openfoodfacts.org/api/v2/product/{codigo}.json?fields=...
    -> {"status": 1, "product": {"product_name", "brands", "manufacturing_places",
        "quantity", "image_front_url", ...}}   (status 0 = no existe en la base)

Open Food Facts pide identificarse con un User-Agent propio (OFF_USER_AGENT en config).
La base es colaborativa: algunos productos no están, o no tienen foto. Siempre se avisa.

Seguridad: la foto se descarga SOLO desde el dominio de imágenes de Open Food Facts (anti-SSRF)
y con tope de tamaño, porque la URL viene de un servicio externo.
"""
import io
import re
import unicodedata
from urllib.parse import urlparse

import requests
from flask import current_app

URL = "https://world.openfoodfacts.org/api/v2/product/{codigo}.json"
CAMPOS = "product_name,product_name_es,brands,manufacturing_places,quantity,image_front_url,image_front_small_url"
TIMEOUT = 8
DOMINIOS_IMAGEN = ("openfoodfacts.org",)  # incluye subdominios (images.openfoodfacts.org)
MAX_BYTES_IMAGEN = 3 * 1024 * 1024
CACHE_MAX = 200

_cache: dict[str, dict] = {}


class ErrorOFF(Exception):
    """Falla de Open Food Facts con un mensaje amigable."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def limpiar_cache() -> None:
    _cache.clear()


def codigo_valido(codigo: str) -> bool:
    """Código de barras: solo dígitos, entre 8 y 14 (EAN-8, UPC, EAN-13, GTIN-14)."""
    return bool(re.fullmatch(r"\d{8,14}", codigo or ""))


def _primero(texto: str | None) -> str | None:
    """'Arcor, Bagley' -> 'Arcor' (los campos de OFF vienen separados por coma)."""
    if not texto:
        return None
    parte = texto.split(",")[0].strip()
    return parte or None


def buscar_por_codigo(codigo: str) -> dict:
    """Devuelve {"codigo","nombre","marca","fabricante","cantidad","imagen_url"} o lanza ErrorOFF."""
    codigo = (codigo or "").strip()
    if not codigo_valido(codigo):
        raise ErrorOFF("El código de barras debe tener entre 8 y 14 dígitos.")
    if codigo in _cache:
        return _cache[codigo]

    try:
        resp = requests.get(
            URL.format(codigo=codigo),
            params={"fields": CAMPOS},
            headers={"User-Agent": current_app.config.get("OFF_USER_AGENT") or "Jumbox/1.0 (proyecto academico)"},
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        raise ErrorOFF("Open Food Facts tardó demasiado en responder.") from None
    except requests.ConnectionError:
        raise ErrorOFF("No hay conexión con Open Food Facts.") from None
    except requests.RequestException:
        raise ErrorOFF("No se pudo consultar Open Food Facts.") from None

    if resp.status_code == 404:
        raise ErrorOFF("Open Food Facts no tiene ese producto. Cargalo a mano.")
    if resp.status_code == 429:
        raise ErrorOFF("Open Food Facts limitó las consultas. Probá en un minuto.")
    if resp.status_code != 200:
        raise ErrorOFF(f"Open Food Facts respondió con error (código {resp.status_code}).")

    try:
        datos = resp.json()
    except ValueError:
        raise ErrorOFF("Open Food Facts devolvió una respuesta inesperada.") from None
    if not isinstance(datos, dict) or datos.get("status") != 1 or not isinstance(datos.get("product"), dict):
        raise ErrorOFF("Open Food Facts no tiene ese producto. Cargalo a mano.")

    p = datos["product"]
    resultado = {
        "codigo": codigo,
        "nombre": (p.get("product_name_es") or p.get("product_name") or "").strip() or None,
        "marca": _primero(p.get("brands")),
        "fabricante": _primero(p.get("manufacturing_places")),
        "cantidad": (p.get("quantity") or "").strip() or None,
        "imagen_url": p.get("image_front_url") or p.get("image_front_small_url") or None,
    }
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[codigo] = resultado
    return resultado


def _simplificar(texto: str | None) -> str:
    """'Coca-Cola' y 'CocaCola Company' -> solo letras y números en minúscula, sin tildes."""
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", base.lower())


def sugerir_proveedor(marca: str | None, proveedores) -> object | None:
    """Busca un proveedor cuyo nombre coincida con la marca (ignora mayúsculas, guiones, espacios y tildes)."""
    m = _simplificar(marca)
    if not m:
        return None
    for prov in proveedores:
        n = _simplificar(prov.nombre)
        if n and (n == m or n in m or m in n):
            return prov
    return None


def _imagen_permitida(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == d or host.endswith("." + d) for d in DOMINIOS_IMAGEN)


def descargar_imagen(url: str) -> tuple[io.BytesIO, str]:
    """Baja la foto del producto a memoria. Devuelve (archivo, extensión). Lanza ErrorOFF si no sirve."""
    if not _imagen_permitida(url or ""):
        raise ErrorOFF("La imagen no viene de Open Food Facts.")
    try:
        resp = requests.get(url, timeout=TIMEOUT, stream=True,
                            headers={"User-Agent": current_app.config.get("OFF_USER_AGENT") or "Jumbox/1.0"})
    except requests.RequestException:
        raise ErrorOFF("No se pudo descargar la foto del producto.") from None
    if resp.status_code != 200:
        raise ErrorOFF("No se pudo descargar la foto del producto.")
    tipo = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(tipo)
    if ext is None:
        raise ErrorOFF("La foto del producto tiene un formato no soportado.")
    contenido = resp.content
    if len(contenido) > MAX_BYTES_IMAGEN:
        raise ErrorOFF("La foto del producto es demasiado pesada.")
    return io.BytesIO(contenido), ext
