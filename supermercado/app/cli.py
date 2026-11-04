"""Comandos de consola de Jumbox: `flask --app run <comando>`.

s3-check: prueba el bucket de AWS S3 de punta a punta, sin tocar datos de la app.
    1. Variables cargadas (sin mostrar secretos).
    2. Sube diagnostico/prueba-UUID.txt (PutObject).
    3. Lo baja con una URL prefirmada (GetObject) y compara el contenido.
    4. Lo pide SIN firma: tiene que dar 403 (bucket privado).
    5. Lo borra (DeleteObject), pase lo que pase en los pasos anteriores.
Termina con exit code 1 si algún paso falla, así se puede usar en scripts.
"""
import io
import os
import re
import uuid

import click
import requests
from flask import current_app

from .services import s3

# Código de error de AWS -> qué revisar (en castellano)
PISTAS_AWS = {
    "AccessDenied": "La política IAM no permite esta acción: revisá s3:PutObject, s3:GetObject y s3:DeleteObject "
                    "sobre arn:aws:s3:::BUCKET/*.",
    "NoSuchBucket": "No existe un bucket con ese nombre: revisá S3_BUCKET (solo el nombre, sin s3:// ni URL).",
    "InvalidAccessKeyId": "El AWS_ACCESS_KEY_ID no existe o fue borrado: copiá el de la access key activa.",
    "SignatureDoesNotMatch": "El AWS_SECRET_ACCESS_KEY está mal copiado (espacios, comillas o le falta un caracter).",
    "PermanentRedirect": "La región no coincide con la del bucket: revisá AWS_DEFAULT_REGION (ej. sa-east-1).",
    "AuthorizationHeaderMalformed": "La región no coincide con la del bucket: revisá AWS_DEFAULT_REGION.",
    "NoCredentialsError": "Faltan AWS_ACCESS_KEY_ID y/o AWS_SECRET_ACCESS_KEY en el .env.",
}


def _codigo_aws(error: Exception) -> str:
    """Saca el código de error de una excepción de boto3 (ClientError o NoCredentialsError)."""
    respuesta = getattr(error, "response", None)
    if isinstance(respuesta, dict):
        return str(respuesta.get("Error", {}).get("Code") or type(error).__name__)
    return type(error).__name__


def _pista(codigo: str) -> str:
    return PISTAS_AWS.get(codigo, f"Error de AWS: {codigo}.")


def _codigo_en_xml(texto: str) -> str:
    """S3 contesta los errores HTTP con un XML <Error><Code>...</Code>."""
    m = re.search(r"<Code>([^<]+)</Code>", texto or "")
    return m.group(1) if m else ""


def registrar_comandos(app):
    @app.cli.command("s3-check")
    def s3_check():
        """Verifica que el bucket de S3 funcione (subir, URL prefirmada, privado y borrar)."""
        ok = True

        def paso(numero: int, texto: str, bien: bool, detalle: str = ""):
            nonlocal ok
            ok = ok and bien
            marca = "[OK]   " if bien else "[ERROR]"
            click.echo(f"{marca} {numero}. {texto}" + (f" -> {detalle}" if detalle else ""))

        # 1. Variables
        bucket = current_app.config.get("S3_BUCKET")
        if not bucket:
            click.echo("[ERROR] S3_BUCKET está vacío: la app está en MODO LOCAL (app/static/uploads). "
                       "Cargá S3_BUCKET y las credenciales AWS_* en el .env.")
            raise SystemExit(1)
        region = current_app.config["AWS_DEFAULT_REGION"]
        key_id = os.environ.get("AWS_ACCESS_KEY_ID") or ""
        tiene_secret = bool(os.environ.get("AWS_SECRET_ACCESS_KEY"))
        variables_ok = bool(key_id) and tiene_secret
        paso(1, "Variables cargadas", variables_ok,
             f"bucket={bucket} · región={region} · access key=...{key_id[-4:] or '(falta)'} · "
             f"secret={'cargado' if tiene_secret else '(falta)'}")
        if not variables_ok:
            click.echo("        " + _pista("NoCredentialsError"))
            raise SystemExit(1)

        key = f"diagnostico/prueba-{uuid.uuid4().hex}.txt"
        contenido = f"Jumbox s3-check {key}".encode()
        subido = False
        try:
            # 2. Subida
            try:
                s3.subir_archivo(io.BytesIO(contenido), "prueba.txt", key=key)
                subido = True
                paso(2, "Subir archivo de prueba (PutObject)", True, key)
            except Exception as error:  # ClientError, NoCredentialsError, etc.
                codigo = _codigo_aws(error)
                paso(2, "Subir archivo de prueba (PutObject)", False, codigo)
                click.echo("        " + _pista(codigo))
                raise SystemExit(1)

            # 3. URL prefirmada
            url = s3.url_prefirmada(key, expira=300)
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and resp.content == contenido:
                paso(3, "Bajarlo con URL prefirmada (GetObject)", True, "HTTP 200, contenido correcto")
            else:
                codigo = _codigo_en_xml(resp.text) or f"HTTP {resp.status_code}"
                paso(3, "Bajarlo con URL prefirmada (GetObject)", False, codigo)
                click.echo("        " + _pista(codigo))

            # 4. Bucket privado: sin firma tiene que dar 403
            sin_firma = requests.get(f"https://{bucket}.s3.{region}.amazonaws.com/{key}", timeout=15)
            if sin_firma.status_code == 403:
                paso(4, "Bucket privado (GET sin firma)", True, "HTTP 403: nadie lo puede leer sin URL firmada")
            else:
                paso(4, "Bucket privado (GET sin firma)", False, f"HTTP {sin_firma.status_code}")
                click.echo("        Si dio 200, el bucket es PÚBLICO: activá 'Block all public access' "
                           "y sacá cualquier bucket policy.")
        except requests.RequestException:
            paso(3, "Conexión con S3", False, "no hay conexión a internet o S3 no respondió")
        finally:
            # 5. Borrado (siempre, para no dejar basura en el bucket)
            if subido:
                try:
                    s3.borrar(key)
                    paso(5, "Borrar archivo de prueba (DeleteObject)", True, "borrado")
                except Exception as error:
                    codigo = _codigo_aws(error)
                    paso(5, "Borrar archivo de prueba (DeleteObject)", False, codigo)
                    click.echo("        " + _pista(codigo))

        click.echo("RESULTADO: " + ("TODO OK, el bucket funciona." if ok else "HAY ERRORES (ver arriba)."))
        if not ok:
            raise SystemExit(1)
