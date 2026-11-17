"""LLM local pequeña para sugerencias de reposición.

Modelo por defecto: Qwen/Qwen2.5-0.5B-Instruct (Hugging Face, ~1 GB). Se descarga
la primera vez y después funciona sin internet.

Patrón singleton + lazy loading:
    - El modelo NO se carga al iniciar Flask (la app arranca rápido).
    - Se carga la primera vez que alguien pide una sugerencia, una sola vez,
      protegido con un lock por si llegan dos requests al mismo tiempo.
    - `transformers` / `torch` se importan dentro de la carga, así en Render
      (LLM_ENABLED=false) ni siquiera se importan.

Decisión de diseño: las cantidades y montos los calcula Python
(services/inventario.calcular_candidatos_reposicion). El modelo recibe las
viñetas exactas sin ejemplos de productos ficticios y redacta **una única oración
corta y precisa** (máximo 70 tokens). Un validador post-generación verifica que no
haya inventado números ni productos ajenos; si detecta desvíos, usa la oración
exacta derivada de los datos reales.
"""
import re
import threading
import time

from flask import current_app

FRASES_ECO_PROHIBIDAS = (
    "primero lo urgente",
    "agrupá los pedidos",
    "para cada producto usá",
    "si hay productos para promocionar",
    "terminá con una línea",
    "reglas del negocio",
    "un producto vencido no se puede",
)


def sistema_prompt(dias_alerta: int) -> str:
    return (
        "Sos el asistente de inventario de JUMBOX. "
        f"Reglas del negocio: productos vencidos se retiran como merma; si vence en {dias_alerta} días o menos se promociona; "
        "los pedidos ingresan al depósito y suman deuda al proveedor. "
        "Respondé en español rioplatense con UNA SOLA ORACIÓN CORTA (máximo 25 palabras) indicando qué hacer primero, "
        "copiando exactamente el nombre del producto y la cantidad informada. No inventes otros productos ni números."
    )


class AsistenteReposicion:
    _instancia = None
    _lock = threading.Lock()

    def __init__(self, modelo: str):
        from transformers import pipeline  # import diferido (pesado)

        inicio = time.time()
        print(f"[LLM] Cargando {modelo} (la primera vez se descarga)...")
        self.modelo = modelo
        self.generar = pipeline("text-generation", model=modelo, dtype="auto")
        self.segundos_carga = round(time.time() - inicio, 1)
        print(f"[LLM] Modelo listo en {self.segundos_carga}s")

    @classmethod
    def obtener(cls, modelo: str) -> "AsistenteReposicion":
        """Devuelve la única instancia, creándola si hace falta."""
        if cls._instancia is None:
            with cls._lock:
                if cls._instancia is None:  # doble verificación
                    cls._instancia = cls(modelo)
        return cls._instancia

    @classmethod
    def cargado(cls) -> bool:
        return cls._instancia is not None

    @classmethod
    def reiniciar(cls):
        """Descarga la instancia (lo usan los tests)."""
        with cls._lock:
            cls._instancia = None

    def redactar(self, alertas: str, dias_alerta: int, max_tokens: int) -> str:
        mensajes = [
            {"role": "system", "content": sistema_prompt(dias_alerta)},
            {"role": "user", "content": f"Situación actual (priorizada):\n{alertas}\n\nEscribí UNA sola oración corta con la acción más urgente:"},
        ]
        salida = self.generar(
            mensajes,
            max_new_tokens=min(max_tokens, 70),
            do_sample=False,
            repetition_penalty=1.15,
        )
        generado = salida[0]["generated_text"][-1]["content"].strip()
        return _limpiar_salida_llm(generado)


def _limpiar_salida_llm(texto: str) -> str:
    """Quita saltos extra, ecos del prompt o barras '|' si el modelo 0.5B intenta copiarlas."""
    lineas_limpias = []
    for linea in texto.splitlines():
        limpia = linea.strip(" •-*")
        if not limpia or "|" in limpia:
            continue
        if any(frase in limpia.lower() for frase in FRASES_ECO_PROHIBIDAS):
            continue
        lineas_limpias.append(limpia)
    unido = " ".join(lineas_limpias).strip()
    # Quedarse como máximo con las dos primeras oraciones para mantener precisión y brevedad
    oraciones = re.split(r"(?<=[.!?])\s+", unido)
    return " ".join(oraciones[:2]).strip()


