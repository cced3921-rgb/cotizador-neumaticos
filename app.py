"""Cotizador de Neumaticos - app local para armar y enviar cotizaciones por
WhatsApp en el menor numero de clics posible, con catalogo, generacion
automatica de imagenes y un CRM simple de contactos."""
import os
import socket
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, flash, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename

import database
from utils import auth
from utils import importar_catalogo as importador
from utils import importar_contactos as importador_contactos
from utils import imagenes as img_utils
from utils import pdf_cotizacion
from utils import portapapeles
from utils import whatsapp

BASE_DIR = Path(__file__).resolve().parent
EXTENSIONES_IMAGEN = {".jpg", ".jpeg", ".png", ".webp"}
TASA_IVA = 0.15


def calcular_totales(total_con_iva: float, iva_manual=None):
    """A partir del total de catalogo (precios ya incluyen IVA), calcula el
    subtotal (sin IVA) y el IVA. Si se pasa iva_manual, ese monto reemplaza
    el 15% automatico (por ejemplo un cliente exento o un ajuste puntual);
    el total final siempre es subtotal + iva."""
    subtotal = round(total_con_iva / (1 + TASA_IVA), 2)
    iva_automatico = round(total_con_iva - subtotal, 2)
    es_manual = iva_manual is not None
    iva = round(float(iva_manual), 2) if es_manual else iva_automatico
    total_final = round(subtotal + iva, 2)
    return {"subtotal": subtotal, "iva": iva, "total": total_final, "iva_manual": es_manual}

