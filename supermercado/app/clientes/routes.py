"""Blueprint de Clientes y Fidelización (Puntos)."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..decorators import rol_requerido
from ..extensions import db
from ..models import ROL_ADMIN, ROL_CAJERO, Cliente

bp = Blueprint("clientes", __name__)


@bp.route("/", methods=["GET", "POST"])
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def index():
    if request.method == "POST":
        nombre = (request.form.get("nombre") or "").strip()
        apellido = (request.form.get("apellido") or "").strip()
        dni = (request.form.get("dni") or "").strip()
        telefono = (request.form.get("telefono") or "").strip() or None
        direccion = (request.form.get("direccion") or "").strip() or None

        if not nombre or not apellido or not dni:
            flash("Nombre, apellido y DNI son obligatorios.", "error")
        elif Cliente.query.filter_by(dni=dni).first():
            flash("Ya existe un cliente registrado con ese DNI.", "error")
        else:
            db.session.add(
                Cliente(
                    nombre=nombre[:45],
                    apellido=apellido[:45],
                    dni=dni[:45],
                    puntos=0,
                    telefono=telefono,
                    direccion=direccion,
                )
            )
            db.session.commit()
            flash(f"Cliente «{nombre} {apellido}» registrado.", "success")
            return redirect(url_for("clientes.index"))

    q = (request.args.get("q") or "").strip()
    consulta = Cliente.query
    if q:
        patron = f"%{q}%"
        consulta = consulta.filter(
            (Cliente.nombre.ilike(patron))
            | (Cliente.apellido.ilike(patron))
            | (Cliente.dni.ilike(patron))
        )
    clientes = consulta.order_by(Cliente.puntos.desc(), Cliente.nombre).all()
    return render_template("clientes/clientes.html", clientes=clientes, q=q)


@bp.route("/<int:cliente_id>/editar", methods=["POST"])
@rol_requerido(ROL_ADMIN, ROL_CAJERO)
def editar(cliente_id: int):
    cliente = db.session.get(Cliente, cliente_id)
    if cliente is None:
        abort(404)

    nombre = (request.form.get("nombre") or "").strip()
    apellido = (request.form.get("apellido") or "").strip()
    dni = (request.form.get("dni") or "").strip()
    if not nombre or not apellido or not dni:
        flash("Nombre, apellido y DNI son obligatorios.", "error")
        return redirect(url_for("clientes.index"))

    otro = Cliente.query.filter(Cliente.dni == dni, Cliente.id != cliente.id).first()
    if otro:
        flash("Otro cliente ya tiene ese DNI.", "error")
        return redirect(url_for("clientes.index"))

    cliente.nombre = nombre[:45]
    cliente.apellido = apellido[:45]
    cliente.dni = dni[:45]
    cliente.telefono = (request.form.get("telefono") or "").strip() or None
    cliente.direccion = (request.form.get("direccion") or "").strip() or None
    if request.form.get("puntos") not in (None, ""):
        try:
            cliente.puntos = max(0, int(request.form["puntos"]))
        except ValueError:
            pass
    db.session.commit()
    flash(f"Datos de «{cliente.nombre_completo}» actualizados.", "success")
    return redirect(url_for("clientes.index"))
