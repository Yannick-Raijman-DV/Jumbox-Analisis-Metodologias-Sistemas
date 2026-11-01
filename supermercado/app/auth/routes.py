"""Blueprint de autenticación: login local (Bcrypt), Google OAuth y elección de rol."""
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..decorators import rol_requerido
from ..extensions import bcrypt, db, oauth
from ..models import ROL_ADMIN, ROLES_AUTOREGISTRO, RolUsuario, Usuario

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))

    if request.method == "POST":
        identificador = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        usuario = (
            Usuario.query.filter(
                (Usuario.nombre == identificador) | (Usuario.correo == identificador)
            ).first()
        )
        if usuario and usuario.contrasena and bcrypt.check_password_hash(usuario.contrasena, password):
            login_user(usuario)
            session.pop("google_pendiente", None)
            flash(f"Bienvenido/a, {usuario.nombre} ({usuario.rol_nombre}).", "success")
            destino = request.args.get("next")
            if destino and destino.startswith("/"):
                return redirect(destino)
            return redirect(url_for("main.index"))

        flash("Usuario/correo o contraseña incorrectos.", "error")

    return render_template("auth/login.html")


@bp.route("/auth/google")
def login_google():
    google = oauth.create_client("google")
    return google.authorize_redirect(url_for("auth.callback_google", _external=True))


@bp.route("/auth/google/callback")
def callback_google():
    google = oauth.create_client("google")
    try:
        token = google.authorize_access_token()
    except Exception:
        flash("No se pudo iniciar sesión con Google.", "error")
        return redirect(url_for("auth.login"))

    info = token.get("userinfo") or {}
    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    nombre = (info.get("name") or email.split("@")[0] or "usuario")[:45]
    foto = info.get("picture")

    if not sub or not email:
        flash("Google no devolvió los datos necesarios de la cuenta.", "error")
        return redirect(url_for("auth.login"))

    # 1. ¿Ya tiene google_id vinculado?
    usuario = Usuario.query.filter_by(google_id=sub).first()
    # 2. ¿Existe una cuenta con ese email? La vinculamos.
    if usuario is None:
        usuario = Usuario.query.filter(db.func.lower(Usuario.correo) == email).first()
        if usuario is not None:
            usuario.google_id = sub
            usuario.foto_url = foto
            db.session.commit()

    if usuario is not None:
        login_user(usuario)
        flash(f"Sesión iniciada con Google como {usuario.nombre} ({usuario.rol_nombre}).", "success")
        return redirect(url_for("main.index"))

    # 3. Cuenta nueva: pedirle que elija rol entre Operario y Cajero
    session["google_pendiente"] = {
        "sub": sub,
        "email": email,
        "nombre": nombre,
        "foto": foto,
    }
    return redirect(url_for("auth.elegir_rol"))


@bp.route("/auth/elegir-rol", methods=["GET", "POST"])
def elegir_rol():
    pendiente = session.get("google_pendiente")
    if not pendiente:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        rol_elegido = (request.form.get("rol") or "").strip()
        if rol_elegido not in ROLES_AUTOREGISTRO:
            flash("Seleccioná un rol válido (Operario o Cajero).", "error")
            return render_template("auth/elegir_rol.html", pendiente=pendiente, roles=ROLES_AUTOREGISTRO), 400

        rol_obj = RolUsuario.query.filter_by(nombre=rol_elegido).first()
        if rol_obj is None:
            rol_obj = RolUsuario(nombre=rol_elegido)
            db.session.add(rol_obj)
            db.session.flush()

        usuario = Usuario(
            nombre=pendiente["nombre"],
            correo=pendiente["email"],
            rol_id=rol_obj.id,
            google_id=pendiente["sub"],
            foto_url=pendiente.get("foto"),
        )
        db.session.add(usuario)
        db.session.commit()

        session.pop("google_pendiente", None)
        login_user(usuario)
        flash(f"Cuenta creada con rol {rol_elegido}. ¡Bienvenido/a a Jumbox!", "success")
        return redirect(url_for("main.index"))

    return render_template("auth/elegir_rol.html", pendiente=pendiente, roles=ROLES_AUTOREGISTRO)


@bp.route("/usuarios", methods=["GET", "POST"])
@rol_requerido(ROL_ADMIN)
def gestionar_usuarios():
    roles = RolUsuario.query.order_by(RolUsuario.id).all()
    if request.method == "POST":
        nombre = (request.form.get("nombre") or "").strip()
        correo = (request.form.get("correo") or "").strip().lower()
        password = request.form.get("password") or ""
        rol_id = request.form.get("rol_id")

        if not nombre or not correo or not password or not rol_id:
            flash("Completá nombre, correo, contraseña y rol.", "error")
        elif Usuario.query.filter((Usuario.correo == correo) | (Usuario.nombre == nombre)).first():
            flash("Ya existe un usuario con ese nombre o correo.", "error")
        else:
            hashed = bcrypt.generate_password_hash(password).decode("utf-8")
            db.session.add(Usuario(nombre=nombre[:45], correo=correo[:120], contrasena=hashed, rol_id=int(rol_id)))
            db.session.commit()
            flash(f"Usuario «{nombre}» creado.", "success")
            return redirect(url_for("auth.gestionar_usuarios"))

    usuarios = Usuario.query.order_by(Usuario.id).all()
    return render_template("auth/usuarios.html", usuarios=usuarios, roles=roles)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("google_pendiente", None)
    flash("Sesión cerrada correctamente.", "info")
    return redirect(url_for("auth.login"))
