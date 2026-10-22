from flask import Flask, jsonify, request, abort, render_template, redirect, url_for, flash, session
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, login_user, logout_user, login_required, current_user

from models import (
    db,
    Usuario,
    agregar_producto,
    obtener_todos,
    obtener_por_id,
    existe_codigo,
    eliminar_por_id,
    cargar_datos_iniciales,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "bd1xd"

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///supermercado.db"

db.init_app(app)
bcrypt = Bcrypt(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"  # a dónde redirige @login_required si no hay sesión
login_manager.login_message = "Tenés que iniciar sesión para ver esa página."


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))


with app.app_context():
    db.create_all()  # crea las tablas si no existen
    cargar_datos_iniciales()


# ---------- Validación ----------

def validar(datos, parcial=False):
    """Devuelve (datos_limpios, error). Si parcial=True (PUT) solo valida los campos presentes."""
    limpio = {}

    def requerido(campo):
        return not parcial or campo in datos

    if requerido("nombre"):
        nombre = str(datos.get("nombre", "")).strip()
        if not nombre:
            return None, "El campo 'nombre' es obligatorio"
        limpio["nombre"] = nombre

    if requerido("categoria"):
        categoria = str(datos.get("categoria", "")).strip()
        if not categoria:
            return None, "El campo 'categoria' es obligatorio"
        limpio["categoria"] = categoria

    if requerido("precio"):
        try:
            precio = float(datos.get("precio"))
        except (TypeError, ValueError):
            return None, "El campo 'precio' debe ser un número"
        if precio < 0:
            return None, "El precio no puede ser negativo"
        limpio["precio"] = precio

    if "stock" in datos or (not parcial):
        try:
            stock = int(datos.get("stock", 0) if datos.get("stock") not in (None, "") else 0)
        except (TypeError, ValueError):
            return None, "El campo 'stock' debe ser un número entero"
        if stock < 0:
            return None, "El stock no puede ser negativo"
        limpio["stock"] = stock

    if "codigo_barras" in datos:
        codigo = str(datos.get("codigo_barras") or "").strip()
        limpio["codigo_barras"] = codigo or None

    return limpio, None


# ---------- Vistas web ----------

@app.route("/")
def index():
    return render_template("index.html", productos=obtener_todos(), form={}, editando=None)


@app.route("/productos/nuevo", methods=["POST"])
def nuevo_producto():
    limpio, error = validar(request.form)
    if not error and existe_codigo(limpio.get("codigo_barras")):
        error = "Ya existe un producto con ese código de barras"

    if error:
        flash(error)
        return render_template("index.html", productos=obtener_todos(), form=request.form, editando=None)

    agregar_producto(**limpio)
    flash("Producto creado.")
    return redirect(url_for("index"))


@app.route("/productos/<int:producto_id>/editar", methods=["GET", "POST"])
def editar_producto(producto_id):
    producto = obtener_por_id(producto_id)
    if producto is None:
        abort(404, description="Producto no encontrado")

    if request.method == "POST":
        limpio, error = validar(request.form)
        if not error and existe_codigo(limpio.get("codigo_barras"), ignorar_id=producto_id):
            error = "Ya existe un producto con ese código de barras"

        if error:
            flash(error)
            return render_template("index.html", productos=obtener_todos(), form=request.form, editando=producto_id)

        for campo, valor in limpio.items():
            setattr(producto, campo, valor)
        db.session.commit()
        flash("Producto actualizado.")
        return redirect(url_for("index"))

    # GET: muestra el formulario con los datos actuales del producto
    return render_template("index.html", productos=obtener_todos(), form=producto.to_dict(), editando=producto_id)


@app.route("/productos/<int:producto_id>/eliminar", methods=["POST"])
def borrar_producto(producto_id):
    if eliminar_por_id(producto_id) is None:
        abort(404, description="Producto no encontrado")
    flash("Producto eliminado.")
    return redirect(url_for("index"))


@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            flash("Completá usuario y contraseña.")
        elif Usuario.query.filter_by(username=username).first():
            flash("Ese usuario ya existe.")
        else:
            hashed_pw = bcrypt.generate_password_hash(password).decode("utf-8")
            db.session.add(Usuario(username=username, password=hashed_pw, rol="admin"))
            db.session.commit()
            flash("Usuario creado, ya podés iniciar sesión.")
            return redirect(url_for("login"))

    return render_template("registro.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = Usuario.query.filter_by(username=request.form["username"]).first()

        # Comparamos la contraseña ingresada con el hash guardado en la BD
        if usuario and bcrypt.check_password_hash(usuario.password, request.form["password"]):
            login_user(usuario)  # guarda el id del usuario en la sesión
            session["username"] = usuario.username  # ejemplo de guardar un dato propio en session
            return redirect(url_for("protegido"))

        flash("Usuario o contraseña incorrectos.")

    return render_template("login.html")


@app.route("/protegido")
@login_required
def protegido():
    return render_template("protegido.html", usuario=current_user)


@app.route("/logout")
@login_required
def logout():
    logout_user()  # borra el usuario de la sesión
    session.pop("username", None)
    flash("Sesión cerrada.")
    return redirect(url_for("login"))


# ---------- API REST ----------

@app.route("/api/productos", methods=["GET"])
def listar_productos():
    return jsonify([p.to_dict() for p in obtener_todos()])


@app.route("/api/productos/<int:producto_id>", methods=["GET"])
def obtener_producto(producto_id):
    producto = obtener_por_id(producto_id)
    if producto is None:
        abort(404, description="Producto no encontrado")
    return jsonify(producto.to_dict())


@app.route("/api/productos", methods=["POST"])
def crear_producto():
    datos = request.get_json(silent=True)
    if not datos:
        abort(400, description="Se requiere un cuerpo JSON")

    limpio, error = validar(datos)
    if error:
        abort(400, description=error)
    if existe_codigo(limpio.get("codigo_barras")):
        abort(400, description="Ya existe un producto con ese código de barras")

    producto = agregar_producto(**limpio)
    return jsonify(producto.to_dict()), 201


@app.route("/api/productos/<int:producto_id>", methods=["PUT"])
def actualizar_producto(producto_id):
    producto = obtener_por_id(producto_id)
    if producto is None:
        abort(404, description="Producto no encontrado")

    datos = request.get_json(silent=True)
    if not datos:
        abort(400, description="Se requiere un cuerpo JSON con los campos a actualizar")

    limpio, error = validar(datos, parcial=True)
    if error:
        abort(400, description=error)
    if existe_codigo(limpio.get("codigo_barras"), ignorar_id=producto_id):
        abort(400, description="Ya existe un producto con ese código de barras")

    for campo, valor in limpio.items():
        setattr(producto, campo, valor)
    db.session.commit()  # sin commit el cambio no se guarda en la BD

    return jsonify(producto.to_dict())


@app.route("/api/productos/<int:producto_id>", methods=["DELETE"])
def eliminar_producto(producto_id):
    producto = eliminar_por_id(producto_id)
    if producto is None:
        abort(404, description="Producto no encontrado")
    return "", 204


@app.errorhandler(404)
def no_encontrado(error):
    return jsonify({"error": str(error.description)}), 404


@app.errorhandler(400)
def solicitud_invalida(error):
    return jsonify({"error": str(error.description)}), 400


if __name__ == "__main__":
    app.run(debug=True, port=5000)
