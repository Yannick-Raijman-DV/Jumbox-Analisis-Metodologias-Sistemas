"""Fixtures y pruebas integrales de Jumbox (migración, auth, inventario, S3, LLM y rutas)."""
from datetime import date, timedelta
import io
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    BajaProducto,
    Cliente,
    Envio,
    HistorialMovimiento,
    PedidoProveedor,
    Producto,
    Proveedor,
    Usuario,
    Venta,
)
from app.services import inventario, llm, s3
from app.services.inventario import ErrorNegocio
from config import TestingConfig
from migracion.convertir_dump import convertir


@pytest.fixture()
def app(tmp_path):
    class TestCfg(TestingConfig):
        pass

    aplicacion = create_app(TestCfg)
    # Redirigir uploads locales de test a carpeta temporal
    with aplicacion.app_context():
        yield aplicacion
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login_como(client, username: str, password: str = "1234"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# 1. Migración de supermercado.sql -> SQLite
# ---------------------------------------------------------------------------

def test_migracion_sql_completa(app, tmp_path):
    salida = tmp_path / "seed_test.json"
    fotos = tmp_path / "fotos_test"
    tablas = convertir(salida=salida, carpeta_fotos=fotos)

    assert len(tablas["roles_usuarios"]) == 3
    assert len(tablas["usuarios"]) == 7
    assert len(tablas["productos"]) == 5
    assert len(tablas["ventas"]) == 10
    assert len(tablas["ventas_productos"]) == 16
    assert (fotos / "1.webp").exists()
    assert (fotos / "3.jpg").exists()

    # Verificar que el seed poblo SQLite y corrigió la venta #1 con fk_usuario=3 huérfano
    assert Producto.query.count() == 5
    assert Venta.query.count() == 10
    venta_1 = db.session.get(Venta, 1)
    assert venta_1.usuario is not None
    assert venta_1.usuario.id == 1  # reasignada a administrador


# ---------------------------------------------------------------------------
# 2. Autenticación, jBCrypt ($2a$), Roles y Google elegir-rol
# ---------------------------------------------------------------------------

def test_login_usuarios_migrados_y_permisos_por_rol(client):
    # Operario tiene hash $2a$10$... de jBCrypt en supermercado.sql con contraseña '1234'
    resp = login_como(client, "operario", "1234")
    assert resp.status_code == 200
    assert "Dashboard Principal" in resp.get_data(as_text=True)

    # Operario puede entrar a Stock e IA, pero NO a POS (403)
    assert client.get("/stock/").status_code == 200
    assert client.get("/ia/reposicion").status_code == 200
    assert client.get("/pos/").status_code == 403
    client.get("/logout")

    # Cajero (hash $2a$10$...) puede entrar a POS y Clientes, pero NO a Stock (403)
    login_como(client, "cajero", "1234")
    assert client.get("/pos/").status_code == 200
    assert client.get("/clientes/").status_code == 200
    assert client.get("/stock/").status_code == 403
    client.get("/logout")

    # Administrador (re-hasheado desde texto plano '1234') accede a todo
    login_como(client, "administrador", "1234")
    assert client.get("/pos/").status_code == 200
    assert client.get("/stock/").status_code == 200
    assert client.get("/usuarios").status_code == 200


def test_google_usuario_nuevo_elige_rol_operario_o_cajero(client, app):
    with client.session_transaction() as sess:
        sess["google_pendiente"] = {
            "sub": "google-sub-999",
            "email": "nuevo@jumbox.com",
            "nombre": "Usuaria Google",
            "foto": "https://example.com/avatar.png",
        }

    # Intentar forzar rol Admin debe ser rechazado (400)
    r_invalido = client.post("/auth/elegir-rol", data={"rol": "Admin"})
    assert r_invalido.status_code == 400
    assert Usuario.query.filter_by(correo="nuevo@jumbox.com").first() is None

    # Elegir Operario crea la cuenta y loguea
    r_ok = client.post("/auth/elegir-rol", data={"rol": "Operario"}, follow_redirects=True)
    assert r_ok.status_code == 200
    creado = Usuario.query.filter_by(correo="nuevo@jumbox.com").first()
    assert creado is not None
    assert creado.rol_nombre == "Operario"
    assert creado.google_id == "google-sub-999"


# ---------------------------------------------------------------------------
# 3. Lógica Transaccional: POS, Traslados, Mermas, Proveedores y Puntos
# ---------------------------------------------------------------------------

def test_flujo_venta_traslado_merma_y_proveedor(app):
    admin = db.session.get(Usuario, 1)
    # Producto #2 (Lays) vence en 2027, precio $10, stock_gondola=1, stock_deposito=2
    lays = db.session.get(Producto, 2)
    cliente = db.session.get(Cliente, 1)

    # 1. Trasladar 2 unidades del depósito a la góndola (queda góndola=3, depósito=0)
    inventario.trasladar_a_gondola(admin, producto_id=lays.id, cantidad=2)
    assert lays.stock_gondola == 3
    assert lays.stock_deposito == 0

    # 2. Vender 2 unidades con envío a domicilio
    venta = inventario.registrar_venta(
        usuario=admin,
        items=[{"producto_id": lays.id, "cantidad": 2}],
        cliente_id=cliente.id,
        metodo_pago="Mercado Pago",
        envio={"direccion": "Av. Siempre Viva 742", "telefono": "11-2222-3333"},
    )
    assert venta.total == 20.0
    assert lays.stock_gondola == 1
    assert venta.envio is not None
    assert venta.envio.estado == "En preparación"

    # 3. Intentar vender un producto vencido (CocaCola #1 tiene fecha 2026-06-26 < hoy 2026-10-31)
    with pytest.raises(ErrorNegocio, match="vencido"):
        inventario.registrar_venta(usuario=admin, items=[{"producto_id": 1, "cantidad": 1}])

    # 4. Registrar merma de la unidad restante de Lays
    baja = inventario.registrar_merma(admin, producto_id=lays.id, cantidad=1, motivo="Rotura", ubicacion="gondola")
    assert baja.id is not None
    assert lays.stock_gondola == 0

    # 5. Pedido a proveedor #1 (CocaCola Company) y pago parcial de deuda
    prov = db.session.get(Proveedor, 1)
    assert prov.deuda == 0.0
    pedido = inventario.registrar_pedido_proveedor(
        usuario=admin,
        proveedor_id=prov.id,
        items=[{"producto_id": lays.id, "cantidad": 15, "precio_unitario": 10.0}],
    )
    assert pedido.total == 150.0
    assert prov.deuda == 150.0
    assert lays.stock_deposito == 15

    pago = inventario.registrar_pago_proveedor(admin, proveedor_id=prov.id, monto=50.0, comprobante_key="comprobantes/test.pdf")
    assert pago.monto == 50.0
    assert prov.deuda == 100.0

    # No permite pagar más de la deuda actual
    with pytest.raises(ErrorNegocio, match="supera la deuda"):
        inventario.registrar_pago_proveedor(admin, proveedor_id=prov.id, monto=200.0)


# ---------------------------------------------------------------------------
# 4. Servicio AWS S3 (Boto3 prefirmado + fallback local)
# ---------------------------------------------------------------------------

def test_s3_subida_y_url_prefirmada_con_mock_boto3(app):
    s3.reiniciar_cliente()
    app.config["S3_BUCKET"] = "jumbox-bucket-privado"

    cliente_falso = MagicMock()
    cliente_falso.generate_presigned_url.return_value = "https://jumbox-bucket-privado.s3.amazonaws.com/productos/abc.webp?X-Amz-Signature=123"

    with patch.object(s3, "cliente", return_value=cliente_falso):
        key = s3.subir_archivo(io.BytesIO(b"datos-imagen"), "foto.webp", carpeta="productos")
        assert key.startswith("productos/") and key.endswith(".webp")
        cliente_falso.upload_fileobj.assert_called_once()

        url = s3.url_prefirmada(key, expira=1800)
        assert "X-Amz-Signature=" in url
        cliente_falso.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "jumbox-bucket-privado", "Key": key},
            ExpiresIn=1800,
        )

        s3.borrar(key)
        cliente_falso.delete_object.assert_called_once_with(Bucket="jumbox-bucket-privado", Key=key)

    app.config["S3_BUCKET"] = None
    s3.reiniciar_cliente()


