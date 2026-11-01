"""Carga inicial de la base SQLite a partir de migracion/seed.json (derivado de supermercado.sql).

- Es idempotente: solo carga si `roles_usuarios` está vacía.
- Limpieza de integridad referencial:
  * La venta #1 del dump MySQL apunta a `fk_usuario=3` (que no existe en `usuarios`);
    se reasigna a `administrador` (`id=1`).
  * La contraseña en texto plano `'1234'` de `administrador` se hashea con Bcrypt.
    Los hashes `$2a$10$...` de jBCrypt (Java) de `operario` y `cajero` (cuya clave
    también es `'1234'`) se conservan intactos porque son compatibles con Flask-Bcrypt.
  * Los BLOBs de fotos extraídos en `migracion/fotos/` se suben con keys deterministas
    (`productos/seed/<id>.<ext>`) para no duplicar objetos en S3 en cada redeploy.
"""
import io
import json
from datetime import date, datetime
from pathlib import Path

from .extensions import bcrypt, db
from .models import (
    BajaProducto,
    Categoria,
    Cliente,
    Deposito,
    Envio,
    HistorialMovimiento,
    PedidoProveedor,
    PedidoProveedorProducto,
    Producto,
    ProductoDeposito,
    Proveedor,
    RolUsuario,
    Usuario,
    Venta,
    VentaProducto,
)
from .services import s3

RAIZ_SUPERMERCADO = Path(__file__).resolve().parent.parent
SEED_JSON = RAIZ_SUPERMERCADO / "migracion" / "seed.json"
CARPETA_FOTOS = RAIZ_SUPERMERCADO / "migracion" / "fotos"

# Códigos EAN-13 asignados a los 5 productos migrados para agilizar el escaneo en POS
CODIGOS_BARRAS_SEED = {
    1: "7790895000997",  # CocaCola
    2: "7790310980019",  # Lays
    3: "7790310980026",  # Cheetos
    4: "7790580001018",  # Topline
    5: "7791813420019",  # Pepsi
}


def _parse_fecha(valor: str) -> date:
    return date.fromisoformat(valor)


def _parse_fecha_hora(valor: str | None) -> datetime | None:
    if not valor:
        return None
    return datetime.fromisoformat(valor)


def _subir_foto_seed(nombre_archivo: str | None, producto_id: int) -> str | None:
    """Sube la foto extraída del dump (si existe) con key determinista."""
    if not nombre_archivo:
        return None
    ruta = CARPETA_FOTOS / nombre_archivo
    if not ruta.exists():
        return None
    ext = ruta.suffix.lower()
    key = f"productos/seed/{producto_id}{ext}"
    try:
        if not s3.existe(key):
            s3.subir_archivo(io.BytesIO(ruta.read_bytes()), nombre_archivo, carpeta="productos/seed", key=key)
        return key
    except Exception:
        # Si S3 falla por credenciales inválidas en entorno local, continúa sin romper el arranque
        return None


