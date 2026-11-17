"""Configuración de la aplicación. Todo lo sensible se lee del entorno (.env)."""
import os
import ssl
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _configurar_ca_windows() -> None:
    """Combina certifi con el almacén de certificados de Windows para requests y boto3."""
    if sys.platform != "win32":
        return
    if os.environ.get("REQUESTS_CA_BUNDLE") and os.environ.get("AWS_CA_BUNDLE"):
        return
    try:
        import certifi

        bloques = [Path(certifi.where()).read_text(encoding="ascii", errors="ignore")]
        vistos: set[str] = set()
        for almacen in ("ROOT", "CA"):
            for der, codificacion, _ in ssl.enum_certificates(almacen):
                if codificacion != "x509_asn":
                    continue
                pem = ssl.DER_cert_to_PEM_cert(der)
                if pem not in vistos:
                    vistos.add(pem)
                    bloques.append(pem)

        bundle = BASE_DIR / "instance" / "ca_bundle_win.pem"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("\n".join(bloques), encoding="ascii")
        os.environ.setdefault("REQUESTS_CA_BUNDLE", str(bundle))
        os.environ.setdefault("AWS_CA_BUNDLE", str(bundle))
    except Exception:
        pass


_configurar_ca_windows()


def _bool(nombre: str, defecto: bool = False) -> bool:
    valor = os.environ.get(nombre)
    if valor is None or valor.strip() == "":
        return defecto
    return valor.strip().lower() in ("1", "true", "si", "sí", "yes", "on")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-insegura-cambiar"

    # Base de datos: por defecto SQLite en instance/proyecto.db
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or (
        "sqlite:///" + str(BASE_DIR / "instance" / "proyecto.db").replace("\\", "/")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SEED_AUTOMATICO = _bool("SEED_AUTOMATICO", True)

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB por request (fotos y comprobantes)

    # Google OAuth
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

    # AWS S3 (boto3 lee AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY del entorno)
    AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    S3_BUCKET = os.environ.get("S3_BUCKET") or None
    S3_URL_EXPIRA = int(os.environ.get("S3_URL_EXPIRA") or 3600)

    # LLM local
    LLM_ENABLED = _bool("LLM_ENABLED", False)
    LLM_MODEL = os.environ.get("LLM_MODEL") or "Qwen/Qwen2.5-0.5B-Instruct"
    LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS") or 300)

    # Geoapify (API externa con API key): ubica la dirección del envío, calcula la ruta y dibuja el mapa
    GEOAPIFY_API_KEY = os.environ.get("GEOAPIFY_API_KEY") or None
    SUCURSAL_NOMBRE = os.environ.get("SUCURSAL_NOMBRE") or "Sucursal Centro"
    SUCURSAL_LAT = float(os.environ.get("SUCURSAL_LAT") or -34.6037)  # Obelisco, CABA
    SUCURSAL_LON = float(os.environ.get("SUCURSAL_LON") or -58.3816)
    ENVIO_COSTO_BASE = float(os.environ.get("ENVIO_COSTO_BASE") or 1500)  # $ fijo por envío
    ENVIO_COSTO_KM = float(os.environ.get("ENVIO_COSTO_KM") or 400)  # $ por km recorrido

    # Mercado Pago (API externa con Access Token de PRUEBA): cobro del ticket con Checkout Pro
    MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN") or None

    # DolarAPI (API externa pública, sin key): cotización del dólar al momento de la venta
    DOLAR_TIPO = os.environ.get("DOLAR_TIPO") or "oficial"  # oficial | blue | tarjeta | mep ...
    DOLAR_CACHE_SEGUNDOS = int(os.environ.get("DOLAR_CACHE_SEGUNDOS") or 300)

    # Reglas de negocio
    DIAS_ALERTA_VENCIMIENTO = 7
    PESOS_POR_PUNTO = 100  # 1 punto de fidelización cada $100
    DEUDA_CRITICA = 500.0  # a partir de este monto la deuda se muestra en rojo


class TestingConfig(Config):
    TESTING = True
    SECRET_KEY = "test"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    S3_BUCKET = None
    LLM_ENABLED = False
    GEOAPIFY_API_KEY = None
    MP_ACCESS_TOKEN = None
    DOLAR_CACHE_SEGUNDOS = 300
    SEED_AUTOMATICO = True
    GOOGLE_CLIENT_ID = "test-client-id"
    GOOGLE_CLIENT_SECRET = "test-client-secret"
