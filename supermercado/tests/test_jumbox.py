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

    # Operario puede entrar a Stock, IA y al endpoint asíncrono /ia/api/recomendacion, pero NO a POS (403)
    assert client.get("/stock/").status_code == 200
    assert client.get("/ia/reposicion").status_code == 200
    r_api_ia = client.get("/ia/api/recomendacion")
    assert r_api_ia.status_code == 200
    assert "texto" in r_api_ia.get_json()
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

    # Administrador puede cambiar el rol de otro usuario (ej. cajero #10 -> Operario rol_id=2)
    r_cambio = client.post("/usuarios/10/rol", data={"rol_id": 2}, follow_redirects=True)
    assert r_cambio.status_code == 200
    assert db.session.get(Usuario, 10).rol_nombre == "Operario"

    # Administrador NO puede quitarse el rol Admin a sí mismo
    client.post("/usuarios/1/rol", data={"rol_id": 3}, follow_redirects=True)
    assert db.session.get(Usuario, 1).rol_nombre == "Admin"


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

    # 2. Vender 2 unidades con envío a domicilio (el envío se suma al total de la venta)
    venta = inventario.registrar_venta(
        usuario=admin,
        items=[{"producto_id": lays.id, "cantidad": 2}],
        cliente_id=cliente.id,
        metodo_pago="Mercado Pago",
        envio={"direccion": "Av. Siempre Viva 742", "telefono": "11-2222-3333"},
    )
    costo_esperado = app.config["ENVIO_COSTO_BASE"]
    assert venta.subtotal_productos == 20.0
    assert venta.envio is not None
    assert venta.envio.costo == costo_esperado
    assert venta.total == 20.0 + costo_esperado
    assert lays.stock_gondola == 1
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
        assert "Resumen IA" in res_llm["texto"]

        # El modelo recibe las reglas del negocio y el monto/deuda de cada pedido
        mensajes = pipeline_falso.call_args.args[0]
        assert "Reglas del negocio" in mensajes[0]["content"]
        assert f"vence en {app.config['DIAS_ALERTA_VENCIMIENTO']} días" in mensajes[0]["content"]
        assert any(c["accion"] == "Reponer" for c in candidatos)
        assert "deuda actual con el proveedor" in mensajes[-1]["content"]

    llm.AsistenteReposicion.reiniciar()
    app.config["LLM_ENABLED"] = False


def test_llm_pedido_con_monto_y_deuda():
    candidatos = [
        {"accion": "Reponer", "nombre": "Bon o Bon", "proveedor": "Arcor", "cantidad_sugerida": 10,
         "precio_compra": 50.0, "deuda_proveedor": 1200.0},
        {"accion": "Reponer", "nombre": "Rocklets", "proveedor": "Arcor", "cantidad_sugerida": 2,
         "precio_compra": 25.0, "deuda_proveedor": 1200.0},
    ]
    assert llm.puntos_operativos(candidatos) == [
        "• PEDIDO A ARCOR — Solicitar 10 u. de Bon o Bon, 2 u. de Rocklets "
        "(unos $550.00; deuda actual con el proveedor: $1,200.00)."
    ]


def test_llm_validador_rechaza_alucinaciones():
    cands = [{"accion": "Dar de baja", "nombre": "CocaCola", "cantidad_sugerida": 4, "motivo": "Vencido"}]
    ctx = "• URGENTE — CocaCola está vencido: retirar 4 u. y registrar merma."
    # Respuesta válida
    assert llm._es_respuesta_precisa("Retirar las 4 unidades de CocaCola vencida hoy.", cands, ctx) is True
    # Inventó otro número (99) -> rechazada
    assert llm._es_respuesta_precisa("Retirar las 99 unidades de CocaCola vencida hoy.", cands, ctx) is False
    # Inventó otro producto (Leche) -> rechazada
    assert llm._es_respuesta_precisa("Retirar Leche 1L hoy.", cands, ctx) is False


def test_stock_filtro_categoria_y_modal_alta(client):
    login_como(client, "operario", "1234")
    # En inventario hay selector de filtro, no input de alta accidental
    html_stock = client.get("/stock/").get_data(as_text=True)
    assert 'id="filtro-categoria"' in html_stock
    assert "Agregar categoría" not in html_stock

    # El alta de categoría está disponible desde el modal de Nuevo Producto
    html_form = client.get("/stock/productos/nuevo").get_data(as_text=True)
    assert "+ Nueva categoría" in html_form
    assert 'id="modal-categoria"' in html_form

    r_cat = client.post("/stock/categorias/nueva", data={"nombre_categoria": "Lácteos"}, follow_redirects=True)
    assert r_cat.status_code == 200
    assert "Lácteos" in r_cat.get_data(as_text=True)


