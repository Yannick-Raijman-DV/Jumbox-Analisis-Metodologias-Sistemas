"""Tests del comando `flask --app run s3-check` (todo mockeado: nunca habla con AWS de verdad)."""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app import create_app
from app.extensions import db
from app.services import s3
from config import TestingConfig

BUCKET = "jumbox-bucket-test"
URL_FIRMADA = f"https://{BUCKET}.s3.sa-east-1.amazonaws.com/diagnostico/x.txt?X-Amz-Signature=abc"


@pytest.fixture()
def app(monkeypatch):
    class TestCfg(TestingConfig):
        S3_BUCKET = BUCKET
        AWS_DEFAULT_REGION = "sa-east-1"
        SEED_AUTOMATICO = False  # con S3_BUCKET cargado, el seed intentaría hablar con AWS de verdad

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTJUMBOX1234")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secreto-de-prueba")
    s3.reiniciar_cliente()
    aplicacion = create_app(TestCfg)
    with aplicacion.app_context():
        yield aplicacion
        db.session.remove()
        db.drop_all()
    s3.reiniciar_cliente()


def respuesta(status: int, contenido: bytes = b"", texto: str = ""):
    r = MagicMock()
    r.status_code = status
    r.content = contenido
    r.text = texto
    return r


def cliente_s3_falso():
    """Cliente boto3 falso que recuerda lo que se subió."""
    falso = MagicMock()
    falso.subido = {}

    def upload(fileobj, bucket, key, ExtraArgs=None):
        falso.subido[key] = fileobj.read()

    falso.upload_fileobj.side_effect = upload
    falso.generate_presigned_url.return_value = URL_FIRMADA
    return falso


def test_s3_check_sin_bucket_avisa_modo_local(monkeypatch):
    s3.reiniciar_cliente()
    aplicacion = create_app(TestingConfig)  # S3_BUCKET = None
    resultado = aplicacion.test_cli_runner().invoke(args=["s3-check"])
    assert resultado.exit_code == 1
    assert "MODO LOCAL" in resultado.output


def test_s3_check_todo_ok(app):
    falso = cliente_s3_falso()

    def get(url, timeout):
        if "X-Amz-Signature" in url:  # URL prefirmada: devuelve lo que se subió
            return respuesta(200, next(iter(falso.subido.values())))
        return respuesta(403, texto="<Error><Code>AccessDenied</Code></Error>")  # sin firma: privado

    with patch.object(s3, "cliente", return_value=falso), patch("app.cli.requests.get", side_effect=get):
        resultado = app.test_cli_runner().invoke(args=["s3-check"])

    assert resultado.exit_code == 0, resultado.output
    assert resultado.output.count("[OK]") == 5
    assert "TODO OK" in resultado.output
    key = next(iter(falso.subido))
    assert key.startswith("diagnostico/prueba-") and key.endswith(".txt")
    falso.delete_object.assert_called_once_with(Bucket=BUCKET, Key=key)  # no deja basura
    assert "secreto-de-prueba" not in resultado.output  # nunca muestra el secret
    assert "...1234" in resultado.output


def test_s3_check_bucket_publico_falla(app):
    falso = cliente_s3_falso()
    with patch.object(s3, "cliente", return_value=falso), \
         patch("app.cli.requests.get", side_effect=lambda url, timeout: respuesta(200, next(iter(falso.subido.values())))):
        resultado = app.test_cli_runner().invoke(args=["s3-check"])
    assert resultado.exit_code == 1
    assert "PÚBLICO" in resultado.output
    falso.delete_object.assert_called_once()  # igual borra el archivo de prueba


def test_s3_check_sin_permiso_da_pista_de_la_politica(app):
    falso = cliente_s3_falso()
    falso.upload_fileobj.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "PutObject")
    with patch.object(s3, "cliente", return_value=falso), patch("app.cli.requests.get") as get:
        resultado = app.test_cli_runner().invoke(args=["s3-check"])
    assert resultado.exit_code == 1
    assert "política IAM" in resultado.output
    get.assert_not_called()
    falso.delete_object.assert_not_called()  # no había nada que borrar


def test_s3_check_sin_credenciales(app, monkeypatch):
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    resultado = app.test_cli_runner().invoke(args=["s3-check"])
    assert resultado.exit_code == 1
    assert "Faltan AWS_ACCESS_KEY_ID" in resultado.output
