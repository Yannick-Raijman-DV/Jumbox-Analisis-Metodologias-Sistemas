"""Blueprint de Stock e Inventario: productos (con foto en S3), semáforo, traslados, mermas y movimientos."""
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..decorators import rol_requerido
from ..extensions import db
from ..models import (
    MOTIVOS_MERMA,
    ROL_ADMIN,
    ROL_OPERARIO,
    BajaProducto,
    Categoria,
    HistorialMovimiento,
    Producto,
    Proveedor,
)
from ..services import s3
from ..services.inventario import (
    ErrorNegocio,
    registrar_merma,
    stock_en_deposito,
    trasladar_a_gondola,
)

bp = Blueprint("stock", __name__)


@bp.route("/")
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def index():
    filtro = request.args.get("filtro", "todos")
    categoria_id = request.args.get("categoria_id", type=int)

    consulta = Producto.query
    if categoria_id:
        consulta = consulta.filter_by(categoria_id=categoria_id)
    productos = consulta.order_by(Producto.fecha_vencimiento).all()

    if filtro == "vencidos":
        productos = [p for p in productos if p.estado_vencimiento == "vencido"]
    elif filtro == "por_vencer":
        productos = [p for p in productos if p.estado_vencimiento == "por_vencer"]
    elif filtro == "bajo_stock":
        productos = [p for p in productos if p.bajo_minimo]

    categorias = Categoria.query.order_by(Categoria.nombre).all()
    todos = Producto.query.all()
    contadores = {
        "todos": len(todos),
        "vencidos": sum(1 for p in todos if p.estado_vencimiento == "vencido"),
        "por_vencer": sum(1 for p in todos if p.estado_vencimiento == "por_vencer"),
        "bajo_stock": sum(1 for p in todos if p.bajo_minimo),
    }
    return render_template(
        "stock/stock.html",
        productos=productos,
        categorias=categorias,
        filtro=filtro,
        categoria_id=categoria_id,
        contadores=contadores,
        motivos_merma=MOTIVOS_MERMA,
    )


def _leer_form_producto(producto_actual: Producto | None = None):
    nombre = (request.form.get("nombre") or "").strip()
    codigo_barras = (request.form.get("codigo_barras") or "").strip() or None
    if not nombre:
        return None, "El nombre del producto es obligatorio."

    try:
        precio = float(request.form.get("precio") or 0)
        stock_gondola = int(request.form.get("stock_gondola") or 0)
        stock_deposito = int(request.form.get("stock_deposito") or 0)
        stock_minimo = int(request.form.get("stock_minimo") or 1)
        categoria_id = int(request.form.get("categoria_id") or 0)
        proveedor_id = int(request.form.get("proveedor_id") or 0)
        fecha_venc = date.fromisoformat(request.form.get("fecha_vencimiento") or "")
    except ValueError:
        return None, "Revisá que precio, stocks y fecha de vencimiento sean válidos."

    if precio < 0 or stock_gondola < 0 or stock_deposito < 0 or stock_minimo < 0:
        return None, "Los precios y cantidades no pueden ser negativos."

    if codigo_barras:
        existente = Producto.query.filter_by(codigo_barras=codigo_barras).first()
        if existente and (producto_actual is None or existente.id != producto_actual.id):
            return None, "Ya existe otro producto con ese código de barras."

    foto = request.files.get("foto")
    if foto and foto.filename and not s3.extension_valida(foto.filename):
        return None, "La foto debe ser una imagen (.jpg, .png, .webp o .gif)."

    return {
        "nombre": nombre[:45],
        "precio": round(precio, 2),
        "stock_gondola": stock_gondola,
        "stock_deposito": stock_deposito,
        "stock_minimo": stock_minimo,
        "fecha_vencimiento": fecha_venc,
        "codigo_barras": codigo_barras,
        "categoria_id": categoria_id,
        "proveedor_id": proveedor_id,
    }, None


def _guardar_foto_si_vino(producto: Producto):
    foto = request.files.get("foto")
    if not foto or not foto.filename:
        return
    anterior = producto.foto_key
    producto.foto_key = s3.subir_archivo(foto, foto.filename, carpeta="productos")
    db.session.commit()
    if anterior and not anterior.startswith("productos/seed/"):
        s3.borrar(anterior)


