"""Lógica de negocio de Jumbox (stock, ventas, mermas, proveedores, envíos).

Las rutas validan la entrada HTTP y llaman a estas funciones. Cada operación
se hace en una única transacción: si algo falla se hace rollback y se lanza
ErrorNegocio con un mensaje para el usuario.

Modelo de stock (heredado del sistema Java):
    productos.stock_actual_producto         -> stock en GÓNDOLA
    productos_depositos.stock_producto_dep. -> stock en DEPÓSITO
    Proveedor --INGRESO_COMPRA--> Depósito --TRASLADO_GONDOLA--> Góndola --EGRESO_VENTA--> Cliente
"""
import math
from datetime import date, datetime, timedelta

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from ..models import (
    ESTADOS_ENVIO,
    MOV_BAJA_MERMA,
    MOV_EGRESO_VENTA,
    MOV_INGRESO_COMPRA,
    MOV_TRASLADO_GONDOLA,
    BajaProducto,
    Cliente,
    Deposito,
    Envio,
    HistorialMovimiento,
    PagoProveedor,
    PedidoProveedor,
    PedidoProveedorProducto,
    Producto,
    ProductoDeposito,
    Proveedor,
    Venta,
    VentaProducto,
    ahora,
)

GONDOLA = "Góndola"


class ErrorNegocio(Exception):
    """Error de validación de una regla de negocio (se muestra al usuario)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entero_positivo(valor, campo: str) -> int:
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        raise ErrorNegocio(f"La {campo} tiene que ser un número entero.")
    if numero <= 0:
        raise ErrorNegocio(f"La {campo} tiene que ser mayor a cero.")
    return numero


def _producto(producto_id) -> Producto:
    producto = db.session.get(Producto, int(producto_id)) if str(producto_id).isdigit() else None
    if producto is None:
        raise ErrorNegocio("El producto no existe.")
    return producto


def deposito_principal() -> Deposito:
    deposito = Deposito.query.order_by(Deposito.id).first()
    if deposito is None:
        deposito = Deposito(nombre="Depósito Central")
        db.session.add(deposito)
        db.session.flush()
    return deposito


def stock_en_deposito(producto: Producto) -> ProductoDeposito:
    """Fila de stock del producto en el depósito principal (la crea si no existe)."""
    deposito = deposito_principal()
    fila = ProductoDeposito.query.filter_by(producto_id=producto.id, deposito_id=deposito.id).first()
    if fila is None:
        fila = ProductoDeposito(producto=producto, deposito=deposito, stock=0)
        db.session.add(fila)
        db.session.flush()
    return fila


def registrar_movimiento(producto, cantidad, tipo, origen, destino, usuario) -> HistorialMovimiento:
    mov = HistorialMovimiento(
        producto=producto, cantidad=cantidad, tipo=tipo,
        origen=origen, destino=destino, usuario_id=usuario.id, fecha=ahora(),
    )
    db.session.add(mov)
    return mov


def _transaccion(funcion):
    """Decorador: commit si todo sale bien, rollback si hay cualquier error."""
    def envoltura(*args, **kwargs):
        try:
            resultado = funcion(*args, **kwargs)
            db.session.commit()
            return resultado
        except Exception:
            db.session.rollback()
            raise
    envoltura.__name__ = funcion.__name__
    envoltura.__doc__ = funcion.__doc__
    return envoltura


# ---------------------------------------------------------------------------
# Ventas (POS)
# ---------------------------------------------------------------------------

METODOS_PAGO = ("Efectivo", "Débito", "Crédito", "Mercado Pago")


@_transaccion
def registrar_venta(usuario, items, cliente_id=None, metodo_pago="Efectivo", envio=None) -> Venta:
    """Registra una venta: descuenta stock de góndola, suma puntos y crea el envío si corresponde.

    items: lista de {"producto_id": int, "cantidad": int}. El precio se toma de la base,
    nunca del navegador.
    envio: None o {"direccion": str, "telefono": str, "observaciones": str}
    """
    if not items:
        raise ErrorNegocio("El ticket está vacío.")
    if metodo_pago not in METODOS_PAGO:
        raise ErrorNegocio("Método de pago inválido.")

    # Agrupar por producto (si escanearon dos veces el mismo)
    cantidades: dict[int, int] = {}
    for item in items:
        producto = _producto(item.get("producto_id"))
        cantidades[producto.id] = cantidades.get(producto.id, 0) + _entero_positivo(item.get("cantidad"), "cantidad")

    cliente = None
    if cliente_id not in (None, "", 0, "0"):
        cliente = db.session.get(Cliente, int(cliente_id))
        if cliente is None:
            raise ErrorNegocio("El cliente no existe.")

    venta = Venta(usuario_id=usuario.id, cliente=cliente, metodo_pago=metodo_pago, fecha=ahora(),
                  tipo_entrega="envio" if envio else "mostrador")
    db.session.add(venta)

    total = 0.0
    for producto_id, cantidad in cantidades.items():
        producto = db.session.get(Producto, producto_id)
        if producto.estado_vencimiento == "vencido":
            raise ErrorNegocio(f"«{producto.nombre}» está vencido y no se puede vender.")
        if producto.stock_gondola < cantidad:
            raise ErrorNegocio(
                f"Stock insuficiente de «{producto.nombre}» en góndola (hay {producto.stock_gondola})."
            )
        subtotal = round(producto.precio * cantidad, 2)
        total += subtotal
        producto.stock_gondola -= cantidad
        venta.items.append(VentaProducto(producto=producto, cantidad=cantidad,
                                         precio_unitario=producto.precio, total=subtotal))
        registrar_movimiento(producto, cantidad, MOV_EGRESO_VENTA, GONDOLA, "Cliente (Venta)", usuario)

    venta.total = round(total, 2)

    if cliente:
        cliente.puntos += int(venta.total // current_app.config["PESOS_POR_PUNTO"])

    if envio:
        direccion = (envio.get("direccion") or "").strip() or (cliente.direccion if cliente else "")
        if not direccion:
            raise ErrorNegocio("Para un envío a domicilio hace falta la dirección.")
        venta.envio = Envio(
            direccion=direccion,
            telefono=(envio.get("telefono") or "").strip() or (cliente.telefono if cliente else None),
            observaciones=(envio.get("observaciones") or "").strip() or None,
            estado=ESTADOS_ENVIO[0],
            fecha_creacion=ahora(),
        )
    return venta


# ---------------------------------------------------------------------------
# Stock: traslados y mermas
# ---------------------------------------------------------------------------

@_transaccion
def trasladar_a_gondola(usuario, producto_id, cantidad) -> Producto:
    producto = _producto(producto_id)
    cantidad = _entero_positivo(cantidad, "cantidad")
    fila = stock_en_deposito(producto)
    if fila.stock < cantidad:
        raise ErrorNegocio(f"En depósito solo hay {fila.stock} unidades de «{producto.nombre}».")
    fila.stock -= cantidad
    producto.stock_gondola += cantidad
    registrar_movimiento(producto, cantidad, MOV_TRASLADO_GONDOLA, fila.deposito.nombre, GONDOLA, usuario)
    return producto


@_transaccion
def registrar_merma(usuario, producto_id, cantidad, motivo, ubicacion="gondola") -> BajaProducto:
    producto = _producto(producto_id)
    cantidad = _entero_positivo(cantidad, "cantidad")
    motivo = (motivo or "").strip()
    if not motivo:
        raise ErrorNegocio("Indicá el motivo de la merma.")
    if ubicacion not in ("gondola", "deposito"):
        raise ErrorNegocio("Ubicación inválida.")

    if ubicacion == "gondola":
        if producto.stock_gondola < cantidad:
            raise ErrorNegocio(f"En góndola solo hay {producto.stock_gondola} unidades.")
        producto.stock_gondola -= cantidad
        origen = GONDOLA
    else:
        fila = stock_en_deposito(producto)
        if fila.stock < cantidad:
            raise ErrorNegocio(f"En depósito solo hay {fila.stock} unidades.")
        fila.stock -= cantidad
        origen = fila.deposito.nombre

    baja = BajaProducto(producto=producto, cantidad=cantidad, motivo=motivo[:100],
                        usuario_id=usuario.id, ubicacion=ubicacion, fecha=ahora())
    db.session.add(baja)
    registrar_movimiento(producto, cantidad, MOV_BAJA_MERMA, origen, "Merma", usuario)
    return baja


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------

@_transaccion
def registrar_pedido_proveedor(usuario, proveedor_id, items) -> PedidoProveedor:
    """Registra la compra a un proveedor: entra stock al depósito y aumenta la deuda.

    items: lista de {"producto_id", "cantidad", "precio_unitario"}
    """
    proveedor = db.session.get(Proveedor, int(proveedor_id)) if str(proveedor_id).isdigit() else None
    if proveedor is None:
        raise ErrorNegocio("El proveedor no existe.")
    items = [i for i in items if str(i.get("cantidad") or "").strip() not in ("", "0")]
    if not items:
        raise ErrorNegocio("El pedido no tiene productos.")

    pedido = PedidoProveedor(usuario_id=usuario.id, proveedor=proveedor, fecha=ahora(), estado="Recibido")
    db.session.add(pedido)
    deposito = deposito_principal()
    total = 0.0

    for item in items:
        producto = _producto(item.get("producto_id"))
        if producto.proveedor_id != proveedor.id:
            raise ErrorNegocio(f"«{producto.nombre}» no es de {proveedor.nombre}.")
        cantidad = _entero_positivo(item.get("cantidad"), "cantidad")
        try:
            precio = float(item.get("precio_unitario"))
        except (TypeError, ValueError):
            raise ErrorNegocio("El precio unitario tiene que ser un número.")
        if precio < 0:
            raise ErrorNegocio("El precio unitario no puede ser negativo.")

        subtotal = round(precio * cantidad, 2)
        total += subtotal
        pedido.items.append(PedidoProveedorProducto(producto=producto, cantidad=cantidad,
                                                    precio_unitario=precio, total=subtotal))
        stock_en_deposito(producto).stock += cantidad
        registrar_movimiento(producto, cantidad, MOV_INGRESO_COMPRA, "Proveedor", deposito.nombre, usuario)

    pedido.total = round(total, 2)
    proveedor.deuda = round((proveedor.deuda or 0) + pedido.total, 2)
    return pedido


@_transaccion
def registrar_pago_proveedor(usuario, proveedor_id, monto, comprobante_key=None, observaciones=None) -> PagoProveedor:
    proveedor = db.session.get(Proveedor, int(proveedor_id)) if str(proveedor_id).isdigit() else None
    if proveedor is None:
        raise ErrorNegocio("El proveedor no existe.")
    try:
        monto = round(float(monto), 2)
    except (TypeError, ValueError):
        raise ErrorNegocio("El monto tiene que ser un número.")
    if monto <= 0:
        raise ErrorNegocio("El monto tiene que ser mayor a cero.")
    if monto > (proveedor.deuda or 0) + 0.001:
        raise ErrorNegocio(f"El pago supera la deuda actual (${proveedor.deuda:,.2f}).")

    pago = PagoProveedor(proveedor=proveedor, usuario_id=usuario.id, monto=monto, fecha=ahora(),
                         comprobante_key=comprobante_key, observaciones=(observaciones or "").strip() or None)
    db.session.add(pago)
    proveedor.deuda = round(proveedor.deuda - monto, 2)
    return pago


def ultimo_precio_compra(producto: Producto) -> float:
    """Último precio pagado al proveedor; si nunca se compró, 60% del precio de venta."""
    ultimo = (PedidoProveedorProducto.query
              .join(PedidoProveedor)
              .filter(PedidoProveedorProducto.producto_id == producto.id)
              .order_by(PedidoProveedor.fecha.desc())
              .first())
    return ultimo.precio_unitario if ultimo else round(producto.precio * 0.6, 2)


# ---------------------------------------------------------------------------
# Envíos
# ---------------------------------------------------------------------------

@_transaccion
def cambiar_estado_envio(envio_id, estado, repartidor=None) -> Envio:
    envio = db.session.get(Envio, int(envio_id)) if str(envio_id).isdigit() else None
    if envio is None:
        raise ErrorNegocio("El envío no existe.")
    if estado not in ESTADOS_ENVIO:
        raise ErrorNegocio("Estado de envío inválido.")
    if ESTADOS_ENVIO.index(estado) < ESTADOS_ENVIO.index(envio.estado):
        raise ErrorNegocio("Un envío no puede volver a un estado anterior.")
    envio.estado = estado
    if repartidor is not None and repartidor.strip():
        envio.repartidor = repartidor.strip()[:60]
    envio.fecha_entrega = ahora() if estado == "Entregado" else None
    return envio


# ---------------------------------------------------------------------------
# Reposición (los números los calcula Python; la LLM solo explica y prioriza)
# ---------------------------------------------------------------------------

def unidades_vendidas(desde: datetime) -> dict[int, int]:
    filas = (db.session.query(VentaProducto.producto_id, func.sum(VentaProducto.cantidad))
             .join(Venta)
             .filter(Venta.fecha >= desde)
             .group_by(VentaProducto.producto_id)
             .all())
    return {pid: int(total or 0) for pid, total in filas}


def calcular_candidatos_reposicion(dias_ventas: int = 30, dias_cobertura: int = 14) -> list[dict]:
    """Analiza el inventario y devuelve los productos que requieren acción.

    Acciones posibles:
        - "Reponer": pedir al proveedor (stock total bajo el mínimo o riesgo de quiebre).
        - "Trasladar": hay stock en depósito y la góndola está bajo el mínimo.
        - "Promocionar": vence en <= 7 días (no conviene reponer).
        - "Dar de baja": ya está vencido.
    """
    vendidas = unidades_vendidas(datetime.now() - timedelta(days=dias_ventas))
    alerta = current_app.config["DIAS_ALERTA_VENCIMIENTO"]
    candidatos = []

    for p in Producto.query.order_by(Producto.nombre).all():
        venta_diaria = vendidas.get(p.id, 0) / dias_ventas
        stock_total = p.stock_total
        dias = p.dias_para_vencer
        base = {
            "producto_id": p.id, "nombre": p.nombre, "sku": p.sku,
            "proveedor_id": p.proveedor_id, "proveedor": p.proveedor.nombre,
            "stock_gondola": p.stock_gondola, "stock_deposito": p.stock_deposito,
            "stock_minimo": p.stock_minimo, "vendidas": vendidas.get(p.id, 0),
            "venta_diaria": round(venta_diaria, 2), "dias_para_vencer": dias,
            "estado_vencimiento": p.estado_vencimiento,
            "precio_compra": ultimo_precio_compra(p), "cantidad_sugerida": 0,
        }

        if dias < 0:
            candidatos.append({**base, "accion": "Dar de baja", "prioridad": "alta",
                               "cantidad_sugerida": stock_total,
                               "motivo": f"Vencido hace {-dias} días: retirar de la venta y registrar merma."})
            continue
        if dias <= alerta:
            candidatos.append({**base, "accion": "Promocionar", "prioridad": "media",
                               "motivo": f"Vence en {dias} días: no reponer, conviene promocionarlo."})
            continue

        objetivo = max(p.stock_minimo * 2, math.ceil(venta_diaria * dias_cobertura))
        riesgo_quiebre = venta_diaria > 0 and stock_total < venta_diaria * 7

        if stock_total <= p.stock_minimo or riesgo_quiebre:
            cantidad = max(1, objetivo - stock_total)
            prioridad = "alta" if stock_total == 0 or p.stock_gondola == 0 else "media"
            cobertura = f"{stock_total / venta_diaria:.0f} días" if venta_diaria else "sin ventas recientes"
            candidatos.append({**base, "accion": "Reponer", "prioridad": prioridad, "cantidad_sugerida": cantidad,
                               "motivo": f"Stock total {stock_total} (mínimo {p.stock_minimo}), cobertura {cobertura}."})
        elif p.bajo_minimo and p.stock_deposito > 0:
            cantidad = min(p.stock_deposito, max(1, p.stock_minimo * 2 - p.stock_gondola))
            candidatos.append({**base, "accion": "Trasladar", "prioridad": "baja", "cantidad_sugerida": cantidad,
                               "motivo": f"Góndola con {p.stock_gondola} u. y depósito con {p.stock_deposito} u."})

    orden = {"alta": 0, "media": 1, "baja": 2}
    candidatos.sort(key=lambda c: (orden[c["prioridad"]], c["nombre"]))
    return candidatos


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def resumen_dashboard() -> dict:
    hoy = date.today()
    inicio_hoy = datetime.combine(hoy, datetime.min.time())
    alerta = current_app.config["DIAS_ALERTA_VENCIMIENTO"]

    ventas_hoy = Venta.query.filter(Venta.fecha >= inicio_hoy).all()
    productos = Producto.query.order_by(Producto.fecha_vencimiento).all()
    criticos = [p for p in productos if p.bajo_minimo or p.estado_vencimiento != "ok"]

    # Ventas de los últimos 7 días para el gráfico
    dias = [hoy - timedelta(days=i) for i in range(6, -1, -1)]
    por_dia = {d: 0.0 for d in dias}
    for v in Venta.query.filter(Venta.fecha >= datetime.combine(dias[0], datetime.min.time())).all():
        por_dia[v.fecha.date()] = por_dia.get(v.fecha.date(), 0) + (v.total or 0)
    maximo = max(por_dia.values()) or 1

    return {
        "ventas_hoy_total": sum(v.total for v in ventas_hoy),
        "ventas_hoy_cantidad": len(ventas_hoy),
        "ventas_totales": db.session.query(func.coalesce(func.sum(Venta.total), 0)).scalar(),
        "alertas_vencimiento": sum(1 for p in productos if 0 <= p.dias_para_vencer <= alerta),
        "vencidos": sum(1 for p in productos if p.dias_para_vencer < 0),
        "deuda_total": db.session.query(func.coalesce(func.sum(Proveedor.deuda), 0)).scalar(),
        "envios_pendientes": Envio.query.filter(Envio.estado != "Entregado").count(),
        "criticos": criticos[:8],
        "grafico": [{"dia": d.strftime("%d/%m"), "total": por_dia[d], "alto": int(por_dia[d] / maximo * 100)}
                    for d in dias],
        "ultimos_movimientos": HistorialMovimiento.query.order_by(HistorialMovimiento.fecha.desc()).limit(5).all(),
    }