app = Flask(__name__)
# En local usa una clave fija (no importa, nadie mas la ve). En un servidor
# hay que definir SECRET_KEY como variable de entorno con un valor propio y
# secreto: si quedara la clave fija, cualquiera podria falsificar sesiones.
app.secret_key = os.environ.get("SECRET_KEY", "cotizador-neumaticos-local")
# Exige que la cookie de sesion solo viaje por HTTPS cuando el sitio se sirve
# por HTTPS (Render, el VPS detras de nginx con certificado, etc.). En local
# (HTTP simple) no se exige, o el navegador la descartaria. RENDER lo define
# automaticamente; en un VPS propio hay que definir FORZAR_COOKIE_SEGURA=1
# una vez que el dominio tenga su certificado funcionando.
app.config["SESSION_COOKIE_SECURE"] = bool(
    os.environ.get("RENDER") or os.environ.get("FORZAR_COOKIE_SEGURA")
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Limite de tamaño de subida (imagenes de producto, logo, Excel/CSV de
# importacion): evita que una subida enorme llene el disco por error o abuso.
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB

database.init_db()


# ------------------------------------------------------------ autenticacion --
_configuracion_lista = False


def _falta_configuracion_inicial() -> bool:
    """True si todavia no existe ningun usuario (primer arranque del
    sistema). Se cachea en memoria para no consultar la base de datos en
    cada request una vez que ya existe al menos un administrador."""
    global _configuracion_lista
    if _configuracion_lista:
        return False
    conn = database.get_connection()
    try:
        hay_usuarios = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0
    finally:
        conn.close()
    if hay_usuarios:
        _configuracion_lista = True
    return not hay_usuarios


@app.route("/configurar", methods=["GET", "POST"])
def configurar_admin():
    """Primer arranque: no hay ningun usuario todavia, asi que en vez de
    traer una contraseña de administrador predefinida, se le pide a quien
    abre el sistema por primera vez que cree su propia cuenta."""
    if not _falta_configuracion_inicial():
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("configurar_admin.html")

    usuario_input = request.form.get("usuario", "").strip().lower()
    password_input = request.form.get("password", "")
    password_confirmar = request.form.get("password_confirmar", "")
    nombre = request.form.get("nombre", "").strip()

    errores = []
    if not usuario_input:
        errores.append("Falta el usuario con el que vas a iniciar sesion.")
    if not nombre:
        errores.append("Falta tu nombre.")
    if len(password_input) < 4:
        errores.append("La contraseña debe tener al menos 4 caracteres.")
    if password_input != password_confirmar:
        errores.append("Las contraseñas no coinciden.")

    if errores:
        for error in errores:
            flash(error, "error")
        return render_template("configurar_admin.html", usuario=usuario_input, nombre=nombre)

    conn = database.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO usuarios (usuario, password_hash, nombre, rol, activo, creado_en) "
            "VALUES (?, ?, ?, 'admin', 1, ?)",
            (usuario_input, generate_password_hash(password_input), nombre, database.ahora_iso()),
        )
        admin_id = cursor.lastrowid
        # Contactos que ya existian de antes de que hubiera login quedan
        # asignados a esta primera cuenta de administrador.
        conn.execute("UPDATE contactos SET vendedor_id = ? WHERE vendedor_id IS NULL", (admin_id,))
        conn.commit()
    finally:
        conn.close()

    global _configuracion_lista
    _configuracion_lista = True

    session.clear()
    session["usuario_id"] = admin_id
    session.permanent = True
    flash(f"Cuenta de administrador '{usuario_input}' creada.", "ok")
    return redirect(url_for("dashboard"))


@app.errorhandler(413)
def _archivo_demasiado_grande(error):
    flash("El archivo es demasiado grande (maximo 15 MB).", "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.before_request
def _autenticacion():
    if request.endpoint not in ("configurar_admin", "static") and _falta_configuracion_inicial():
        return redirect(url_for("configurar_admin"))
    auth.cargar_usuario_actual()
    if request.endpoint is None or request.endpoint in auth.RUTAS_PUBLICAS:
        return
    if not g.get("usuario"):
        return redirect(url_for("login", siguiente=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("usuario"):
        return redirect(url_for("dashboard"))
    if request.method == "GET":
        return render_template("login.html")

    usuario_input = request.form.get("usuario", "").strip().lower()
    password_input = request.form.get("password", "")
    conn = database.get_connection()
    try:
        fila = conn.execute(
            "SELECT * FROM usuarios WHERE usuario = ? AND activo = 1", (usuario_input,)
        ).fetchone()
    finally:
        conn.close()

    if not auth.verificar_password(fila, password_input):
        flash("Usuario o contraseña incorrectos.", "error")
        return render_template("login.html", usuario=usuario_input)

    session.clear()
    session["usuario_id"] = fila["id"]
    session.permanent = True

    # g.usuario todavia no existe en este request (recien se acaba de loguear),
    # asi que se registra directo con los datos de "fila" en vez de usar el
    # helper registrar_actividad (que toma el usuario de g).
    conn = database.get_connection()
    try:
        conn.execute(
            "INSERT INTO actividad (usuario_id, usuario_nombre, accion, descripcion, fecha) "
            "VALUES (?, ?, 'login', ?, ?)",
            (fila["id"], fila["nombre"], f"{fila['nombre']} inicio sesion", database.ahora_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    siguiente = request.args.get("siguiente") or url_for("dashboard")
    return redirect(siguiente)


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesion cerrada.", "ok")
    return redirect(url_for("login"))


@app.route("/mi-cuenta", methods=["GET", "POST"])
def mi_cuenta():
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        password_nueva = request.form.get("password_nueva", "")
        conn = database.get_connection()
        try:
            if not nombre:
                flash("El nombre no puede quedar vacio.", "error")
            else:
                if password_nueva:
                    conn.execute(
                        "UPDATE usuarios SET nombre = ?, password_hash = ? WHERE id = ?",
                        (nombre, generate_password_hash(password_nueva), g.usuario["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE usuarios SET nombre = ? WHERE id = ?", (nombre, g.usuario["id"])
                    )
                conn.commit()
                flash("Datos actualizados.", "ok")
        finally:
            conn.close()
        return redirect(url_for("mi_cuenta"))
    return render_template("mi_cuenta.html")


# ---------------------------------------------------------------- helpers --
def obtener_ajustes():
    return database.obtener_ajustes()


@app.context_processor
def inyectar_ajustes():
    return {
        "ajustes": obtener_ajustes(),
        "usuario_actual": g.get("usuario"),
        "es_admin": auth.es_admin(),
    }


def regenerar_imagen_producto(conn, producto_id):
    """Vuelve a generar la imagen 'lista para enviar' de un producto usando
    su foto original y los ajustes actuales del negocio (nombre/logo)."""
    fila = conn.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
    if not fila or not fila["imagen_original"]:
        return None
    ajustes = obtener_ajustes()
    ruta_origen = database.IMAGENES_DIR / fila["imagen_original"]
    if not ruta_origen.exists():
        return None
    nombre_destino = f"{producto_id}_{fila['imagen_original']}"
    ruta_destino = database.PROCESADAS_DIR / nombre_destino
    ruta_logo = None
    if ajustes.get("logo_archivo"):
        posible = database.LOGO_DIR / ajustes["logo_archivo"]
        if posible.exists():
            ruta_logo = posible
    img_utils.generar_imagen_lista(
        ruta_origen,
        ruta_destino,
        fila["marca"],
        fila["modelo"],
        fila["medida"],
        fila["precio"],
        ajustes.get("nombre_negocio", ""),
        ruta_logo,
    )
    conn.execute(
        "UPDATE productos SET imagen_procesada = ? WHERE id = ?",
        (nombre_destino, producto_id),
    )
    conn.commit()
    return nombre_destino


# -------------------------------------------------------------- imagenes --
@app.route("/img/procesada/<path:nombre>")
def servir_imagen_procesada(nombre):
    return send_from_directory(database.PROCESADAS_DIR, nombre)


@app.route("/img/original/<path:nombre>")
def servir_imagen_original(nombre):
    return send_from_directory(database.IMAGENES_DIR, nombre)


@app.route("/img/logo/<path:nombre>")
def servir_logo(nombre):
    return send_from_directory(database.LOGO_DIR, nombre)


@app.route("/pdf/<path:nombre>")
def servir_pdf(nombre):
    return send_from_directory(database.COTIZACIONES_DIR, nombre)


# --------------------------------------------------------------- catalogo --
# Secciones fijas que siempre aparecen como pestaña, aunque todavia no tengan
# ningun producto cargado (para que se pueda empezar a cargar aros/aceites
# desde el primer momento). Cualquier otro valor que se use en "categoria"
# (al cargar un producto o al importar un Excel) aparece como pestaña nueva
# automaticamente, sin tener que tocar el codigo.
CATEGORIAS_BASE = ["llantas", "aros", "aceites"]


def _categorias_existentes(conn):
    filas = conn.execute(
        "SELECT DISTINCT categoria FROM productos WHERE activo = 1 AND categoria != ''"
    ).fetchall()
    extra = sorted({f["categoria"] for f in filas if f["categoria"] not in CATEGORIAS_BASE})
    return CATEGORIAS_BASE + extra


POR_PAGINA_CATALOGO = 60


@app.route("/catalogo")
def catalogo():
    q = request.args.get("q", "").strip()
    categoria = request.args.get("categoria", "").strip().lower()
    try:
        pagina = max(1, int(request.args.get("pagina", 1)))
    except (TypeError, ValueError):
        pagina = 1
    conn = database.get_connection()
    try:
        categorias = _categorias_existentes(conn)
        if categoria not in categorias:
            categoria = ""
        base_sql = "FROM productos WHERE activo = 1"
        params = []
        if categoria:
            base_sql += " AND categoria = ?"
            params.append(categoria)
        if q:
            like = f"%{q}%"
            base_sql += " AND (marca LIKE ? OR modelo LIKE ? OR medida LIKE ?)"
            params.extend([like, like, like])

        total_productos = conn.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()[0]
        total_paginas = max(1, -(-total_productos // POR_PAGINA_CATALOGO))  # division hacia arriba
        pagina = min(pagina, total_paginas)

        productos = conn.execute(
            f"SELECT * {base_sql} ORDER BY orden LIMIT ? OFFSET ?",
            [*params, POR_PAGINA_CATALOGO, (pagina - 1) * POR_PAGINA_CATALOGO],
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "catalogo.html",
        productos=productos,
        q=q,
        categoria=categoria,
        categorias=categorias,
        pagina=pagina,
        total_paginas=total_paginas,
        total_productos=total_productos,
    )


def _categoria_desde_form():
    """La categoria viene del <select>; si se elige "Otra" se usa lo escrito
    en el campo de texto que aparece al lado. Se normaliza a minusculas para
    que no se creen pestañas duplicadas por mayusculas/espacios distintos
    (ej. "Aceites" y "aceites ")."""
    categoria = request.form.get("categoria", "").strip().lower()
    if categoria == "__otra__":
        categoria = request.form.get("categoria_otra", "").strip().lower()
    return categoria or "llantas"


@app.route("/catalogo/nuevo", methods=["GET", "POST"])
@auth.requiere_admin
def producto_nuevo():
    conn = database.get_connection()
    try:
        categorias = _categorias_existentes(conn)
    finally:
        conn.close()

    if request.method == "GET":
        return render_template("producto_form.html", producto=None, categorias=categorias)

    marca = request.form.get("marca", "").strip()
    modelo = request.form.get("modelo", "").strip()
    medida = request.form.get("medida", "").strip()
    precio = float(request.form.get("precio") or 0)
    costo = float(request.form.get("costo") or 0)
    stock = int(request.form.get("stock") or 0)
    descripcion = request.form.get("descripcion", "").strip()
    categoria = _categoria_desde_form()

    if not marca or not modelo or not medida:
        flash("Marca, modelo y medida son obligatorios.", "error")
        return render_template("producto_form.html", producto=request.form, categorias=categorias)

    imagen_original = _guardar_imagen_subida(request.files.get("imagen"))

    conn = database.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO productos (marca, modelo, medida, precio, costo, stock, "
            "imagen_original, descripcion, categoria, activo, creado_en, orden) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, (SELECT COALESCE(MAX(orden), 0) + 1 FROM productos))",
            (marca, modelo, medida, precio, costo, stock, imagen_original, descripcion, categoria, database.ahora_iso()),
        )
        producto_id = cursor.lastrowid
        if imagen_original:
            regenerar_imagen_producto(conn, producto_id)
        registrar_actividad(conn, "producto", f"Agrego {marca} {modelo} al catalogo ({categoria})")
        conn.commit()
    finally:
        conn.close()

    flash(f"{marca} {modelo} agregado al catalogo.", "ok")
    return redirect(url_for("catalogo"))


@app.route("/catalogo/<int:producto_id>/editar", methods=["GET", "POST"])
@auth.requiere_admin
def producto_editar(producto_id):
    conn = database.get_connection()
    try:
        producto = conn.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if not producto:
            flash("Ese producto no existe.", "error")
            return redirect(url_for("catalogo"))
        categorias = _categorias_existentes(conn)

        if request.method == "GET":
            return render_template("producto_form.html", producto=producto, categorias=categorias)

        marca = request.form.get("marca", "").strip()
        modelo = request.form.get("modelo", "").strip()
        medida = request.form.get("medida", "").strip()
        precio = float(request.form.get("precio") or 0)
        costo = float(request.form.get("costo") or 0)
        stock = int(request.form.get("stock") or 0)
        descripcion = request.form.get("descripcion", "").strip()
        categoria = _categoria_desde_form()

        if not marca or not modelo or not medida:
            flash("Marca, modelo y medida son obligatorios.", "error")
            return render_template("producto_form.html", producto=producto, categorias=categorias)

        imagen_nueva = _guardar_imagen_subida(request.files.get("imagen"))
        imagen_original = imagen_nueva or producto["imagen_original"]

        conn.execute(
            "UPDATE productos SET marca=?, modelo=?, medida=?, precio=?, costo=?, stock=?, "
            "imagen_original=?, descripcion=?, categoria=? WHERE id=?",
            (marca, modelo, medida, precio, costo, stock, imagen_original, descripcion, categoria, producto_id),
        )
        if imagen_original:
            regenerar_imagen_producto(conn, producto_id)
        registrar_actividad(conn, "producto", f"Edito el producto {marca} {modelo}")
        conn.commit()
    finally:
        conn.close()

    flash("Producto actualizado.", "ok")
    return redirect(url_for("catalogo"))


@app.route("/catalogo/<int:producto_id>/eliminar", methods=["POST"])
@auth.requiere_admin
def producto_eliminar(producto_id):
    conn = database.get_connection()
    try:
        producto = conn.execute("SELECT marca, modelo FROM productos WHERE id = ?", (producto_id,)).fetchone()
        conn.execute("UPDATE productos SET activo = 0 WHERE id = ?", (producto_id,))
        if producto:
            registrar_actividad(conn, "producto", f"Elimino el neumatico {producto['marca']} {producto['modelo']}")
        conn.commit()
    finally:
        conn.close()
    flash("Neumatico eliminado del catalogo.", "ok")
    return redirect(url_for("catalogo"))


@app.route("/api/catalogo/eliminar-multiples", methods=["POST"])
@auth.requiere_admin
def api_catalogo_eliminar_multiples():
    """Elimina (desactiva) varios neumaticos de una vez, seleccionados con
    las casillas del catalogo - mismo efecto que eliminar uno por uno, solo
    que en un solo paso."""
    datos = request.get_json(force=True) or {}
    ids = [int(i) for i in datos.get("ids", []) if str(i).isdigit()]
    if not ids:
        return jsonify({"error": "No se selecciono ningun neumatico."}), 400

    conn = database.get_connection()
    try:
        marcadores = ",".join("?" * len(ids))
        conn.execute(f"UPDATE productos SET activo = 0 WHERE id IN ({marcadores})", ids)
        registrar_actividad(
            conn, "producto",
            f"Elimino {len(ids)} neumatico{'s' if len(ids) != 1 else ''} del catalogo de una vez",
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "eliminados": len(ids)})


@app.route("/catalogo/importar", methods=["POST"])
@auth.requiere_admin
def catalogo_importar():
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Selecciona un archivo .xlsx o .csv para importar.", "error")
        return redirect(url_for("catalogo"))

    ajustes = obtener_ajustes()
    try:
        margen_defecto = float(ajustes.get("margen_defecto") or 10)
    except (TypeError, ValueError):
        margen_defecto = 10.0

    try:
        productos, errores = importador.importar_catalogo(
            archivo.filename, archivo.read(), margen_defecto=margen_defecto
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("catalogo"))

    conn = database.get_connection()
    insertados = 0
    actualizados = 0
    try:
        for p in productos:
            imagen_original = p["imagen"] if p["imagen"] and (database.IMAGENES_DIR / p["imagen"]).exists() else None

            # Si ya existe un neumatico con la misma marca/modelo/medida (sin
            # importar mayusculas), se actualiza en vez de duplicarlo - asi
            # volver a subir el catalogo completo (ej. con los costos reales)
            # corrige los precios existentes en lugar de crear copias.
            existente = conn.execute(
                "SELECT id FROM productos WHERE LOWER(marca) = LOWER(?) "
                "AND LOWER(modelo) = LOWER(?) AND LOWER(medida) = LOWER(?)",
                (p["marca"], p["modelo"], p["medida"]),
            ).fetchone()

            if existente:
                if imagen_original:
                    conn.execute(
                        "UPDATE productos SET costo = ?, precio = ?, stock = ?, "
                        "imagen_original = ?, categoria = ?, activo = 1, orden = ? WHERE id = ?",
                        (p["costo"], p["precio"], p["stock"], imagen_original, p["categoria"], p["orden"], existente["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE productos SET costo = ?, precio = ?, stock = ?, categoria = ?, activo = 1, orden = ? WHERE id = ?",
                        (p["costo"], p["precio"], p["stock"], p["categoria"], p["orden"], existente["id"]),
                    )
                conn.commit()
                if imagen_original:
                    regenerar_imagen_producto(conn, existente["id"])
                actualizados += 1
            else:
                cursor = conn.execute(
                    "INSERT INTO productos (marca, modelo, medida, costo, precio, stock, "
                    "imagen_original, categoria, activo, creado_en, orden) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (p["marca"], p["modelo"], p["medida"], p["costo"], p["precio"], p["stock"],
                     imagen_original, p["categoria"], database.ahora_iso(), p["orden"]),
                )
                conn.commit()
                if imagen_original:
                    regenerar_imagen_producto(conn, cursor.lastrowid)
                insertados += 1

        if insertados or actualizados:
            registrar_actividad(
                conn, "producto",
                f"Importo el catalogo desde Excel/CSV: {insertados} nuevos, {actualizados} actualizados",
            )
            conn.commit()
    finally:
        conn.close()

    mensaje = f"Listo: {insertados} neumaticos nuevos, {actualizados} actualizados."
    if errores:
        mensaje += f" ({len(errores)} filas con problemas: {'; '.join(errores[:5])})"
    flash(mensaje, "ok" if (insertados or actualizados) else "error")
    return redirect(url_for("catalogo"))


def _guardar_imagen_subida(archivo):
    if not archivo or not archivo.filename:
        return None
    extension = Path(archivo.filename).suffix.lower()
    if extension not in EXTENSIONES_IMAGEN:
        flash("Formato de imagen no soportado (usa jpg, png o webp).", "error")
        return None
    nombre_seguro = secure_filename(archivo.filename)
    nombre_final = f"{database.ahora_iso().replace(':', '').replace(' ', '_').replace('-', '')}_{nombre_seguro}"
    ruta = database.IMAGENES_DIR / nombre_final
    archivo.save(ruta)
    return nombre_final


# -------------------------------------------------------------- contactos --
ESTADOS_VALIDOS = ["nuevo", "cotizado", "negociacion", "vendido", "perdido"]
NOMBRES_ESTADOS = {
    "nuevo": "Nuevo",
    "cotizado": "Cotizado",
    "negociacion": "Negociacion",
    "vendido": "Vendido",
    "perdido": "Perdido",
}


def _vendedor_actual_id():
    """None si es admin (ve todo); id del usuario si es vendedor (solo lo suyo)."""
    return None if auth.es_admin() else g.usuario["id"]


def registrar_actividad(conn, accion: str, descripcion: str):
    """Deja una linea en la bitacora de actividad (visible solo para
    administradores en /actividad): quien hizo que y cuando. El nombre del
    usuario se guarda tal cual esta en ese momento (si despues se le cambia
    el nombre o se elimina la cuenta, el historial queda igual de legible).
    'accion' es una categoria corta (cotizacion, contacto, producto, usuario,
    login, ajustes) para poder filtrar y ponerle un icono en la bitacora."""
    usuario = g.get("usuario")
    conn.execute(
        "INSERT INTO actividad (usuario_id, usuario_nombre, accion, descripcion, fecha) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            usuario["id"] if usuario else None,
            usuario["nombre"] if usuario else "Sistema",
            accion,
            descripcion,
            database.ahora_iso(),
        ),
    )


def obtener_resumen_crm(conn, vendedor_id=None):
    """Totales de todo lo que la app va guardando: clientes, cotizaciones
    enviadas, valor cotizado y neumaticos activos en el catalogo. Si se pasa
    vendedor_id, los totales quedan acotados a los contactos de ese vendedor."""
    if vendedor_id:
        total_contactos = conn.execute(
            "SELECT COUNT(*) FROM contactos WHERE vendedor_id = ?", (vendedor_id,)
        ).fetchone()[0]
        total_cotizaciones = conn.execute(
            "SELECT COUNT(*) FROM cotizaciones WHERE contacto_id IN "
            "(SELECT id FROM contactos WHERE vendedor_id = ?)",
            (vendedor_id,),
        ).fetchone()[0]
        valor_cotizado = conn.execute(
            "SELECT COALESCE(SUM(total), 0) FROM cotizaciones WHERE contacto_id IN "
            "(SELECT id FROM contactos WHERE vendedor_id = ?)",
            (vendedor_id,),
        ).fetchone()[0]
    else:
        total_contactos = conn.execute("SELECT COUNT(*) FROM contactos").fetchone()[0]
        total_cotizaciones = conn.execute("SELECT COUNT(*) FROM cotizaciones").fetchone()[0]
        valor_cotizado = conn.execute("SELECT COALESCE(SUM(total), 0) FROM cotizaciones").fetchone()[0]
    return {
        "total_contactos": total_contactos,
        "total_cotizaciones": total_cotizaciones,
        "valor_cotizado": valor_cotizado,
        "total_productos": conn.execute("SELECT COUNT(*) FROM productos WHERE activo = 1").fetchone()[0],
    }


def _resumen_ventas_por_dia(conn, vendedor_id, dias):
    """Para cada uno de los ultimos `dias` dias (incluyendo hoy): cuanto se
    coti­zo (suma de proformas emitidas ese dia) y cuanto se vendio + la
    ganancia que genero (de los contactos marcados 'vendido' ese dia, usando
    su ultima proforma como el monto/los items de la venta). La ganancia
    depende de que el catalogo tenga el costo cargado por producto (Catalogo
    > Editar); si no esta, se calcula como 0 para ese item, no se inventa."""
    desde = (datetime.now() - timedelta(days=dias - 1)).strftime("%Y-%m-%d")
    dias_lista = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dias - 1, -1, -1)]
    datos = {d: {"cotizado": 0.0, "vendido": 0.0, "ganancia": 0.0} for d in dias_lista}

    sql_cot = (
        "SELECT substr(fecha, 1, 10) AS dia, COALESCE(SUM(total), 0) AS valor "
        "FROM cotizaciones WHERE tipo = 'proforma' AND fecha >= ?"
    )
    params_cot = [desde]
    if vendedor_id:
        sql_cot += " AND contacto_id IN (SELECT id FROM contactos WHERE vendedor_id = ?)"
        params_cot.append(vendedor_id)
    sql_cot += " GROUP BY dia"
    for fila in conn.execute(sql_cot, params_cot).fetchall():
        if fila["dia"] in datos:
            datos[fila["dia"]]["cotizado"] = fila["valor"]

    sql_vend = (
        "SELECT c.id AS contacto_id, substr(c.fecha_venta, 1, 10) AS dia, "
        "(SELECT id FROM cotizaciones WHERE contacto_id = c.id AND tipo = 'proforma' "
        " ORDER BY fecha DESC LIMIT 1) AS cotizacion_id, "
        "COALESCE((SELECT total FROM cotizaciones WHERE contacto_id = c.id AND tipo = 'proforma' "
        " ORDER BY fecha DESC LIMIT 1), 0) AS total "
        "FROM contactos c WHERE c.estado = 'vendido' AND c.fecha_venta >= ?"
    )
    params_vend = [desde]
    if vendedor_id:
        sql_vend += " AND c.vendedor_id = ?"
        params_vend.append(vendedor_id)
    ventas = conn.execute(sql_vend, params_vend).fetchall()

    ids_cotizaciones = [v["cotizacion_id"] for v in ventas if v["cotizacion_id"]]
    ganancia_por_cotizacion = {}
    if ids_cotizaciones:
        marcadores = ",".join("?" * len(ids_cotizaciones))
        filas_gan = conn.execute(
            f"SELECT ci.cotizacion_id AS cid, "
            f"COALESCE(SUM((ci.precio_unitario - COALESCE(p.costo, 0)) * ci.cantidad), 0) AS ganancia "
            f"FROM cotizacion_items ci LEFT JOIN productos p ON p.id = ci.producto_id "
            f"WHERE ci.cotizacion_id IN ({marcadores}) GROUP BY ci.cotizacion_id",
            ids_cotizaciones,
        ).fetchall()
        ganancia_por_cotizacion = {f["cid"]: f["ganancia"] for f in filas_gan}

    for v in ventas:
        dia = v["dia"]
        if dia not in datos:
            continue
        datos[dia]["vendido"] += v["total"]
        datos[dia]["ganancia"] += ganancia_por_cotizacion.get(v["cotizacion_id"], 0)

    return datos, dias_lista


@app.route("/dashboard")
def dashboard():
    """Pantalla principal al entrar al sistema: un vistazo rapido del
    negocio y, sobre todo, los leads a los que ya se les escribio HOY para
    no perder el hilo y poder retomar el seguimiento con un clic."""
    vendedor_id = _vendedor_actual_id()
    hoy = database.ahora_iso()[:10]  # 'YYYY-MM-DD'
    hace_3_dias = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    hace_2_dias = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    conn = database.get_connection()
    try:
        resumen = obtener_resumen_crm(conn, vendedor_id)

        sql = (
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM cotizaciones WHERE contacto_id = c.id AND fecha LIKE ?) AS cotizaciones_hoy, "
            "COALESCE((SELECT SUM(total) FROM cotizaciones WHERE contacto_id = c.id AND fecha LIKE ?), 0) AS valor_hoy, "
            "(SELECT tipo FROM cotizaciones WHERE contacto_id = c.id AND fecha LIKE ? "
            " ORDER BY fecha DESC LIMIT 1) AS ultimo_tipo_hoy, "
            "(SELECT fecha FROM cotizaciones WHERE contacto_id = c.id AND fecha LIKE ? "
            " ORDER BY fecha DESC LIMIT 1) AS ultimo_contacto_hoy, "
            "(SELECT pdf_archivo FROM cotizaciones WHERE contacto_id = c.id AND fecha LIKE ? "
            " ORDER BY fecha DESC LIMIT 1) AS ultimo_pdf_hoy "
            "FROM contactos c "
            "WHERE EXISTS (SELECT 1 FROM cotizaciones WHERE contacto_id = c.id AND fecha LIKE ?) "
        )
        params = [f"{hoy}%"] * 6
        if vendedor_id:
            sql += "AND c.vendedor_id = ? "
            params.append(vendedor_id)
        sql += "ORDER BY ultimo_contacto_hoy DESC"
        filas = conn.execute(sql, params).fetchall()

        # --- vendidos hoy: un contacto puede pasar a "vendido" en el
        # Pipeline/CRM un dia distinto al que se le genero la cotizacion
        # (ej. se le cotizo ayer y hoy confirmo la compra), asi que esto es
        # una consulta aparte de "leads_hoy" (que se basa en cotizaciones
        # enviadas hoy, no en el estado actual). Usa la ultima proforma del
        # contacto como el monto/los items de la venta.
        # Se exige ademas c.estado = 'vendido': si alguien lo movio de vuelta
        # a otro estado (fue un error, o se cayo la venta), tiene que
        # desaparecer de aqui tambien - el dashboard sigue el estado actual
        # del pipeline, no un registro congelado del momento en que se marco. ---
        sql_vendidos_hoy = (
            "SELECT c.*, "
            "(SELECT id FROM cotizaciones WHERE contacto_id = c.id AND tipo = 'proforma' "
            " ORDER BY fecha DESC LIMIT 1) AS ultima_cotizacion_id, "
            "COALESCE((SELECT total FROM cotizaciones WHERE contacto_id = c.id AND tipo = 'proforma' "
            " ORDER BY fecha DESC LIMIT 1), 0) AS valor_venta, "
            "COALESCE((SELECT SUM((ci.precio_unitario - COALESCE(p.costo, 0)) * ci.cantidad) "
            " FROM cotizacion_items ci LEFT JOIN productos p ON p.id = ci.producto_id "
            " WHERE ci.cotizacion_id = (SELECT id FROM cotizaciones WHERE contacto_id = c.id "
            " AND tipo = 'proforma' ORDER BY fecha DESC LIMIT 1)), 0) AS ganancia_venta, "
            "(SELECT pdf_archivo FROM cotizaciones WHERE contacto_id = c.id AND tipo = 'proforma' "
            " ORDER BY fecha DESC LIMIT 1) AS pdf_venta "
            "FROM contactos c WHERE c.estado = 'vendido' AND c.fecha_venta LIKE ? "
        )
        params_vendidos_hoy = [f"{hoy}%"]
        if vendedor_id:
            sql_vendidos_hoy += "AND c.vendedor_id = ? "
            params_vendidos_hoy.append(vendedor_id)
        sql_vendidos_hoy += "ORDER BY c.fecha_venta DESC"
        filas_vendidos_hoy = conn.execute(sql_vendidos_hoy, params_vendidos_hoy).fetchall()

        # --- alertas: cosas que necesitan atencion, sin importar el dia ---
        alertas = []

        sql_estancados = (
            "SELECT COUNT(*) FROM contactos WHERE estado IN ('cotizado', 'negociacion') "
            "AND COALESCE(ultimo_contacto, creado_en) < ?"
        )
        params_estancados = [hace_3_dias]
        if vendedor_id:
            sql_estancados += " AND vendedor_id = ?"
            params_estancados.append(vendedor_id)
        estancados = conn.execute(sql_estancados, params_estancados).fetchone()[0]
        if estancados:
            alertas.append({
                "icono": "⏳",
                "texto": f"{estancados} lead{'s' if estancados != 1 else ''} en cotizado/negociacion sin actividad hace mas de 3 dias",
                "link": url_for("contactos"),
                "boton": "Ver en CRM",
            })

        # Se excluyen los contactos que llegaron por una importacion masiva
        # (origen 'importado' o 'DATA LOCAL'): son carga historica, no leads
        # activos esperando respuesta, y contarlos haria esta alerta inutil
        # (siempre al tope con la base entera).
        sql_sin_seguimiento = (
            "SELECT COUNT(*) FROM contactos WHERE estado = 'nuevo' AND creado_en < ? "
            "AND (origen IS NULL OR origen NOT IN ('importado', 'DATA LOCAL'))"
        )
        params_sin_seguimiento = [hace_2_dias]
        if vendedor_id:
            sql_sin_seguimiento += " AND vendedor_id = ?"
            params_sin_seguimiento.append(vendedor_id)
        sin_seguimiento = conn.execute(sql_sin_seguimiento, params_sin_seguimiento).fetchone()[0]
        if sin_seguimiento:
            alertas.append({
                "icono": "📵",
                "texto": f"{sin_seguimiento} lead{'s' if sin_seguimiento != 1 else ''} nuevo{'s' if sin_seguimiento != 1 else ''} sin cotizar todavia (mas de 2 dias)",
                "link": url_for("contactos") + "?estado=nuevo",
                "boton": "Ver en CRM",
            })

        # Nota: no hay alerta de "poco stock" (ej. stock <= 2) porque el stock
        # en el catalogo no se usa como inventario real controlado (la gran
        # mayoria de productos quedo en 1 por defecto al importar) - esa
        # alerta saldria siempre con casi todo el catalogo y seria puro
        # ruido. "Agotado" (stock = 0) si es una señal real: alguien lo puso
        # en 0 a proposito.
        agotados = conn.execute(
            "SELECT COUNT(*) FROM productos WHERE activo = 1 AND stock = 0"
        ).fetchone()[0]
        if agotados:
            alertas.append({
                "icono": "🚫",
                "texto": f"{agotados} neumatico{'s' if agotados != 1 else ''} marcado{'s' if agotados != 1 else ''} como agotado en el catalogo",
                "link": url_for("catalogo"),
                "boton": "Ver catalogo",
            })

        datos_por_dia, dias_lista = _resumen_ventas_por_dia(conn, vendedor_id, 7)
        hay_costos_cargados = conn.execute(
            "SELECT 1 FROM productos WHERE activo = 1 AND costo > 0 LIMIT 1"
        ).fetchone() is not None
    finally:
        conn.close()

    ajustes = obtener_ajustes()
    leads_hoy = []
    for fila in filas:
        lead = dict(fila)
        lead["wa_link"] = whatsapp.construir_link_wa(fila["telefono"], "", ajustes["prefijo_telefono"])
        lead["hora"] = (lead["ultimo_contacto_hoy"] or "")[11:16]
        leads_hoy.append(lead)

    resumen["leads_hoy"] = len(leads_hoy)
    resumen["cotizaciones_hoy"] = sum(l["cotizaciones_hoy"] for l in leads_hoy)
    resumen["valor_hoy"] = sum(l["valor_hoy"] for l in leads_hoy)

    vendidos_hoy = []
    for fila in filas_vendidos_hoy:
        venta = dict(fila)
        venta["wa_link"] = whatsapp.construir_link_wa(fila["telefono"], "", ajustes["prefijo_telefono"])
        venta["hora"] = (venta["fecha_venta"] or "")[11:16]
        vendidos_hoy.append(venta)

    hoy_str = dias_lista[-1]
    resumen["valor_vendido_hoy"] = datos_por_dia[hoy_str]["vendido"]
    resumen["ganancia_hoy"] = datos_por_dia[hoy_str]["ganancia"]
    serie_7dias = [{"dia": d, **datos_por_dia[d]} for d in dias_lista]

    return render_template(
        "dashboard.html",
        resumen=resumen,
        leads_hoy=leads_hoy,
        vendidos_hoy=vendidos_hoy,
        alertas=alertas,
        serie_7dias=serie_7dias,
        hay_costos_cargados=hay_costos_cargados,
        estados=ESTADOS_VALIDOS,
        nombres_estados=NOMBRES_ESTADOS,
    )


@app.route("/inbox")
def inbox():
    """Bandeja unificada de mensajes. Por ahora es un panel de estado: las
    cuentas de WhatsApp Business y Facebook Messenger todavia no estan
    conectadas (eso requiere una app de Meta aprobada y un servidor publico
    con HTTPS para recibir los webhooks, que hoy no existe porque seguimos
    sin el VPS en linea). La tabla "conversaciones" ya existe lista para
    cuando se conecten de verdad."""
    conn = database.get_connection()
    try:
        conversaciones = conn.execute(
            "SELECT * FROM conversaciones ORDER BY ultimo_mensaje_en DESC LIMIT 100"
        ).fetchall()
    finally:
        conn.close()
    return render_template("inbox.html", conversaciones=conversaciones)


@app.route("/contactos")
def contactos():
    estado = request.args.get("estado", "")
    q = request.args.get("q", "").strip()
    vendedor_id = _vendedor_actual_id()
    conn = database.get_connection()
    try:
        sql = "SELECT * FROM contactos WHERE 1=1"
        params = []
        if vendedor_id:
            sql += " AND vendedor_id = ?"
            params.append(vendedor_id)
        if estado:
            sql += " AND estado = ?"
            params.append(estado)
        if q:
            sql += " AND (nombre LIKE ? OR telefono LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        sql += " ORDER BY COALESCE(ultimo_contacto, creado_en) DESC"
        lista = conn.execute(sql, params).fetchall()
        resumen = obtener_resumen_crm(conn, vendedor_id)
    finally:
        conn.close()
    return render_template(
        "contactos.html", contactos=lista, estado=estado, q=q, estados=ESTADOS_VALIDOS, resumen=resumen
    )


@app.route("/contactos/pipeline")
def contactos_pipeline():
    """Tablero de pipeline de ventas: un cliente por tarjeta, agrupados por
    estado, para arrastrar de columna segun avanza la venta."""
    vendedor_id = _vendedor_actual_id()
    conn = database.get_connection()
    try:
        sql = (
            "SELECT c.*, "
            "COALESCE((SELECT SUM(total) FROM cotizaciones WHERE contacto_id = c.id), 0) AS valor_cotizado, "
            "(SELECT COUNT(*) FROM cotizaciones WHERE contacto_id = c.id) AS num_cotizaciones, "
            "(SELECT pdf_archivo FROM cotizaciones WHERE contacto_id = c.id "
            " ORDER BY fecha DESC LIMIT 1) AS ultimo_pdf, "
            "(SELECT id FROM cotizaciones WHERE contacto_id = c.id "
            " ORDER BY fecha DESC LIMIT 1) AS ultima_cotizacion_id "
            "FROM contactos c "
        )
        params = []
        if vendedor_id:
            sql += "WHERE c.vendedor_id = ? "
            params.append(vendedor_id)
        sql += "ORDER BY COALESCE(c.ultimo_contacto, c.creado_en) DESC"
        filas = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    ajustes = obtener_ajustes()
    columnas = {estado: [] for estado in ESTADOS_VALIDOS}
    for fila in filas:
        contacto = dict(fila)
        contacto["wa_link"] = whatsapp.construir_link_wa(fila["telefono"], "", ajustes["prefijo_telefono"])
        columnas.setdefault(contacto["estado"], []).append(contacto)

    return render_template(
        "pipeline.html",
        columnas=columnas,
        estados=ESTADOS_VALIDOS,
        nombres_estados=NOMBRES_ESTADOS,
    )


@app.route("/api/contactos/<int:contacto_id>/estado", methods=["POST"])
def api_contacto_actualizar_estado(contacto_id):
    """Actualiza solo el estado de un contacto (usado al soltar su tarjeta
    en otra columna del pipeline)."""
    datos = request.get_json(force=True)
    estado = datos.get("estado")
    if estado not in ESTADOS_VALIDOS:
        return jsonify({"error": "Estado invalido."}), 400
    conn = database.get_connection()
    try:
        fila = conn.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        if not fila:
            return jsonify({"error": "Ese contacto no existe."}), 404
        if not auth.es_admin() and fila["vendedor_id"] != g.usuario["id"]:
            return jsonify({"error": "Ese contacto no es tuyo."}), 403
        # La fecha de venta se guarda la primera vez que entra a "vendido"
        # (no se pisa si ya estaba vendido y solo se re-selecciona el mismo
        # estado); sirve para saber que se vendio HOY en el dashboard.
        if estado == "vendido" and fila["estado"] != "vendido":
            conn.execute(
                "UPDATE contactos SET estado = ?, fecha_venta = ? WHERE id = ?",
                (estado, database.ahora_iso(), contacto_id),
            )
        else:
            conn.execute("UPDATE contactos SET estado = ? WHERE id = ?", (estado, contacto_id))
        if estado != fila["estado"]:
            registrar_actividad(
                conn, "contacto",
                f"Cambio el estado de {fila['nombre']} a {NOMBRES_ESTADOS.get(estado, estado)}",
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/cotizaciones/<int:cotizacion_id>/eliminar", methods=["POST"])
def api_cotizacion_eliminar(cotizacion_id):
    """Elimina una cotizacion (y su PDF): se usa tanto desde el pipeline
    como desde el historial del contacto."""
    conn = database.get_connection()
    try:
        fila = conn.execute(
            "SELECT co.*, ct.vendedor_id AS contacto_vendedor_id, ct.nombre AS contacto_nombre "
            "FROM cotizaciones co JOIN contactos ct ON ct.id = co.contacto_id "
            "WHERE co.id = ?",
            (cotizacion_id,),
        ).fetchone()
        if not fila:
            return jsonify({"error": "Esa cotizacion no existe."}), 404
        if not auth.es_admin() and fila["contacto_vendedor_id"] != g.usuario["id"]:
            return jsonify({"error": "Esa cotizacion no es tuya."}), 403

        conn.execute("DELETE FROM cotizacion_items WHERE cotizacion_id = ?", (cotizacion_id,))
        conn.execute("DELETE FROM cotizaciones WHERE id = ?", (cotizacion_id,))
        registrar_actividad(
            conn, "cotizacion",
            f"Elimino una cotizacion de {fila['contacto_nombre']} por ${fila['total']:,.2f}",
        )
        conn.commit()

        if fila["pdf_archivo"]:
            ruta = database.COTIZACIONES_DIR / fila["pdf_archivo"]
            if ruta.exists():
                ruta.unlink()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/contactos/nuevo", methods=["GET", "POST"])
def contacto_nuevo():
    if request.method == "GET":
        return render_template("contacto_form.html")
    nombre = request.form.get("nombre", "").strip()
    telefono = request.form.get("telefono", "").strip()
    origen = request.form.get("origen", "").strip()
    notas = request.form.get("notas", "").strip()
    if not nombre or not telefono:
        flash("Nombre y telefono son obligatorios.", "error")
        return render_template("contacto_form.html")
    conn = database.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO contactos (nombre, telefono, origen, estado, notas, creado_en, vendedor_id) "
            "VALUES (?, ?, ?, 'nuevo', ?, ?, ?)",
            (nombre, telefono, origen, notas, database.ahora_iso(), g.usuario["id"]),
        )
        contacto_id = cursor.lastrowid
        registrar_actividad(conn, "contacto", f"Creo el contacto {nombre}")
        conn.commit()
    finally:
        conn.close()
    flash("Contacto agregado.", "ok")
    return redirect(url_for("contacto_detalle", contacto_id=contacto_id))


@app.route("/contactos/importar", methods=["POST"])
def contactos_importar():
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Selecciona un archivo .xlsx o .csv para importar.", "error")
        return redirect(url_for("contactos"))

    try:
        contactos_nuevos, errores = importador_contactos.importar_contactos(
            archivo.filename, archivo.read()
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("contactos"))

    vendedor_id = g.usuario["id"]
    conn = database.get_connection()
    insertados = 0
    repetidos = 0
    try:
        for c in contactos_nuevos:
            sql_existe = "SELECT 1 FROM contactos WHERE telefono = ?"
            params_existe = [c["telefono"]]
            if not auth.es_admin():
                sql_existe += " AND vendedor_id = ?"
                params_existe.append(vendedor_id)
            existente = conn.execute(sql_existe, params_existe).fetchone()
            if existente:
                repetidos += 1
                continue
            conn.execute(
                "INSERT INTO contactos (nombre, telefono, origen, estado, notas, creado_en, vendedor_id) "
                "VALUES (?, ?, ?, 'nuevo', ?, ?, ?)",
                (c["nombre"], c["telefono"], c["origen"] or "importado", c["notas"], database.ahora_iso(), vendedor_id),
            )
            insertados += 1
        if insertados:
            registrar_actividad(conn, "contacto", f"Importo {insertados} contactos desde Excel/CSV")
        conn.commit()
    finally:
        conn.close()

    mensaje = f"Se importaron {insertados} contactos."
    if repetidos:
        mensaje += f" ({repetidos} ya existian por telefono repetido y se omitieron)"
    if errores:
        mensaje += f" ({len(errores)} filas con problemas: {'; '.join(errores[:5])})"
    flash(mensaje, "ok" if insertados else "error")
    return redirect(url_for("contactos"))


def _puede_ver_contacto(contacto_fila) -> bool:
    return auth.es_admin() or contacto_fila["vendedor_id"] == g.usuario["id"]


@app.route("/contactos/<int:contacto_id>")
def contacto_detalle(contacto_id):
    conn = database.get_connection()
    try:
        contacto = conn.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        if not contacto:
            flash("Ese contacto no existe.", "error")
            return redirect(url_for("contactos"))
        if not _puede_ver_contacto(contacto):
            flash("Ese contacto no es tuyo.", "error")
            return redirect(url_for("contactos"))
        cotizaciones = conn.execute(
            "SELECT * FROM cotizaciones WHERE contacto_id = ? ORDER BY fecha DESC", (contacto_id,)
        ).fetchall()
        items_por_cotizacion = {}
        for c in cotizaciones:
            items_por_cotizacion[c["id"]] = conn.execute(
                "SELECT * FROM cotizacion_items WHERE cotizacion_id = ?", (c["id"],)
            ).fetchall()
    finally:
        conn.close()
    return render_template(
        "contacto_detalle.html",
        contacto=contacto,
        cotizaciones=cotizaciones,
        items_por_cotizacion=items_por_cotizacion,
        estados=ESTADOS_VALIDOS,
    )


@app.route("/contactos/<int:contacto_id>/actualizar", methods=["POST"])
def contacto_actualizar(contacto_id):
    estado = request.form.get("estado", "nuevo")
    notas = request.form.get("notas", "").strip()
    conn = database.get_connection()
    try:
        contacto = conn.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        if not contacto or not _puede_ver_contacto(contacto):
            flash("Ese contacto no es tuyo.", "error")
            return redirect(url_for("contactos"))
        if estado == "vendido" and contacto["estado"] != "vendido":
            conn.execute(
                "UPDATE contactos SET estado = ?, notas = ?, fecha_venta = ? WHERE id = ?",
                (estado, notas, database.ahora_iso(), contacto_id),
            )
        else:
            conn.execute(
                "UPDATE contactos SET estado = ?, notas = ? WHERE id = ?",
                (estado, notas, contacto_id),
            )
        registrar_actividad(conn, "contacto", f"Actualizo el contacto {contacto['nombre']}")
        conn.commit()
    finally:
        conn.close()
    flash("Contacto actualizado.", "ok")
    return redirect(url_for("contacto_detalle", contacto_id=contacto_id))


# ----------------------------------------------------------------- cotizar --
@app.route("/cotizar")
def cotizar():
    vendedor_id = _vendedor_actual_id()
    conn = database.get_connection()
    try:
        productos = conn.execute(
            "SELECT * FROM productos WHERE activo = 1 ORDER BY categoria, orden"
        ).fetchall()
        sql = "SELECT id, nombre, telefono FROM contactos"
        params = []
        if vendedor_id:
            sql += " WHERE vendedor_id = ?"
            params.append(vendedor_id)
        sql += " ORDER BY nombre"
        contactos_lista = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return render_template("cotizar.html", productos=productos, contactos=contactos_lista)


@app.route("/ventas")
def ventas():
    """Listado de todos los pedidos (cotizaciones y proformas) generados
    desde Cotizar, con acceso directo a cada PDF - el historial completo de
    lo que se ha mandado, sin tener que entrar contacto por contacto."""
    vendedor_id = _vendedor_actual_id()
    q = request.args.get("q", "").strip()
    tipo = request.args.get("tipo", "")
    rango = request.args.get("rango", "30")
    conn = database.get_connection()
    try:
        sql = (
            "SELECT co.*, c.nombre AS cliente_nombre, c.telefono AS cliente_telefono, "
            "c.id AS contacto_id, u.nombre AS vendedor_nombre "
            "FROM cotizaciones co "
            "JOIN contactos c ON c.id = co.contacto_id "
            "LEFT JOIN usuarios u ON u.id = c.vendedor_id "
            "WHERE 1=1 "
        )
        params = []
        if vendedor_id:
            sql += "AND c.vendedor_id = ? "
            params.append(vendedor_id)
        if q:
            sql += "AND (c.nombre LIKE ? OR c.telefono LIKE ?) "
            params.extend([f"%{q}%", f"%{q}%"])
        if tipo in ("cotizacion", "proforma"):
            sql += "AND co.tipo = ? "
            params.append(tipo)
        if rango != "todo":
            dias = int(rango) if rango.isdigit() else 30
            desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
            sql += "AND co.fecha >= ? "
            params.append(desde)
        sql += "ORDER BY co.fecha DESC LIMIT 300"
        pedidos = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    total_valor = sum(p["total"] for p in pedidos if p["tipo"] == "proforma")

    return render_template(
        "ventas.html",
        pedidos=pedidos,
        q=q,
        tipo=tipo,
        rango=rango,
        total_valor=total_valor,
    )


@app.route("/ventas/registrar")
def venta_manual():
    """Formulario para registrar una venta que ocurrio sin pasar por
    Cotizar (venta directa de mostrador, telefono, etc.) - mismo selector
    de cliente/productos que Cotizar, pero queda marcada como vendida de
    una vez, con su propio PDF de respaldo."""
    vendedor_id = _vendedor_actual_id()
    conn = database.get_connection()
    try:
        productos = conn.execute(
            "SELECT * FROM productos WHERE activo = 1 ORDER BY categoria, orden"
        ).fetchall()
        sql = "SELECT id, nombre, telefono FROM contactos"
        params = []
        if vendedor_id:
            sql += " WHERE vendedor_id = ?"
            params.append(vendedor_id)
        sql += " ORDER BY nombre"
        contactos_lista = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    hoy = database.ahora_iso()[:10]
    return render_template(
        "venta_manual.html", productos=productos, contactos=contactos_lista, hoy=hoy
    )


@app.route("/api/ventas/registrar", methods=["POST"])
def api_venta_manual():
    """Registra una venta directa: crea (o reutiliza) el contacto, guarda la
    cotizacion tipo proforma con sus items, genera el PDF de respaldo, y
    marca el contacto como 'vendido' de una vez (con la fecha que se haya
    indicado, para poder registrar ventas de dias anteriores que no se
    hayan cargado a tiempo)."""
    datos = request.get_json(force=True)
    ajustes = obtener_ajustes()
    conn = database.get_connection()
    try:
        contacto_id = datos.get("contacto_id")
        if contacto_id:
            contacto_existente = conn.execute(
                "SELECT * FROM contactos WHERE id = ?", (contacto_id,)
            ).fetchone()
            if not contacto_existente or not _puede_ver_contacto(contacto_existente):
                return jsonify({"error": "Ese contacto no es tuyo."}), 403
        else:
            nombre = (datos.get("nombre") or "").strip()
            telefono = (datos.get("telefono") or "").strip()
            if not nombre or not telefono:
                return jsonify({"error": "Falta nombre o telefono del cliente."}), 400
            sql_existe = "SELECT id FROM contactos WHERE telefono = ?"
            params_existe = [telefono]
            if not auth.es_admin():
                sql_existe += " AND vendedor_id = ?"
                params_existe.append(g.usuario["id"])
            existente = conn.execute(sql_existe, params_existe).fetchone()
            if existente:
                contacto_id = existente["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO contactos (nombre, telefono, origen, estado, creado_en, vendedor_id) "
                    "VALUES (?, ?, 'venta directa', 'nuevo', ?, ?)",
                    (nombre, telefono, database.ahora_iso(), g.usuario["id"]),
                )
                conn.commit()
                contacto_id = cursor.lastrowid

        contacto = conn.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        items = _items_desde_json(conn, datos.get("items", []))
        if not items:
            return jsonify({"error": "Selecciona al menos un neumatico."}), 400
        total_catalogo = sum(it["cantidad"] * it["precio_unitario"] for it in items)

        iva_manual = datos.get("iva_manual")
        iva_manual = float(iva_manual) if iva_manual not in (None, "") else None
        totales = calcular_totales(total_catalogo, iva_manual)

        # Fecha de la venta: hoy por defecto, pero se puede indicar una
        # anterior (registrar algo que paso ayer y no se cargo a tiempo).
        # Siempre a la hora actual dentro de ese dia, para que el orden con
        # otras cotizaciones del mismo dia tenga sentido.
        fecha_form = (datos.get("fecha") or "").strip()
        hoy_hora = database.ahora_iso()
        if fecha_form and fecha_form != hoy_hora[:10]:
            fecha = f"{fecha_form} {hoy_hora[11:]}"
        else:
            fecha = hoy_hora

        mensaje = f"Venta registrada manualmente para {contacto['nombre']}."
        cursor = conn.execute(
            "INSERT INTO cotizaciones (contacto_id, fecha, tipo, subtotal, iva, iva_manual, total, mensaje_texto) "
            "VALUES (?, ?, 'proforma', ?, ?, ?, ?, ?)",
            (contacto_id, fecha, totales["subtotal"], totales["iva"], int(totales["iva_manual"]), totales["total"], mensaje),
        )
        cotizacion_id = cursor.lastrowid
        for it in items:
            conn.execute(
                "INSERT INTO cotizacion_items (cotizacion_id, producto_id, marca, modelo, medida, "
                "cantidad, precio_unitario) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cotizacion_id, it["producto_id"], it["marca"], it["modelo"], it["medida"], it["cantidad"], it["precio_unitario"]),
            )

        ruta_logo = None
        if ajustes.get("logo_archivo"):
            posible = database.LOGO_DIR / ajustes["logo_archivo"]
            if posible.exists():
                ruta_logo = posible

        nombre_pdf = pdf_cotizacion.nombre_archivo_pdf(cotizacion_id, contacto["nombre"], "proforma")
        ruta_pdf = database.COTIZACIONES_DIR / nombre_pdf
        pdf_cotizacion.generar_pdf_cotizacion(
            ruta_pdf,
            folio=cotizacion_id,
            fecha=fecha,
            ajustes=ajustes,
            contacto=dict(contacto),
            items=items,
            tipo="proforma",
            subtotal=totales["subtotal"],
            iva=totales["iva"],
            total=totales["total"],
            ruta_logo=ruta_logo,
        )
        conn.execute("UPDATE cotizaciones SET pdf_archivo = ? WHERE id = ?", (nombre_pdf, cotizacion_id))

        # Queda vendido de una vez. La fecha_venta sigue la misma regla que
        # el resto del sistema: solo se pisa si no estaba ya vendido, para
        # no perder la fecha real de una venta anterior si se repite.
        if contacto["estado"] != "vendido":
            conn.execute(
                "UPDATE contactos SET ultimo_contacto = ?, estado = 'vendido', fecha_venta = ? WHERE id = ?",
                (fecha, fecha, contacto_id),
            )
        else:
            conn.execute("UPDATE contactos SET ultimo_contacto = ? WHERE id = ?", (fecha, contacto_id))

        registrar_actividad(
            conn, "cotizacion",
            f"Registro una venta manual para {contacto['nombre']} por ${totales['total']:,.2f}",
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "contacto_id": contacto_id,
            "cotizacion_id": cotizacion_id,
            "pdf_url": url_for("servir_pdf", nombre=nombre_pdf),
            "total": totales["total"],
        }
    )


@app.route("/api/catalogo/producto-rapido", methods=["POST"])
def api_producto_rapido():
    """Crea un neumatico en el catalogo al vuelo, desde la pantalla de
    Cotizar, para cuando el cliente pide algo que todavia no esta cargado.
    Disponible para cualquier usuario (no solo administradores): la idea es
    no perder tiempo ni frenar la venta por tener que ir a Catalogo o pedirle
    a un admin que lo cargue. Sin foto por ahora (se le puede agregar
    despues desde Catalogo > Editar); lo importante es agilizar el mostrador."""
    datos = request.get_json(force=True) or {}
    marca = (datos.get("marca") or "").strip()
    modelo = (datos.get("modelo") or "").strip()
    medida = (datos.get("medida") or "").strip()

    if not marca or not modelo or not medida:
        return jsonify({"error": "Marca, modelo y medida son obligatorios."}), 400

    try:
        precio = round(float(datos.get("precio") or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "El precio no es valido."}), 400
    if precio <= 0:
        return jsonify({"error": "El precio debe ser mayor a 0."}), 400

    try:
        stock = max(0, int(datos.get("stock") or 1))
    except (TypeError, ValueError):
        stock = 1

    conn = database.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO productos (marca, modelo, medida, precio, stock, activo, creado_en, orden) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, (SELECT COALESCE(MAX(orden), 0) + 1 FROM productos))",
            (marca, modelo, medida, precio, stock, database.ahora_iso()),
        )
        producto_id = cursor.lastrowid
        registrar_actividad(conn, "producto", f"Agrego rapido el neumatico {marca} {modelo} desde Cotizar")
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "producto": {
            "id": producto_id,
            "marca": marca,
            "modelo": modelo,
            "medida": medida,
            "precio": precio,
            "stock": stock,
        },
    })


def _items_desde_json(conn, items_json):
    items = []
    for it in items_json:
        producto = conn.execute("SELECT * FROM productos WHERE id = ?", (it["producto_id"],)).fetchone()
        if not producto:
            continue
        cantidad = max(1, int(it.get("cantidad") or 1))

        # Precio del catalogo por defecto; si el vendedor lo edito a mano al
        # armar la cotizacion (para negociar con el cliente), se respeta ese
        # valor en vez del precio del catalogo. Cualquier valor invalido o
        # en 0/negativo se ignora y cae de vuelta al precio del catalogo.
        precio_unitario = producto["precio"]
        precio_manual = it.get("precio_unitario")
        if precio_manual not in (None, ""):
            try:
                precio_manual = round(float(precio_manual), 2)
                if precio_manual > 0:
                    precio_unitario = precio_manual
            except (TypeError, ValueError):
                pass

        items.append(
            {
                "producto_id": producto["id"],
                "marca": producto["marca"],
                "modelo": producto["modelo"],
                "medida": producto["medida"],
                "precio_unitario": precio_unitario,
                "cantidad": cantidad,
                "imagen_procesada": producto["imagen_procesada"],
            }
        )
    return items


@app.route("/api/cotizar/generar", methods=["POST"])
def api_cotizar_generar():
    """Crea (o reutiliza) el contacto, arma el PDF de la cotizacion, la
    guarda en el historial y devuelve el link de WhatsApp para enviarla."""
    datos = request.get_json(force=True)
    ajustes = obtener_ajustes()
    conn = database.get_connection()
    try:
        contacto_id = datos.get("contacto_id")
        if contacto_id:
            contacto_existente = conn.execute(
                "SELECT * FROM contactos WHERE id = ?", (contacto_id,)
            ).fetchone()
            if not contacto_existente or not _puede_ver_contacto(contacto_existente):
                return jsonify({"error": "Ese contacto no es tuyo."}), 403
        else:
            nombre = (datos.get("nombre") or "").strip()
            telefono = (datos.get("telefono") or "").strip()
            if not nombre or not telefono:
                return jsonify({"error": "Falta nombre o telefono del contacto."}), 400
            sql_existe = "SELECT id FROM contactos WHERE telefono = ?"
            params_existe = [telefono]
            if not auth.es_admin():
                sql_existe += " AND vendedor_id = ?"
                params_existe.append(g.usuario["id"])
            existente = conn.execute(sql_existe, params_existe).fetchone()
            if existente:
                contacto_id = existente["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO contactos (nombre, telefono, origen, estado, creado_en, vendedor_id) "
                    "VALUES (?, ?, 'cotizador', 'nuevo', ?, ?)",
                    (nombre, telefono, database.ahora_iso(), g.usuario["id"]),
                )
                conn.commit()
                contacto_id = cursor.lastrowid

        contacto = conn.execute("SELECT * FROM contactos WHERE id = ?", (contacto_id,)).fetchone()
        items = _items_desde_json(conn, datos.get("items", []))
        if not items:
            return jsonify({"error": "Selecciona al menos un neumatico."}), 400
        total_catalogo = sum(it["cantidad"] * it["precio_unitario"] for it in items)

        tipo = datos.get("tipo") or "proforma"
        if tipo not in ("cotizacion", "proforma"):
            tipo = "proforma"

        iva_manual = datos.get("iva_manual")
        iva_manual = float(iva_manual) if iva_manual not in (None, "") else None
        totales = calcular_totales(total_catalogo, iva_manual)

        # Texto corto que abre el chat de WhatsApp (el detalle va en el PDF adjunto).
        mensaje = whatsapp.construir_mensaje_corto(contacto["nombre"], totales["total"], ajustes["plantilla_mensaje"])

        fecha = database.ahora_iso()
        cursor = conn.execute(
            "INSERT INTO cotizaciones (contacto_id, fecha, tipo, subtotal, iva, iva_manual, total, mensaje_texto) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (contacto_id, fecha, tipo, totales["subtotal"], totales["iva"], int(totales["iva_manual"]), totales["total"], mensaje),
        )
        cotizacion_id = cursor.lastrowid
        for it in items:
            conn.execute(
                "INSERT INTO cotizacion_items (cotizacion_id, producto_id, marca, modelo, medida, "
                "cantidad, precio_unitario) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cotizacion_id, it["producto_id"], it["marca"], it["modelo"], it["medida"], it["cantidad"], it["precio_unitario"]),
            )

        ruta_logo = None
        if ajustes.get("logo_archivo"):
            posible = database.LOGO_DIR / ajustes["logo_archivo"]
            if posible.exists():
                ruta_logo = posible

        nombre_pdf = pdf_cotizacion.nombre_archivo_pdf(cotizacion_id, contacto["nombre"], tipo)
        ruta_pdf = database.COTIZACIONES_DIR / nombre_pdf
        pdf_cotizacion.generar_pdf_cotizacion(
            ruta_pdf,
            folio=cotizacion_id,
            fecha=fecha,
            ajustes=ajustes,
            contacto=dict(contacto),
            items=items,
            tipo=tipo,
            subtotal=totales["subtotal"],
            iva=totales["iva"],
            total=totales["total"],
            ruta_logo=ruta_logo,
        )
        conn.execute(
            "UPDATE cotizaciones SET pdf_archivo = ? WHERE id = ?", (nombre_pdf, cotizacion_id)
        )

        nuevo_estado = "cotizado" if contacto["estado"] == "nuevo" else contacto["estado"]
        conn.execute(
            "UPDATE contactos SET ultimo_contacto = ?, estado = ? WHERE id = ?",
            (fecha, nuevo_estado, contacto_id),
        )

        etiqueta_tipo = "proforma" if tipo == "proforma" else "cotizacion"
        registrar_actividad(
            conn, "cotizacion",
            f"Genero una {etiqueta_tipo} para {contacto['nombre']} por ${totales['total']:,.2f}",
        )
        conn.commit()

        link_wa = whatsapp.construir_link_wa(contacto["telefono"], mensaje, ajustes["prefijo_telefono"])
    finally:
        conn.close()

    return jsonify(
        {
            "ok": True,
            "wa_link": link_wa,
            "telefono_normalizado": whatsapp.normalizar_telefono(contacto["telefono"], ajustes["prefijo_telefono"]),
            "mensaje": mensaje,
            "contacto_id": contacto_id,
            "cotizacion_id": cotizacion_id,
            "pdf_url": url_for("servir_pdf", nombre=nombre_pdf),
            "pdf_nombre": nombre_pdf,
            "tipo": tipo,
            "subtotal": totales["subtotal"],
            "iva": totales["iva"],
            "total": totales["total"],
        }
    )


@app.route("/api/cotizar/copiar-pdf", methods=["POST"])
def api_copiar_pdf():
    datos = request.get_json(force=True)
    cotizacion_id = datos.get("cotizacion_id")
    conn = database.get_connection()
    try:
        cotizacion = conn.execute(
            "SELECT * FROM cotizaciones WHERE id = ?", (cotizacion_id,)
        ).fetchone()
    finally:
        conn.close()
    if not cotizacion or not cotizacion["pdf_archivo"]:
        return jsonify({"error": "Esa cotizacion no tiene PDF generado."}), 400
    ruta = database.COTIZACIONES_DIR / cotizacion["pdf_archivo"]
    if not ruta.exists():
        return jsonify({"error": "No se encontro el archivo PDF."}), 404
    try:
        portapapeles.copiar_archivo_al_portapapeles(ruta)
    except ImportError:
        # Este equipo no es Windows (ej. la app corriendo en un servidor en
        # la nube): no existe "portapapeles" para copiar el archivo. Hay que
        # descargar el PDF y adjuntarlo a mano en WhatsApp.
        return jsonify({"error": "Este dispositivo no soporta copiar el PDF automaticamente. Descargalo con el boton 'Ver el PDF' y adjuntalo a mano en WhatsApp."}), 501
    except Exception as exc:
        return jsonify({"error": f"No se pudo copiar el PDF: {exc}"}), 500
    return jsonify({"ok": True})


# ----------------------------------------------------------------- ajustes --
@app.route("/ajustes", methods=["GET", "POST"])
@auth.requiere_admin
def ajustes_vista():
    if request.method == "POST":
        cambios = {
            "nombre_negocio": request.form.get("nombre_negocio", "").strip(),
            "ciudad_negocio": request.form.get("ciudad_negocio", "").strip(),
            "telefono_negocio": request.form.get("telefono_negocio", "").strip(),
            "direccion_negocio": request.form.get("direccion_negocio", "").strip(),
            "prefijo_telefono": request.form.get("prefijo_telefono", "593").strip(),
            "validez_dias": request.form.get("validez_dias", "3").strip(),
            "margen_defecto": request.form.get("margen_defecto", "10").strip(),
            "vendedor_nombre": request.form.get("vendedor_nombre", "").strip(),
            "vendedor_cargo": request.form.get("vendedor_cargo", "").strip(),
            "vendedor_telefono": request.form.get("vendedor_telefono", "").strip(),
            "texto_intro": request.form.get("texto_intro", "").strip(),
            "formas_pago": request.form.get("formas_pago", ""),
            "plantilla_mensaje": request.form.get("plantilla_mensaje", ""),
            "pie_mensaje": request.form.get("pie_mensaje", ""),
        }
        logo = request.files.get("logo")
        if logo and logo.filename:
            extension = Path(logo.filename).suffix.lower()
            if extension in EXTENSIONES_IMAGEN:
                nombre_logo = f"logo{extension}"
                logo.save(database.LOGO_DIR / nombre_logo)
                cambios["logo_archivo"] = nombre_logo
            else:
                flash("El logo debe ser jpg, png o webp.", "error")

        database.guardar_ajustes(cambios)

        conn = database.get_connection()
        try:
            ids = [f["id"] for f in conn.execute("SELECT id FROM productos WHERE activo = 1").fetchall()]
            for producto_id in ids:
                regenerar_imagen_producto(conn, producto_id)
            registrar_actividad(conn, "ajustes", "Actualizo los ajustes del negocio")
            conn.commit()
        finally:
            conn.close()

        flash("Ajustes guardados. Se regeneraron las imagenes del catalogo.", "ok")
        return redirect(url_for("ajustes_vista"))

    return render_template("ajustes.html")


# ---------------------------------------------------------------- usuarios --
@app.route("/usuarios")
@auth.requiere_admin
def usuarios():
    conn = database.get_connection()
    try:
        lista = conn.execute("SELECT * FROM usuarios ORDER BY creado_en").fetchall()
    finally:
        conn.close()
    return render_template("usuarios.html", usuarios=lista)


@app.route("/actividad")
@auth.requiere_admin
def actividad():
    """Bitacora de lo que hace cada usuario (solo para administradores):
    cotizaciones generadas/eliminadas, contactos, catalogo, ajustes, logins."""
    usuario_id = request.args.get("usuario_id", type=int)
    conn = database.get_connection()
    try:
        sql = "SELECT * FROM actividad WHERE 1=1"
        params = []
        if usuario_id:
            sql += " AND usuario_id = ?"
            params.append(usuario_id)
        sql += " ORDER BY fecha DESC LIMIT 300"
        registros = conn.execute(sql, params).fetchall()
        usuarios_lista = conn.execute("SELECT id, nombre FROM usuarios ORDER BY nombre").fetchall()
    finally:
        conn.close()
    return render_template(
        "actividad.html", registros=registros, usuarios=usuarios_lista, usuario_id=usuario_id
    )


@app.route("/gerencia")
@auth.requiere_admin
def gerencia():
    """Dashboard de gerencia: panorama del negocio para la administracion -
    a diferencia de Inicio (el dia a dia operativo de cada vendedor), este
    mira tendencias: ventas en el tiempo, embudo del pipeline, productos
    mas cotizados y desempeno por vendedor."""
    rango = request.args.get("rango", "30")  # dias como texto, o 'todo'
    conn = database.get_connection()
    try:
        filtro_fecha = ""
        params_fecha = []
        if rango != "todo":
            dias = int(rango) if rango.isdigit() else 30
            desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
            filtro_fecha = "AND fecha >= ?"
            params_fecha = [desde]

        # --- KPIs generales (solo proformas: son las que tienen un total real) ---
        total_cotizado = conn.execute(
            f"SELECT COALESCE(SUM(total), 0) FROM cotizaciones WHERE tipo = 'proforma' {filtro_fecha}",
            params_fecha,
        ).fetchone()[0]
        total_envios = conn.execute(
            f"SELECT COUNT(*) FROM cotizaciones WHERE 1=1 {filtro_fecha}",
            params_fecha,
        ).fetchone()[0]
        ticket_promedio = (total_cotizado / total_envios) if total_envios else 0

        # --- embudo del pipeline: foto actual de todos los contactos (no se
        # filtra por rango de fechas, es "donde esta cada quien ahora") ---
        embudo = []
        for estado in ESTADOS_VALIDOS:
            cantidad = conn.execute(
                "SELECT COUNT(*) FROM contactos WHERE estado = ?", (estado,)
            ).fetchone()[0]
            valor = conn.execute(
                "SELECT COALESCE(SUM(total), 0) FROM cotizaciones WHERE tipo = 'proforma' "
                "AND contacto_id IN (SELECT id FROM contactos WHERE estado = ?)",
                (estado,),
            ).fetchone()[0]
            embudo.append({"estado": estado, "nombre": NOMBRES_ESTADOS[estado], "cantidad": cantidad, "valor": valor})
        total_contactos_embudo = sum(e["cantidad"] for e in embudo)
        vendidos = next((e["cantidad"] for e in embudo if e["estado"] == "vendido"), 0)
        tasa_conversion = (vendidos / total_contactos_embudo * 100) if total_contactos_embudo else 0

        # --- ventas en el tiempo, agrupadas por dia ---
        serie_tiempo = conn.execute(
            f"SELECT substr(fecha, 1, 10) AS dia, COUNT(*) AS envios, COALESCE(SUM(total), 0) AS valor "
            f"FROM cotizaciones WHERE tipo = 'proforma' {filtro_fecha} GROUP BY dia ORDER BY dia",
            params_fecha,
        ).fetchall()

        # --- productos mas cotizados (por valor) ---
        filtro_fecha_co = filtro_fecha.replace("fecha", "co.fecha")
        top_productos = conn.execute(
            f"SELECT ci.marca, ci.modelo, ci.medida, SUM(ci.cantidad) AS unidades, "
            f"SUM(ci.cantidad * ci.precio_unitario) AS valor "
            f"FROM cotizacion_items ci JOIN cotizaciones co ON co.id = ci.cotizacion_id "
            f"WHERE co.tipo = 'proforma' {filtro_fecha_co} "
            f"GROUP BY ci.marca, ci.modelo, ci.medida ORDER BY valor DESC LIMIT 8",
            params_fecha,
        ).fetchall()

        # --- desempeno por vendedor ---
        vendedores = conn.execute(
            f"SELECT u.id, u.nombre, "
            f"COUNT(DISTINCT co.id) AS envios, COALESCE(SUM(co.total), 0) AS valor_cotizado, "
            f"(SELECT COUNT(*) FROM contactos WHERE vendedor_id = u.id AND estado = 'vendido') AS vendidos "
            f"FROM usuarios u "
            f"LEFT JOIN contactos c ON c.vendedor_id = u.id "
            f"LEFT JOIN cotizaciones co ON co.contacto_id = c.id AND co.tipo = 'proforma' {filtro_fecha_co} "
            f"WHERE u.activo = 1 "
            f"GROUP BY u.id, u.nombre ORDER BY valor_cotizado DESC",
            params_fecha,
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "gerencia.html",
        rango=rango,
        total_cotizado=total_cotizado,
        total_envios=total_envios,
        ticket_promedio=ticket_promedio,
        embudo=embudo,
        tasa_conversion=tasa_conversion,
        serie_tiempo=[dict(f) for f in serie_tiempo],
        top_productos=top_productos,
        vendedores=vendedores,
    )


@app.route("/usuarios/nuevo", methods=["GET", "POST"])
@auth.requiere_admin
def usuario_nuevo():
    if request.method == "GET":
        return render_template("usuario_form.html", usuario=None, es_nuevo=True)

    usuario_input = request.form.get("usuario", "").strip().lower()
    password_input = request.form.get("password", "")
    nombre = request.form.get("nombre", "").strip()
    rol = request.form.get("rol", "vendedor")
    if rol not in ("admin", "vendedor"):
        rol = "vendedor"

    if not usuario_input or not password_input or not nombre:
        flash("Usuario, contraseña y nombre son obligatorios.", "error")
        return render_template("usuario_form.html", usuario=request.form, es_nuevo=True)
    if len(password_input) < 4:
        flash("La contraseña debe tener al menos 4 caracteres.", "error")
        return render_template("usuario_form.html", usuario=request.form, es_nuevo=True)

    conn = database.get_connection()
    try:
        existe = conn.execute("SELECT 1 FROM usuarios WHERE usuario = ?", (usuario_input,)).fetchone()
        if existe:
            flash("Ya existe un usuario con ese nombre de acceso.", "error")
            return render_template("usuario_form.html", usuario=request.form, es_nuevo=True)
        conn.execute(
            "INSERT INTO usuarios (usuario, password_hash, nombre, rol, activo, creado_en) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (usuario_input, generate_password_hash(password_input), nombre, rol, database.ahora_iso()),
        )
        registrar_actividad(conn, "usuario", f"Creo el usuario {nombre} ({usuario_input}, {rol})")
        conn.commit()
    finally:
        conn.close()

    flash(f"Usuario {usuario_input} creado.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<int:usuario_id>/editar", methods=["GET", "POST"])
@auth.requiere_admin
def usuario_editar(usuario_id):
    conn = database.get_connection()
    try:
        fila = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        if not fila:
            flash("Ese usuario no existe.", "error")
            return redirect(url_for("usuarios"))

        if request.method == "GET":
            return render_template("usuario_form.html", usuario=fila, es_nuevo=False)

        nombre = request.form.get("nombre", "").strip()
        rol = request.form.get("rol", "vendedor")
        if rol not in ("admin", "vendedor"):
            rol = "vendedor"
        activo = 1 if request.form.get("activo") else 0
        password_nueva = request.form.get("password", "")

        if not nombre:
            flash("El nombre es obligatorio.", "error")
            return render_template("usuario_form.html", usuario=fila, es_nuevo=False)

        # No permitir desactivar o quitarle el rol admin al ultimo admin activo.
        if fila["rol"] == "admin" and (rol != "admin" or not activo):
            otros_admins = conn.execute(
                "SELECT COUNT(*) FROM usuarios WHERE rol = 'admin' AND activo = 1 AND id != ?",
                (usuario_id,),
            ).fetchone()[0]
            if otros_admins == 0:
                flash("No puedes quitarle el rol de administrador ni desactivar al ultimo admin.", "error")
                return render_template("usuario_form.html", usuario=fila, es_nuevo=False)

        if password_nueva:
            if len(password_nueva) < 4:
                flash("La contraseña debe tener al menos 4 caracteres.", "error")
                return render_template("usuario_form.html", usuario=fila, es_nuevo=False)
            conn.execute(
                "UPDATE usuarios SET nombre=?, rol=?, activo=?, password_hash=? WHERE id=?",
                (nombre, rol, activo, generate_password_hash(password_nueva), usuario_id),
            )
            registrar_actividad(conn, "usuario", f"Edito el usuario {nombre} y le cambio la contraseña")
        else:
            conn.execute(
                "UPDATE usuarios SET nombre=?, rol=?, activo=? WHERE id=?",
                (nombre, rol, activo, usuario_id),
            )
            registrar_actividad(conn, "usuario", f"Edito el usuario {nombre}")
        conn.commit()
    finally:
        conn.close()

    flash("Usuario actualizado.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/")
def inicio():
    # Si ya hay una sesion iniciada, no tiene caso mostrarle la bienvenida
    # de nuevo: va directo a cotizar. Si no, se ve la pantalla animada con
    # el logo y el boton "Iniciar" antes de pedir usuario/contraseña.
    if g.get("usuario"):
        return redirect(url_for("dashboard"))
    return render_template("inicio.html")


def _obtener_ip_local():
    """IP de este equipo dentro de la red local (la que usan otros
    dispositivos en el mismo WiFi para conectarse). No envia datos a
    ningun lado: solo le pregunta al sistema operativo que interfaz usaria
    para llegar a una IP externa."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _esperar_servidor_y_abrir_navegador(puerto=5000, intentos=60):
    """Espera a que el servidor realmente este escuchando antes de abrir el
    navegador. Con un temporizador fijo, en equipos lentos (antivirus,
    disco lento, primer arranque) el navegador se adelantaba y mostraba
    'no se puede acceder a este sitio' porque el servidor aun no estaba listo."""
    for _ in range(intentos):
        try:
            with socket.create_connection(("127.0.0.1", puerto), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)

    ip_local = _obtener_ip_local()
    print("\n" + "=" * 60, flush=True)
    print(f"  En esta computadora:        http://localhost:{puerto}", flush=True)
    if ip_local:
        print(f"  Desde otro dispositivo:     http://{ip_local}:{puerto}", flush=True)
        print("  (el otro dispositivo debe estar en la misma red WiFi)", flush=True)
    print("=" * 60 + "\n", flush=True)

    webbrowser.open(f"http://127.0.0.1:{puerto}/")


if __name__ == "__main__":
    threading.Thread(target=_esperar_servidor_y_abrir_navegador, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