@bp.route("/productos/nuevo", methods=["GET", "POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def nuevo_producto():
    categorias = Categoria.query.order_by(Categoria.nombre).all()
    proveedores = Proveedor.query.order_by(Proveedor.nombre).all()

    if request.method == "POST":
        datos, error = _leer_form_producto()
        if error:
            flash(error, "error")
        else:
            stock_dep = datos.pop("stock_deposito")
            producto = Producto(**datos)
            db.session.add(producto)
            db.session.flush()
            stock_en_deposito(producto).stock = stock_dep
            db.session.commit()
            _guardar_foto_si_vino(producto)
            flash(f"Producto «{producto.nombre}» creado.", "success")
            return redirect(url_for("stock.index"))

    return render_template("stock/producto_form.html", producto=None, categorias=categorias, proveedores=proveedores)


@bp.route("/productos/<int:producto_id>/editar", methods=["GET", "POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def editar_producto(producto_id: int):
    producto = db.session.get(Producto, producto_id)
    if producto is None:
        abort(404)

    categorias = Categoria.query.order_by(Categoria.nombre).all()
    proveedores = Proveedor.query.order_by(Proveedor.nombre).all()

    if request.method == "POST":
        datos, error = _leer_form_producto(producto_actual=producto)
        if error:
            flash(error, "error")
        else:
            stock_dep = datos.pop("stock_deposito")
            for campo, valor in datos.items():
                setattr(producto, campo, valor)
            stock_en_deposito(producto).stock = stock_dep
            db.session.commit()
            _guardar_foto_si_vino(producto)
            flash(f"Producto «{producto.nombre}» actualizado.", "success")
            return redirect(url_for("stock.index"))

    return render_template("stock/producto_form.html", producto=producto, categorias=categorias, proveedores=proveedores)


@bp.route("/categorias/nueva", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def nueva_categoria():
    nombre = (request.form.get("nombre_categoria") or "").strip()
    if not nombre:
        flash("Indicá el nombre de la categoría.", "error")
    elif Categoria.query.filter_by(nombre=nombre).first():
        flash("Esa categoría ya existe.", "warning")
    else:
        db.session.add(Categoria(nombre=nombre[:45]))
        db.session.commit()
        flash(f"Categoría «{nombre}» creada.", "success")
    return redirect(request.referrer or url_for("stock.index"))


@bp.route("/traslado", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def traslado():
    try:
        prod = trasladar_a_gondola(
            usuario=current_user,
            producto_id=request.form.get("producto_id"),
            cantidad=request.form.get("cantidad"),
        )
        flash(f"Se trasladaron unidades de «{prod.nombre}» del depósito a la góndola.", "success")
    except ErrorNegocio as err:
        flash(str(err), "error")
    return redirect(request.referrer or url_for("stock.index"))


@bp.route("/mermas", methods=["GET", "POST"])
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def mermas():
    if request.method == "POST":
        try:
            baja = registrar_merma(
                usuario=current_user,
                producto_id=request.form.get("producto_id"),
                cantidad=request.form.get("cantidad"),
                motivo=request.form.get("motivo"),
                ubicacion=request.form.get("ubicacion") or "gondola",
            )
            flash(f"Merma registrada: {baja.cantidad} u. de «{baja.producto.nombre}» ({baja.motivo}).", "success")
            return redirect(url_for("stock.mermas"))
        except ErrorNegocio as err:
            flash(str(err), "error")

    bajas = BajaProducto.query.order_by(BajaProducto.fecha.desc()).all()
    productos = Producto.query.order_by(Producto.nombre).all()
    return render_template("stock/mermas.html", bajas=bajas, productos=productos, motivos_merma=MOTIVOS_MERMA)


@bp.route("/movimientos")
@rol_requerido(ROL_ADMIN, ROL_OPERARIO)
def movimientos():
    tipo = request.args.get("tipo", "")
    consulta = HistorialMovimiento.query
    if tipo:
        consulta = consulta.filter_by(tipo=tipo)
    items = consulta.order_by(HistorialMovimiento.fecha.desc()).limit(100).all()
    return render_template("stock/movimientos.html", movimientos=items, tipo=tipo)
