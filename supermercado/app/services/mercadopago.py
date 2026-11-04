"""Integración con Mercado Pago: Checkout Pro vía Orders API, usando solo `requests`.

Flujo del cobro (todo pasa en el servidor; el Access Token nunca llega al navegador):
    1. El cajero registra la venta con medio de pago "Mercado Pago" y se abre el ticket.
    2. Toca "Pagar con Mercado Pago": crear_cobro() hace POST /v1/orders y Mercado Pago
       devuelve un `checkout_url`. Redirigimos al cliente a esa página de mercadopago.com.ar.
    3. El cliente paga ahí con una tarjeta de prueba (titular APRO = aprobado).
    4. Mercado Pago lo devuelve a /pos/mp/retorno (solo si la app corre en https, ej. Render)
       o el cajero toca "Verificar pago". En los dos casos consultar_cobro() hace
       GET /v1/orders/{id}. NUNCA confiamos en el ?status= de la URL de regreso,
       porque cualquiera lo puede editar a mano.

Credenciales: se usa el Access Token de PRUEBA (empieza con APP_USR-), así no se mueve
plata real. Va en MP_ACCESS_TOKEN dentro del .env (y en las variables de Render).

Si mañana hay que pasar a Checkout Pro vía Preferences, solo cambia este archivo:
las rutas llaman a crear_cobro() / consultar_cobro() y no saben qué endpoint hay detrás.
"""
import uuid

import requests
from flask import current_app

API = "https://api.mercadopago.com"
TIMEOUT = 15  # segundos: si Mercado Pago no contesta, cortamos y avisamos

# Colores del badge (paleta de AGENTS.md)
VERDE = "#006c49"
NARANJA = "#6c4300"
ROJO = "#ba1a1a"
AZUL = "#00375e"