def test_vistas_por_rol_y_historial_ventas(client):
    # 1. Cajero: inicio enfocado en caja, ventas y ranking (sin alertas de stock ni deuda a proveedores)
    login_como(client, "cajero", "1234")
    html_cajero = client.get("/").get_data(as_text=True)
    assert "Panel de Caja" in html_cajero
    assert "Productos más vendidos" in html_cajero
    assert "Últimas ventas" in html_cajero
    assert "Deuda con proveedores" not in html_cajero
    assert "Productos que necesitan atención" not in html_cajero

    r_ventas = client.get("/pos/ventas")
    assert r_ventas.status_code == 200
    html_ventas = r_ventas.get_data(as_text=True)
    assert "Productos más vendidos" in html_ventas
    assert "Historial de tickets" in html_ventas
    assert client.get("/pos/ventas?metodo=Efectivo").status_code == 200
    client.get("/logout")

    # 2. Operario: inicio enfocado en depósito/stock/proveedores (sin últimas ventas ni acceso a /pos/ventas)
    login_como(client, "operario", "1234")
    html_operario = client.get("/").get_data(as_text=True)
    assert "Panel de Depósito y Góndola" in html_operario
    assert "Productos que necesitan atención" in html_operario
    assert "Movimientos recientes" in html_operario
    assert "Últimas ventas" not in html_operario
    assert client.get("/pos/ventas").status_code == 403
    client.get("/logout")

    # 3. Admin: panel ejecutivo descomprimido + acceso completo a /pos/ventas
    login_como(client, "administrador", "1234")
    html_admin = client.get("/").get_data(as_text=True)
    assert "Panel Ejecutivo" in html_admin
    assert "Reporte comercial (7 días)" in html_admin
    assert "Alertas operativas de stock" in html_admin
    assert client.get("/pos/ventas").status_code == 200


def test_mejoras_buscador_clientes_movimientos_y_mermas(client):
    # 1. Admin: buscador muestra tickets (#10 / 10), clientes, productos y proveedores con links
    login_como(client, "administrador", "1234")
    html_ticket = client.get("/buscador?q=%2310").get_data(as_text=True)
    assert "Tickets (1)" in html_ticket
    assert "/pos/ticket/10" in html_ticket

    html_coca = client.get("/buscador?q=CocaCola").get_data(as_text=True)
    assert "Productos (1)" in html_coca
    assert "/stock/productos/1/editar" in html_coca

    # 2. Clientes: buscador y modal de edición + POST /clientes/<id>/editar
    html_cli = client.get("/clientes/").get_data(as_text=True)
    assert 'id="modal-editar-cliente"' in html_cli
    assert 'name="q"' in html_cli
    r_edit = client.post(
        "/clientes/1/editar",
        data={
            "nombre": "ClienteEditado",
            "apellido": "Demo",
            "dni": "99888777",
            "telefono": "11-5555-9999",
            "direccion": "Av. Callao 500, CABA",
        },
        follow_redirects=True,
    )
    assert r_edit.status_code == 200
    assert "ClienteEditado" in client.get("/clientes/?q=99888777").get_data(as_text=True)

    # 3. Movimientos: píldoras de filtro por tipo real (INGRESO_COMPRA, etc.)
    html_mov = client.get("/stock/movimientos").get_data(as_text=True)
    assert "tipo=INGRESO_COMPRA" in html_mov
    assert "tipo=BAJA_MERMA" in html_mov
    assert client.get("/stock/movimientos?tipo=INGRESO_COMPRA").status_code == 200

    # 4. Preselección en Mermas desde Stock / IA
    html_merma = client.get("/stock/mermas?producto_id=2").get_data(as_text=True)
    assert '<option value="2" selected' in html_merma
    html_ia = client.get("/ia/reposicion").get_data(as_text=True)
    if "Ir a Mermas" in html_ia:
        assert "/stock/mermas?producto_id=" in html_ia
    client.get("/logout")

    # 5. Buscador filtrado por rol: Cajero no ve Proveedores; Operario no ve Clientes ni Tickets
    login_como(client, "cajero", "1234")
    html_busc_cajero = client.get("/buscador?q=10").get_data(as_text=True)
    assert "Tickets (1)" in html_busc_cajero
    assert "Proveedores (" not in html_busc_cajero
    client.get("/logout")

    login_como(client, "operario", "1234")
    html_busc_op = client.get("/buscador?q=10").get_data(as_text=True)
    assert "Proveedores (" in html_busc_op
    assert "Clientes (" not in html_busc_op
    assert "Tickets (" not in html_busc_op