def cargar_seed(reset: bool = False) -> dict:
    """Puebla la base con los datos migrados de supermercado.sql."""
    if reset:
        db.drop_all()
        db.create_all()

    if RolUsuario.query.first() is not None:
        return {"cargado": False, "motivo": "La base ya contiene datos."}

    datos = json.loads(SEED_JSON.read_text(encoding="utf-8"))

    # 1. Roles
    for r in datos.get("roles_usuarios", []):
        db.session.add(RolUsuario(id=r["id_rol_usuario"], nombre=r["rol_usuario"]))

    # 2. Usuarios (re-hashear si la contraseña no es un hash bcrypt válido)
    ids_usuarios_validos = set()
    for u in datos.get("usuarios", []):
        pw = u["contrasena"] or ""
        if not pw.startswith(("$2a$", "$2b$", "$2y$")):
            pw = bcrypt.generate_password_hash(pw).decode("utf-8")
        db.session.add(
            Usuario(
                id=u["id_usuario"],
                nombre=u["nombre_usuario"],
                contrasena=pw,
                correo=u["correo_usuario"],
                rol_id=u["fk_roles_usuarios"],
            )
        )
        ids_usuarios_validos.add(u["id_usuario"])

    # 3. Categorías, Depósitos, Proveedores, Clientes
    for c in datos.get("categorias_productos", []):
        db.session.add(Categoria(id=c["id_categoria_producto"], nombre=c["nombre_categoria"]))

    for d in datos.get("depositos", []):
        db.session.add(Deposito(id=d["id_deposito"], nombre=d["nombre_deposito"]))

    for pr in datos.get("proveedores", []):
        db.session.add(
            Proveedor(
                id=pr["id_proveedor"],
                nombre=pr["nombre_proveedor"],
                cuit=pr["cuit_proveedor"],
                deuda=float(pr.get("deuda_proveedor") or 0.0),
            )
        )

    direcciones_demo = {
        1: ("11-4555-0101", "Av. Corrientes 2048, CABA"),
        2: ("11-4555-0102", "Av. Santa Fe 3120, CABA"),
        3: ("11-4555-0103", "Av. Cabildo 1540, CABA"),
        4: ("11-4555-0104", "Av. Rivadavia 5200, CABA"),
    }
    for cl in datos.get("clientes", []):
        tel, direc = direcciones_demo.get(cl["id_cliente"], (None, None))
        db.session.add(
            Cliente(
                id=cl["id_cliente"],
                nombre=cl["nombre_cliente"],
                apellido=cl["apellido"],
                dni=cl["dni_cliente"],
                puntos=int(cl.get("puntos_cliente") or 0),
                telefono=tel,
                direccion=direc,
            )
        )

    # 4. Productos + fotos a S3/local
    for p in datos.get("productos", []):
        pid = p["id_producto"]
        foto_key = _subir_foto_seed(p.get("foto_archivo"), pid)
        db.session.add(
            Producto(
                id=pid,
                nombre=p["nombre_producto"],
                precio=float(p["precio_producto"]),
                stock_gondola=int(p["stock_actual_producto"]),
                stock_minimo=int(p["stock_minimo_producto"]),
                fecha_vencimiento=_parse_fecha(p["fecha_vencimiento_producto"]),
                foto_key=foto_key,
                codigo_barras=CODIGOS_BARRAS_SEED.get(pid),
                categoria_id=p["fk_categoria_producto"],
                proveedor_id=p["fk_proveedor"],
            )
        )

    # 5. Stock en depósitos
    for pd in datos.get("productos_depositos", []):
        db.session.add(
            ProductoDeposito(
                id=pd["id_producto_deposito"],
                stock=int(pd["stock_producto_deposito"]),
                producto_id=pd["fk_producto"],
                deposito_id=pd["fk_deposito"],
            )
        )

    # 6. Pedidos a proveedor y sus renglones
    totales_pedido: dict[int, float] = {}
    for item in datos.get("pedidos_proveedor_productos", []):
        totales_pedido[item["fk_pedido_proveedor"]] = (
            totales_pedido.get(item["fk_pedido_proveedor"], 0.0) + float(item["total"])
        )

    for ped in datos.get("pedidos_proveedor", []):
        uid = ped["fk_usuario"] if ped["fk_usuario"] in ids_usuarios_validos else 1
        db.session.add(
            PedidoProveedor(
                id=ped["id_pedido_proveedor"],
                fecha=_parse_fecha_hora(ped["fecha_pedido"]),
                usuario_id=uid,
                proveedor_id=ped["fk_proveedor"],
                total=round(totales_pedido.get(ped["id_pedido_proveedor"], 0.0), 2),
                estado="Recibido",
            )
        )

    for item in datos.get("pedidos_proveedor_productos", []):
        db.session.add(
            PedidoProveedorProducto(
                id=item["id_pedido_proveedor_producto"],
                cantidad=int(item["cantidad"]),
                precio_unitario=float(item["precio_unitario"]),
                total=float(item["total"]),
                pedido_id=item["fk_pedido_proveedor"],
                producto_id=item["fk_producto"],
            )
        )

    # 7. Ventas y sus renglones (reasignar fk_usuario huérfano #3 -> #1 administrador)
    totales_venta: dict[int, float] = {}
    for vp in datos.get("ventas_productos", []):
        totales_venta[vp["fk_venta"]] = totales_venta.get(vp["fk_venta"], 0.0) + float(vp["total"])

    for v in datos.get("ventas", []):
        uid = v["fk_usuario"] if v["fk_usuario"] in ids_usuarios_validos else 1
        vid = v["id_venta"]
        con_envio = vid in (6, 8, 10)
        db.session.add(
            Venta(
                id=vid,
                fecha=_parse_fecha_hora(v["fecha_venta"]),
                usuario_id=uid,
                cliente_id=v.get("fk_cliente"),
                total=round(totales_venta.get(vid, 0.0), 2),
                metodo_pago="Efectivo",
                tipo_entrega="envio" if con_envio else "mostrador",
            )
        )

    for vp in datos.get("ventas_productos", []):
        db.session.add(
            VentaProducto(
                id=vp["id_venta_producto"],
                cantidad=int(vp["cantidad"]),
                precio_unitario=float(vp["precio_unitario"]),
                total=float(vp["total"]),
                venta_id=vp["fk_venta"],
                producto_id=vp["fk_producto"],
            )
        )

    # 8. Bajas y Movimientos
    for b in datos.get("bajas_productos", []):
        db.session.add(
            BajaProducto(
                id=b["id_baja_producto"],
                producto_id=b.get("fk_producto"),
                motivo=b.get("motivo_baja"),
                cantidad=b.get("cantidad_baja"),
                fecha=_parse_fecha_hora(b.get("fecha_baja")),
                usuario_id=1,
            )
        )

    for m in datos.get("historial_movimientos", []):
        uid = m["fk_usuario"] if m["fk_usuario"] in ids_usuarios_validos else 1
        db.session.add(
            HistorialMovimiento(
                id=m["id_movimiento"],
                fecha=_parse_fecha_hora(m["fecha_movimiento"]),
                producto_id=m["fk_producto"],
                cantidad=int(m["cantidad"]),
                tipo=m["tipo_movimiento"],
                origen=m["origen"],
                destino=m["destino"],
                usuario_id=uid,
            )
        )

    # 9. Envíos iniciales asociados a las ventas con cliente del dump
    envios_demo = [
        (6, "Av. Santa Fe 3120, CABA", "11-4555-0102", "Entregado", "Martín R.", "Timbre 4B"),
        (8, "Av. Corrientes 2048, CABA", "11-4555-0101", "En camino", "Lucía V.", "Dejar en recepción"),
        (10, "Av. Rivadavia 5200, CABA", "11-4555-0104", "En preparación", None, "Pago ya acreditado"),
    ]
    for vid, direc, tel, est, rep, obs in envios_demo:
        db.session.add(
            Envio(
                venta_id=vid,
                direccion=direc,
                telefono=tel,
                estado=est,
                repartidor=rep,
                observaciones=obs,
            )
        )

    db.session.commit()
    return {"cargado": True, "productos": len(datos.get("productos", [])), "ventas": len(datos.get("ventas", []))}


def seed_si_vacia() -> None:
    if SEED_JSON.exists() and RolUsuario.query.first() is None:
        cargar_seed(reset=False)