def _es_respuesta_precisa(texto: str, candidatos: list[dict], contexto: str) -> bool:
    """Verifica que la respuesta del modelo chico no haya inventado números o ignorado los productos reales."""
    if not texto or len(texto) > 260:
        return False
    texto_low = texto.lower()
    # Debe mencionar al menos el producto más urgente
    if candidatos and candidatos[0]["nombre"].lower() not in texto_low:
        return False
    # Todo número mencionado por el modelo debe aparecer literalmente en los datos de entrada (evita alucinaciones)
    numeros_contexto = set(re.findall(r"\d+(?:[.,]\d+)?", contexto))
    numeros_salida = re.findall(r"\d+(?:[.,]\d+)?", texto)
    for num in numeros_salida:
        if num not in numeros_contexto:
            return False
    return True


def _dictamen_exacto(candidatos: list[dict]) -> str:
    urgente = candidatos[0]
    accion = urgente["accion"].lower()
    cant = f" ({urgente['cantidad_sugerida']} u.)" if urgente.get("cantidad_sugerida") else ""
    return f"Prioridad inmediata: {accion} «{urgente['nombre']}»{cant} ({urgente['motivo']})"


def puntos_operativos(candidatos: list[dict]) -> list[str]:
    """Construye las viñetas exactas de compras, traslados y vencimientos."""
    if not candidatos:
        return ["• El inventario está en orden: no hay productos bajo el mínimo ni por vencer."]

    lineas = []
    reponer: dict[str, list[str]] = {}
    montos: dict[str, float] = {}
    deudas: dict[str, float] = {}
    for c in candidatos:
        if c["accion"] == "Dar de baja":
            lineas.append(f"• URGENTE — {c['nombre']} está vencido: retirar {c['cantidad_sugerida']} u. y registrar merma.")
        elif c["accion"] == "Promocionar":
            lineas.append(f"• ATENCIÓN — {c['nombre']} vence en {c['dias_para_vencer']} días: promocionar en caja, no reponer.")
        elif c["accion"] == "Reponer":
            reponer.setdefault(c["proveedor"], []).append(f"{c['cantidad_sugerida']} u. de {c['nombre']}")
            montos[c["proveedor"]] = montos.get(c["proveedor"], 0.0) + c["cantidad_sugerida"] * c["precio_compra"]
            deudas[c["proveedor"]] = c["deuda_proveedor"]
        elif c["accion"] == "Trasladar":
            lineas.append(f"• TRASLADO — Pasar {c['cantidad_sugerida']} u. de {c['nombre']} del Depósito Central a Góndola.")

    for proveedor, productos in reponer.items():
        lineas.append(
            f"• PEDIDO A {proveedor.upper()} — Solicitar " + ", ".join(productos)
            + f" (unos ${montos[proveedor]:,.2f}; deuda actual con el proveedor: ${deudas[proveedor]:,.2f})."
        )
    return lineas


def resumen_por_reglas(candidatos: list[dict]) -> str:
    """Fallback sin modelo: arma las viñetas operativas + resumen final."""
    vinetas = puntos_operativos(candidatos)
    if not candidatos:
        return "\n".join(vinetas) + "\n\nResumen: no hace falta pedir ni dar de baja productos hoy."
    urgente = candidatos[0]
    return (
        "\n".join(vinetas)
        + f"\n\nResumen: la prioridad inmediata es «{urgente['nombre']}» ({urgente['accion'].lower()})."
    )


def estado() -> dict:
    cfg = current_app.config
    return {
        "habilitada": cfg["LLM_ENABLED"],
        "modelo": cfg["LLM_MODEL"],
        "cargada": AsistenteReposicion.cargado(),
    }


def generar_recomendacion(candidatos: list[dict]) -> dict:
    """Devuelve {"texto", "fuente" ("llm" | "reglas"), "modelo", "error", "segundos"}."""
    cfg = current_app.config
    resultado = {"texto": "", "fuente": "reglas", "modelo": cfg["LLM_MODEL"], "error": None, "segundos": None}

    if not candidatos or not cfg["LLM_ENABLED"]:
        resultado["texto"] = resumen_por_reglas(candidatos)
        return resultado

    vinetas_str = "\n".join(puntos_operativos(candidatos))
    try:
        inicio = time.time()
        asistente = AsistenteReposicion.obtener(cfg["LLM_MODEL"])
        dictamen = asistente.redactar(vinetas_str, cfg["DIAS_ALERTA_VENCIMIENTO"], cfg["LLM_MAX_TOKENS"])
        if not _es_respuesta_precisa(dictamen, candidatos, vinetas_str):
            dictamen = _dictamen_exacto(candidatos)
        resultado["texto"] = f"{vinetas_str}\n\nResumen IA ({cfg['LLM_MODEL']}):\n{dictamen}"
        resultado["fuente"] = "llm"
        resultado["segundos"] = round(time.time() - inicio, 1)
    except Exception as error:  # sin torch, sin RAM, sin internet la primera vez...
        current_app.logger.warning("LLM no disponible: %s", error)
        resultado["texto"] = resumen_por_reglas(candidatos)
        resultado["error"] = str(error)
    return resultado
