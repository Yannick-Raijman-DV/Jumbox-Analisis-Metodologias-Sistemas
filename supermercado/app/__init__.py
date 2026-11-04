"""Application Factory de Jumbox."""
from pathlib import Path

import click
from flask import Flask, flash, redirect, render_template, request, url_for
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config

from .extensions import bcrypt, db, login_manager, oauth
from .models import Envio, Usuario
from .seed import cargar_seed, seed_si_vacia
from .services import llm, s3


@event.listens_for(Engine, "connect")
def _habilitar_fk_sqlite(dbapi_connection, _connection_record):
    """Activa integridad referencial real en SQLite."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    finally:
        cursor.close()


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Render está detrás de un proxy HTTPS: necesario para que url_for(_external=True) genere https://
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Asegurar que exista la carpeta instance/ cuando se usa SQLite en archivo
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite:///") and ":memory:" not in uri:
        ruta_db = Path(uri.replace("sqlite:///", ""))
        ruta_db.parent.mkdir(parents=True, exist_ok=True)

    # Extensiones
    db.init_app(app)
    bcrypt.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Tenés que iniciar sesión para acceder a Jumbox."
    login_manager.login_message_category = "warning"

    oauth.init_app(app)
    if "google" not in oauth._clients:
        oauth.register(
            name="google",
            client_id=app.config.get("GOOGLE_CLIENT_ID"),
            client_secret=app.config.get("GOOGLE_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(Usuario, int(user_id)) if str(user_id).isdigit() else None

    # Registro de Blueprints
    from .auth.routes import bp as auth_bp
    from .clientes.routes import bp as clientes_bp
    from .ia.routes import bp as ia_bp
    from .logistica.routes import bp as logistica_bp
    from .main.routes import bp as main_bp
    from .pos.routes import bp as pos_bp
    from .proveedores.routes import bp as proveedores_bp
    from .stock.routes import bp as stock_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(pos_bp, url_prefix="/pos")
    app.register_blueprint(stock_bp, url_prefix="/stock")
    app.register_blueprint(logistica_bp, url_prefix="/envios")
    app.register_blueprint(clientes_bp, url_prefix="/clientes")
    app.register_blueprint(proveedores_bp, url_prefix="/proveedores")
    app.register_blueprint(ia_bp, url_prefix="/ia")

    # Context processor para todos los templates
    @app.context_processor
    def utilidades_plantillas():
        try:
            envios_activos = Envio.query.filter(Envio.estado != "Entregado").count()
        except Exception:
            envios_activos = 0
        return {
            "url_imagen": s3.url_prefirmada,
            "usa_s3": s3.usa_s3(),
            "estado_llm": llm.estado(),
            "envios_activos": envios_activos,
        }

    # Manejadores de errores
    @app.errorhandler(403)
    def error_403(_error):
        return render_template("partials/error.html", codigo=403, titulo="Acceso restringido",
                               mensaje="Tu rol no tiene permisos para acceder a este módulo."), 403

    @app.errorhandler(404)
    def error_404(_error):
        return render_template("partials/error.html", codigo=404, titulo="Página no encontrada",
                               mensaje="El recurso que buscás no existe o fue movido."), 404

    @app.errorhandler(413)
    def error_413(_error):
        flash("El archivo supera el límite máximo permitido de 5 MB.", "error")
        return redirect(request.referrer or url_for("main.index"))

    # CLI comando: flask --app run seed [--reset]
    @app.cli.command("seed")
    @click.option("--reset", is_flag=True, help="Recrea todas las tablas antes de sembrar.")
    def comando_seed(reset: bool):
        info = cargar_seed(reset=reset)
        click.echo(f"Seed ejecutado: {info}")

    # CLI comando: flask --app run s3-check (diagnóstico del bucket de AWS S3)
    from .cli import registrar_comandos
    registrar_comandos(app)

    # Crear tablas y sembrar si está vacía
    with app.app_context():
        db.create_all()
        if app.config.get("SEED_AUTOMATICO", True):
            seed_si_vacia()

    return app