class ErrorMP(Exception):
    """Error al hablar con Mercado Pago.

    `.mensaje` está pensado para mostrarse tal cual al cajero (flash): nunca incluye
    el token ni la URL del request. `.codigo` es el status HTTP (None si fue un error de red).
    """

    def __init__(self, mensaje: str, codigo: int | None = None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.codigo = codigo


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def _token() -> str | None:
    return current_app.config.get("MP_ACCESS_TOKEN") or None


def configurado() -> bool:
    """True si hay un Access Token cargado en la configuración."""
    return bool(_token())


def es_token_de_prueba() -> bool:
    """Chequeo de formato del token: los de Mercado Pago empiezan con APP_USR- (o TEST- en apps viejas).

    OJO: el de producción también empieza con APP_USR-, así que esto no los distingue.
    Lo que garantiza que sea de prueba es copiarlo del selector "Prueba" del panel.
    """
    token = _token() or ""
    return token.startswith(("APP_USR-", "TEST-"))


def _headers(nuevo_cobro: bool = False) -> dict:
    """Headers de cada request. Si no hay token, cortamos ANTES de llamar a la API."""
    token = _token()
    if not token:
        raise ErrorMP("Mercado Pago no está configurado (falta MP_ACCESS_TOKEN en el .env).")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if nuevo_cobro:
        # Obligatorio en POST /v1/orders. Uno nuevo por intento: si se repite, MP responde 409.
        headers["X-Idempotency-Key"] = str(uuid.uuid4())
    return headers


# ---------------------------------------------------------------------------
# Manejo de errores (mensajes propios, sin datos sensibles)
# ---------------------------------------------------------------------------

def _enviar(funcion, url: str, **kwargs) -> requests.Response:
    """Hace el request (requests.post o requests.get) y traduce los errores de red.

    No mostramos str(exc): el texto de las excepciones de requests trae la URL completa.
    """
    try:
        return funcion(url, timeout=TIMEOUT, **kwargs)
    except requests.Timeout:
        raise ErrorMP("Mercado Pago tardó demasiado en responder. Probá de nuevo en unos segundos.") from None
    except requests.ConnectionError:
        raise ErrorMP("No hay conexión con Mercado Pago. Revisá la conexión a internet del servidor.") from None
    except requests.RequestException:
        raise ErrorMP("No se pudo comunicar con Mercado Pago. Probá de nuevo.") from None


def _leer_json(resp: requests.Response) -> dict:
    try:
        datos = resp.json()
    except ValueError:
        datos = None
    return datos if isinstance(datos, dict) else {}


def _resumen_error(resp: requests.Response) -> str:
    """Resume el error que manda MP (solo los campos `code` y `message`, recortados)."""
    datos = _leer_json(resp)
    partes = []
    for error in (datos.get("errors") or [])[:2]:  # formato de Orders API
        if isinstance(error, dict):
            partes.append(str(error.get("message") or error.get("code") or ""))
    if not partes and datos.get("message"):  # formato de las APIs viejas
        partes.append(str(datos["message"]))
    resumen = " / ".join(p for p in partes if p)
    return resumen[:200] or "sin detalle"


def _error_http(resp: requests.Response) -> ErrorMP:
    """Traduce el status HTTP de Mercado Pago a un mensaje entendible para el cajero."""
    codigo = resp.status_code
    if codigo == 400:
        mensaje = f"Mercado Pago rechazó los datos del cobro (400): {_resumen_error(resp)}."
    elif codigo in (401, 403):
        mensaje = (f"Mercado Pago rechazó las credenciales ({codigo}). Usá el Access Token de PRUEBA "
                   "(empieza con APP_USR-) de una aplicación nueva de Checkout Pro.")
    elif codigo == 404:
        mensaje = "Mercado Pago no encontró ese cobro (404)."
    elif codigo == 409:
        mensaje = "Mercado Pago detectó un cobro duplicado (409). Volvé a tocar «Pagar con Mercado Pago»."
    elif codigo == 429:
        mensaje = "Hicimos demasiados pedidos a Mercado Pago (429). Esperá un minuto y probá de nuevo."
    elif codigo >= 500:
        mensaje = (f"Mercado Pago está con problemas en este momento ({codigo}). "
                   "Probá en unos minutos o cobrá con otro medio de pago.")
    else:
        mensaje = f"Mercado Pago respondió con un error inesperado ({codigo})."
    return ErrorMP(mensaje, codigo)


# ---------------------------------------------------------------------------
# Cobros
# ---------------------------------------------------------------------------

def crear_cobro(venta, url_retorno: str | None = None) -> dict:
    """Crea la order de Checkout Pro por el total de la venta.

    Devuelve {"id": order_id, "checkout_url": link de pago, "estado": "created"}.
    `url_retorno` solo se manda si es https (Render): MP no acepta localhost como back URL.
    """
    if not venta.total or venta.total <= 0:
        raise ErrorMP("La venta no tiene un monto para cobrar.")

    # Montos como STRING con 2 decimales. Un solo ítem por el total del ticket:
    # así total_amount siempre coincide con la suma de los ítems (si no, MP da 400).
    total = f"{venta.total:.2f}"
    body = {
        "type": "online",
        "processing_mode": "manual",
        "total_amount": total,
        "external_reference": f"JBX-{venta.id}",  # con esto se puede buscar la venta al volver
        "description": f"Jumbox - Ticket #{venta.id}",
        "items": [{"title": f"Compra Jumbox ticket #{venta.id}", "quantity": 1, "unit_price": total}],
    }
    # NO mandamos allowed_user_type: bloquearía el pago como invitado (sin cuenta de MP).
    if url_retorno and url_retorno.startswith("https://"):
        body["config"] = {
            "online": {
                "success_url": url_retorno,
                "failure_url": url_retorno,
                "pending_url": url_retorno,
                "auto_return": "approved",  # si se aprueba, MP vuelve solo a Jumbox
            }
        }

    headers = _headers(nuevo_cobro=True)  # sin token, corta acá y no se hace el request
    resp = _enviar(requests.post, f"{API}/v1/orders", json=body, headers=headers)
    if resp.status_code not in (200, 201):
        raise _error_http(resp)

    datos = _leer_json(resp)
    checkout_url = str(datos.get("checkout_url") or "")
    if not datos.get("id") or not checkout_url.startswith("https://"):
        raise ErrorMP("Mercado Pago no devolvió el link de pago. Probá de nuevo.")
    return {"id": str(datos["id"]), "checkout_url": checkout_url, "estado": datos.get("status") or "created"}


def _ultimo_pago(datos: dict) -> dict:
    """Último intento de pago dentro de la order (transactions.payments[-1]), o {} si no hay."""
    transacciones = datos.get("transactions")
    pagos = transacciones.get("payments") if isinstance(transacciones, dict) else None
    if isinstance(pagos, list) and pagos and isinstance(pagos[-1], dict):
        return pagos[-1]
    return {}


def consultar_cobro(order_id: str) -> dict:
    """Pregunta a Mercado Pago cómo está la order (la fuente de verdad del pago).

    Devuelve {"estado", "detalle", "pagado", "monto"}. Estados posibles de una order:
    created, processing, action_required, processed (pagada), failed, canceled, refunded, expired.

    Caso especial "rejected" (estado propio de Jumbox): si el cliente pagó con una tarjeta
    rechazada (titular OTHE en las pruebas), Mercado Pago NO cierra la order: la deja en
    action_required / waiting_retry para que reintente en el MISMO checkout, y el rechazo
    queda en el último pago (status "failed", status_detail "rejected_by_issuer").
    Lo informamos como "rejected" para que el ticket diga "Rechazado"; como no es un estado
    final, "Pagar con Mercado Pago" vuelve a abrir ese mismo checkout para reintentar.
    """
    resp = _enviar(requests.get, f"{API}/v1/orders/{order_id}", headers=_headers())
    if resp.status_code != 200:
        raise _error_http(resp)

    datos = _leer_json(resp)
    estado = str(datos.get("status") or "desconocido")
    detalle = datos.get("status_detail")
    ultimo_pago = _ultimo_pago(datos)
    if estado not in ("processed", "failed", "canceled", "expired", "refunded") and ultimo_pago.get("status") == "failed":
        estado = "rejected"
        detalle = ultimo_pago.get("status_detail") or detalle
    return {
        "estado": estado,
        "detalle": detalle,
        "pagado": estado == "processed",
        "monto": datos.get("total_paid_amount"),
    }


# status_detail de Mercado Pago (viene en inglés) traducido para el cajero
DETALLES = {
    "accredited": "acreditado",
    "created": "esperando que el cliente pague",
    "waiting_payment": "esperando que el cliente pague",
    "in_process": "en revisión",
    "in_review": "en revisión",
    "rejected_by_issuer": "lo rechazó el banco",
    "insufficient_amount": "fondos insuficientes",
    "high_risk": "rechazado por prevención de fraude",
    "bad_filled_card_data": "datos de la tarjeta incorrectos",
    "card_disabled": "tarjeta deshabilitada",
    "max_attempts_exceeded": "demasiados intentos",
}


def detalle_legible(detalle: str | None) -> str | None:
    """status_detail en castellano; None si no lo conocemos (mejor nada que el código crudo)."""
    return DETALLES.get(detalle) if detalle else None


def estado_legible(estado: str | None, detalle: str | None = None) -> tuple[str, str]:
    """Texto y color del badge del ticket a partir del estado de la order."""
    if not estado:
        return "Sin cobrar", AZUL  # todavía no se tocó "Pagar con Mercado Pago"
    if estado in ("failed", "rejected"):  # rejected = tarjeta rechazada, se puede reintentar
        return "Rechazado", ROJO
    if estado in ("canceled", "expired"):
        return "Cancelado", ROJO
    if estado == "refunded":
        return "Devuelto", ROJO
    if estado == "processed" or detalle == "accredited":
        return "Pagado", VERDE
    return "Pendiente", NARANJA  # created, processing, action_required, etc.
