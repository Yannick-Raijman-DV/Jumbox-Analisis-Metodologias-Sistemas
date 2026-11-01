"""Instancias de las extensiones, vacías. Se inicializan en create_app().

Este módulo no importa nada de la app: es la base del flujo
routes -> services -> models -> extensions.
"""
from authlib.integrations.flask_client import OAuth
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
bcrypt = Bcrypt()
oauth = OAuth()
