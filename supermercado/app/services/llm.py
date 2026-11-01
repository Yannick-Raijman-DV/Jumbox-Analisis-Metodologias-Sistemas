"""LLM local pequeña para sugerencias de reposición.

Modelo por defecto: Qwen/Qwen2.5-0.5B-Instruct (Hugging Face, ~1 GB). Se descarga
la primera vez y después funciona sin internet.

Patrón singleton + lazy loading:
    - El modelo NO se carga al iniciar Flask (la app arranca rápido).
    - Se carga la primera vez que alguien pide una sugerencia, una sola vez,
      protegido con un lock por si llegan dos requests al mismo tiempo.
    - `transformers` / `torch` se importan dentro de la carga, así en Render
      (LLM_ENABLED=false) ni siquiera se importan.

Decisión de diseño: las cantidades las calcula Python
(services/inventario.calcular_candidatos_reposicion). El modelo solo redacta
un resumen priorizado en español a partir de esos datos, para que un modelo de
0.5B parámetros no invente números.
"""
import threading
import time

from flask import current_app

INSTRUCCIONES = """Sos el asistente de compras del supermercado JUMBOX.
Recibís una tabla con productos que necesitan atención, ya analizada por el sistema.
Escribí en español rioplatense, en 4 a 6 viñetas cortas:
- Primero lo urgente (prioridad alta), después lo demás.
- Agrupá los pedidos por proveedor cuando se pueda.
- Para cada producto usá EXACTAMENTE la acción y la cantidad de la tabla. No inventes números ni productos.
- Si hay productos para promocionar o dar de baja, recomendá qué hacer con ellos.
Terminá con una línea que empiece con "Resumen:" con la acción más importante del día."""


class AsistenteReposicion:
    _instancia = None
    _lock = threading.Lock()

    def __init__(self, modelo: str):
        from transformers import pipeline  # import diferido (pesado)

        inicio = time.time()
        print(f"[LLM] Cargando {modelo} (la primera vez se descarga)...")
        self.modelo = modelo
        self.generar = pipeline("text-generation", model=modelo, torch_dtype="auto")
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

    def redactar(self, tabla: str, max_tokens: int) -> str:
        mensajes = [
            {"role": "system", "content": INSTRUCCIONES},
            {"role": "user", "content": f"Productos a revisar:\n{tabla}"},
        ]
        salida = self.generar(mensajes, max_new_tokens=max_tokens, do_sample=False)
        return salida[0]["generated_text"][-1]["content"].strip()


def _tabla_para_prompt(candidatos: list[dict]) -> str:
    lineas = ["producto | proveedor | acción | cantidad | prioridad | detalle"]
    for c in candidatos[:15]:  # el contexto de un modelo chico es limitado
        lineas.append(
            f"{c['nombre']} | {c['proveedor']} | {c['accion']} | {c['cantidad_sugerida']} | "
            f"{c['prioridad']} | {c['motivo']}"
        )
    return "\n".join(lineas)


def resumen_por_reglas(candidatos: list[dict]) -> str:
    """Fallback sin modelo: arma el mismo tipo de resumen con plantillas."""
    if not candidatos:
        return "• El inventario está en orden: no hay productos bajo el mínimo ni por vencer.\nResumen: no hace falta pedir nada hoy."

    lineas = []
    reponer: dict[str, list[str]] = {}
    for c in candidatos:
        if c["accion"] == "Reponer":
            reponer.setdefault(c["proveedor"], []).append(f"{c['cantidad_sugerida']} u. de {c['nombre']}")
        elif c["accion"] == "Trasladar":
            lineas.append(f"• Trasladar {c['cantidad_sugerida']} u. de {c['nombre']} del depósito a la góndola.")
        elif c["accion"] == "Promocionar":
            lineas.append(f"• {c['nombre']} vence en {c['dias_para_vencer']} días: armar una promoción, no reponer.")
        elif c["accion"] == "Dar de baja":
            lineas.append(f"• {c['nombre']} está vencido: retirarlo de la góndola y registrar la merma.")
    for proveedor, productos in reponer.items():
        lineas.insert(0, f"• Pedido a {proveedor}: " + ", ".join(productos) + ".")

    urgente = candidatos[0]
    lineas.append(f"Resumen: lo más urgente es «{urgente['nombre']}» ({urgente['accion'].lower()}).")
    return "\n".join(lineas)


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

    try:
        inicio = time.time()
        asistente = AsistenteReposicion.obtener(cfg["LLM_MODEL"])
        resultado["texto"] = asistente.redactar(_tabla_para_prompt(candidatos), cfg["LLM_MAX_TOKENS"])
        resultado["fuente"] = "llm"
        resultado["segundos"] = round(time.time() - inicio, 1)
    except Exception as error:  # sin torch, sin RAM, sin internet la primera vez...
        current_app.logger.warning("LLM no disponible: %s", error)
        resultado["texto"] = resumen_por_reglas(candidatos)
        resultado["error"] = str(error)
    return resultado
