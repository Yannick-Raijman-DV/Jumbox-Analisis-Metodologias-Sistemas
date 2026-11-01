"""Decorador para restringir rutas según el rol del usuario autenticado."""
from functools import wraps

from flask import abort
from flask_login import current_user, login_required


def rol_requerido(*roles_permitidos: str):
    """Exige sesión activa y que el usuario tenga alguno de los roles indicados."""
    def decorador(funcion):
        @wraps(funcion)
        @login_required
        def envoltura(*args, **kwargs):
            if not current_user.tiene_rol(*roles_permitidos):
                abort(403)
            return funcion(*args, **kwargs)
        return envoltura
    return decorador