# ---------------------------------------------------------------------------
# 5. Servicio LLM Local (Singleton + Fallback por reglas)
# ---------------------------------------------------------------------------

def test_llm_singleton_y_fallback(app):
    llm.AsistenteReposicion.reiniciar()
    candidatos = inventario.calcular_candidatos_reposicion()
    assert len(candidatos) > 0

    # Con LLM_ENABLED=False usa reglas sin cargar el modelo
    app.config["LLM_ENABLED"] = False
    res_reglas = llm.generar_recomendacion(candidatos)
    assert res_reglas["fuente"] == "reglas"
    assert "Resumen:" in res_reglas["texto"]
    assert not llm.AsistenteReposicion.cargado()

    # Con LLM_ENABLED=True verifica patrón Singleton (se instancia una sola vez)
    app.config["LLM_ENABLED"] = True
    pipeline_falso = MagicMock(return_value=[{"generated_text": [{"content": "• Pedir Lays\nResumen: reponer stock."}]}])
    modulo_transformers_falso = types.SimpleNamespace(pipeline=MagicMock(return_value=pipeline_falso))
    with patch.dict(sys.modules, {"transformers": modulo_transformers_falso}):
        inst1 = llm.AsistenteReposicion.obtener("modelo-prueba")
        inst2 = llm.AsistenteReposicion.obtener("modelo-prueba")
        assert inst1 is inst2
        assert modulo_transformers_falso.pipeline.call_count == 1

        res_llm = llm.generar_recomendacion(candidatos)
        assert res_llm["fuente"] == "llm"
        assert "Resumen:" in res_llm["texto"]

    llm.AsistenteReposicion.reiniciar()
    app.config["LLM_ENABLED"] = False
