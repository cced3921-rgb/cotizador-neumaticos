"""Login por sesion y control de acceso por rol (admin / vendedor)."""
from functools import wraps

from flask import flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

import database


def verificar_password(usuario_fila, password_en_claro: str) -> bool:
    return bool(usuario_fila) and check_password_hash(usuario_fila["password_hash"], password_en_claro)


def cargar_usuario_actual():
    """Se llama una vez por request (before_request) y deja el usuario
    logueado disponible en g.usuario para el resto de la peticion."""
    g.usuario = None
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return
    conn = database.get_connection()
    try:
        fila = conn.execute(
            "SELECT * FROM usuarios WHERE id = ? AND activo = 1", (usuario_id,)
        ).fetchone()
    finally:
        conn.close()
    if fila:
        g.usuario = dict(fila)
    else:
        session.clear()  # el usuario fue desactivado o eliminado


def es_admin() -> bool:
    return bool(g.get("usuario")) and g.usuario["rol"] == "admin"


RUTAS_PUBLICAS = {
    "login",
    "configurar_admin",
    "static",
    "servir_pdf",
    "servir_imagen_procesada",
    "servir_imagen_original",
    "servir_logo",
}


def requiere_login(f):
    @wraps(f)
    def envoltura(*args, **kwargs):
        if not g.get("usuario"):
            return redirect(url_for("login", siguiente=request.path))
        return f(*args, **kwargs)

    return envoltura


def requiere_admin(f):
    @wraps(f)
    def envoltura(*args, **kwargs):
        if not g.get("usuario"):
            return redirect(url_for("login", siguiente=request.path))
        if not es_admin():
            flash("Esa seccion es solo para administradores.", "error")
            return redirect(url_for("cotizar"))
        return f(*args, **kwargs)

    return envoltura
