"""Integración con AWS S3 (boto3).

- El bucket es PRIVADO: nunca se exponen URLs públicas. Para mostrar una imagen
  se genera una URL prefirmada que vence en S3_URL_EXPIRA segundos.
- En la base de datos solo se guarda la "key" del objeto (ej. productos/ab12.webp).
- Si S3_BUCKET no está configurado, los archivos se guardan en app/static/uploads
  (modo local, útil para desarrollo sin credenciales y para los tests).
"""
import os
import uuid
from pathlib import Path

from flask import current_app, url_for

EXTENSIONES_IMAGEN = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
EXTENSIONES_COMPROBANTE = EXTENSIONES_IMAGEN | {".pdf"}
CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
}

_cliente_s3 = None  # singleton del cliente boto3


def usa_s3() -> bool:
    return bool(current_app.config.get("S3_BUCKET"))


def _bucket() -> str:
    return current_app.config["S3_BUCKET"]


def carpeta_local() -> Path:
    return Path(current_app.static_folder) / "uploads"


def cliente():
    """Devuelve el cliente de S3, creándolo una sola vez."""
    global _cliente_s3
    if _cliente_s3 is None:
        import boto3
        from botocore.config import Config as BotoConfig

        region = current_app.config["AWS_DEFAULT_REGION"]
        # s3v4 + endpoint regional: las URLs prefirmadas funcionan en todas las regiones (ej. sa-east-1)
        _cliente_s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://s3.{region}.amazonaws.com",
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _cliente_s3


def reiniciar_cliente():
    """Olvida el cliente cacheado (lo usan los tests)."""
    global _cliente_s3
    _cliente_s3 = None


def extension(nombre_archivo: str) -> str:
    return os.path.splitext(nombre_archivo or "")[1].lower()


def extension_valida(nombre_archivo: str, permitidas=EXTENSIONES_IMAGEN) -> bool:
    return extension(nombre_archivo) in permitidas


def subir_archivo(fileobj, nombre_original: str, carpeta: str = "productos", key: str | None = None) -> str:
    """Sube un archivo (objeto tipo file) y devuelve su key.

    Si no se indica key se genera un nombre único con uuid4 para no pisar archivos.
    """
    ext = extension(nombre_original)
    key = key or f"{carpeta}/{uuid.uuid4().hex}{ext}"
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

    if usa_s3():
        cliente().upload_fileobj(fileobj, _bucket(), key, ExtraArgs={"ContentType": content_type})
    else:
        destino = carpeta_local() / key
        destino.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(fileobj, "save"):  # FileStorage de Werkzeug
            fileobj.save(destino)
        else:
            destino.write_bytes(fileobj.read())
    return key


def existe(key: str) -> bool:
    if not key:
        return False
    if usa_s3():
        from botocore.exceptions import ClientError
        try:
            cliente().head_object(Bucket=_bucket(), Key=key)
            return True
        except ClientError:
            return False
    return (carpeta_local() / key).exists()


def url_prefirmada(key: str | None, expira: int | None = None) -> str | None:
    """URL temporal de lectura (S3) o URL estática (modo local)."""
    if not key:
        return None
    if usa_s3():
        return cliente().generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=expira or current_app.config["S3_URL_EXPIRA"],
        )
    return url_for("static", filename=f"uploads/{key}")


def borrar(key: str | None) -> None:
    if not key:
        return
    if usa_s3():
        cliente().delete_object(Bucket=_bucket(), Key=key)
    else:
        ruta = carpeta_local() / key
        if ruta.exists():
            ruta.unlink()
