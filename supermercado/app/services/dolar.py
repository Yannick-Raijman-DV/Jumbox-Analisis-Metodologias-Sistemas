"""Integración con DolarAPI (API externa pública REST, sin API key) para el cambio del dólar en el POS.

Endpoint que usamos:
    GET https://dolarapi.com/v1/dolares/{tipo}   (tipo: oficial, blue, tarjeta, mep, ...)
    -> {"moneda": "USD", "casa": "oficial", "nombre": "Oficial", "compra": 1000,
        "venta": 1050, "fechaActualizacion": "2026-11-16T14:30:00.000Z"}

Cómo se usa en Jumbox:
    - El POS muestra la cotización vigente y el total del ticket convertido a dólares.
    - Al confirmar la venta se GUARDA la cotización usada (modelo VentaCotizacion): así el
      ticket de hoy sigue mostrando el mismo cambio aunque mañana el dólar se mueva.
    - Cobramos en pesos; el dólar es el equivalente de referencia. Para convertir pesos -> USD
      se usa el valor de VENTA (lo que cuesta comprar un dólar).

Resiliencia: si DolarAPI no responde, la venta NO se frena. Los errores se traducen a ErrorDolar
con un mensaje amigable y, si hay una cotización vieja en memoria, se usa marcada como "desactualizada".
La cotización se cachea unos minutos (DOLAR_CACHE_SEGUNDOS) para no llamar a la API en cada venta.
"""
import time

import requests
from flask import current_app

URL = "https://dolarapi.com/v1/dolares/{tipo}"
TIMEOUT = 6  # segundos: el cajero no puede quedarse esperando
TIPOS_VALIDOS = ("oficial", "blue", "bolsa", "contadoconliqui", "mayorista", "cripto", "tarjeta")

_cache: dict[str, dict] = {}  # tipo -> {"cotizacion": {...}, "ts": epoch}


class ErrorDolar(Exception):
    """Falla de DolarAPI con un mensaje amigable (apto para mostrar en pantalla)."""

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def limpiar_cache() -> None:
    _cache.clear()


def tipo_configurado() -> str:
    tipo = (current_app.config.get("DOLAR_TIPO") or "oficial").strip().lower()
    return tipo if tipo in TIPOS_VALIDOS else "oficial"


def _consultar(tipo: str) -> dict:
    try:
        resp = requests.get(URL.format(tipo=tipo), timeout=TIMEOUT, headers={"Accept": "application/json"})
    except requests.Timeout:
        raise ErrorDolar("DolarAPI tardó demasiado en responder.") from None
    except requests.ConnectionError:
        raise ErrorDolar("No hay conexión con DolarAPI.") from None
    except requests.RequestException:
        raise ErrorDolar("No se pudo consultar DolarAPI.") from None

    if resp.status_code == 404:
        raise ErrorDolar(f"DolarAPI no conoce la cotización «{tipo}».")
    if resp.status_code == 429:
        raise ErrorDolar("Se alcanzó el límite de consultas de DolarAPI. Probá en un minuto.")
    if resp.status_code != 200:
        raise ErrorDolar(f"DolarAPI respondió con error (código {resp.status_code}).")

    try:
        datos = resp.json()
        venta = float(datos["venta"])
        compra = float(datos.get("compra") or venta)
    except (ValueError, KeyError, TypeError):
        raise ErrorDolar("DolarAPI devolvió una respuesta inesperada.") from None
    if venta <= 0:
        raise ErrorDolar("DolarAPI devolvió una cotización inválida.")

    return {
        "tipo": tipo,
        "nombre": datos.get("nombre") or tipo.capitalize(),
        "compra": compra,
        "venta": venta,
        "actualizada": datos.get("fechaActualizacion"),
        "desactualizada": False,
    }


def cotizacion(tipo: str | None = None) -> dict:
    """Cotización vigente {"tipo","nombre","compra","venta","actualizada","desactualizada"}.

    Usa la cache si es reciente. Si la API falla y hay una cotización vieja, la devuelve
    con desactualizada=True; si no hay ninguna, lanza ErrorDolar.
    """
    tipo = tipo or tipo_configurado()
    ttl = current_app.config.get("DOLAR_CACHE_SEGUNDOS")
    ttl = 300 if ttl is None else int(ttl)  # 0 es válido: siempre reconsulta
    guardada = _cache.get(tipo)
    if guardada and time.time() - guardada["ts"] < ttl:
        return guardada["cotizacion"]

    try:
        nueva = _consultar(tipo)
    except ErrorDolar:
        if guardada:
            return {**guardada["cotizacion"], "desactualizada": True}
        raise
    _cache[tipo] = {"cotizacion": nueva, "ts": time.time()}
    return nueva


def pesos_a_usd(pesos: float, venta: float) -> float:
    """Convierte pesos a dólares con el valor de venta, redondeado a centavos."""
    return round(float(pesos) / float(venta), 2)


def cotizacion_o_none(tipo: str | None = None) -> dict | None:
    """Como cotizacion() pero nunca lanza: devuelve None si no hay cotización disponible."""
    try:
        return cotizacion(tipo)
    except ErrorDolar:
        return None
