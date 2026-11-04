"""Configuración de la aplicación. Todo lo sensible se lee del entorno (.env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


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
    SEED_AUTOMATICO = True
    GOOGLE_CLIENT_ID = "test-client-id"
    GOOGLE_CLIENT_SECRET = "test-client-secret"
