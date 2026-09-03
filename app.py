"""Cotizador de Neumaticos - app local para armar y enviar cotizaciones por
WhatsApp en el menor numero de clics posible, con catalogo, generacion
automatica de imagenes y un CRM simple de contactos."""
import os
import socket
import threading
import time
import webbrowser
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
# Render (y la mayoria de hostings) sirven la app por HTTPS y definen esta
# variable: ahi exigimos que la cookie de sesion solo viaje por HTTPS. En
# local (HTTP simple) no se exige, o el navegador la descartaria.
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

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
    return redirect(url_for("cotizar"))


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
        return redirect(url_for("cotizar"))
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
    siguiente = request.args.get("siguiente") or url_for("cotizar")
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
@app.route("/catalogo")
def catalogo():
    q = request.args.get("q", "").strip()
    conn = database.get_connection()
    try:
        if q:
            like = f"%{q}%"
            productos = conn.execute(
                "SELECT * FROM productos WHERE activo = 1 AND "
                "(marca LIKE ? OR modelo LIKE ? OR medida LIKE ?) "
                "ORDER BY marca, modelo",
                (like, like, like),
            ).fetchall()
        else:
            productos = conn.execute(
                "SELECT * FROM productos WHERE activo = 1 ORDER BY marca, modelo"
            ).fetchall()
    finally:
        conn.close()
    return render_template("catalogo.html", productos=productos, q=q)


@app.route("/catalogo/nuevo", methods=["GET", "POST"])
@auth.requiere_admin
def producto_nuevo():
    if request.method == "GET":
        return render_template("producto_form.html", producto=None)

    marca = request.form.get("marca", "").strip()
    modelo = request.form.get("modelo", "").strip()
    medida = request.form.get("medida", "").strip()
    precio = float(request.form.get("precio") or 0)
    stock = int(request.form.get("stock") or 0)
    descripcion = request.form.get("descripcion", "").strip()

    if not marca or not modelo or not medida:
        flash("Marca, modelo y medida son obligatorios.", "error")
        return render_template("producto_form.html", producto=request.form)

    imagen_original = _guardar_imagen_subida(request.files.get("imagen"))

    conn = database.get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO productos (marca, modelo, medida, precio, stock, "
            "imagen_original, descripcion, activo, creado_en) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (marca, modelo, medida, precio, stock, imagen_original, descripcion, database.ahora_iso()),
        )
        conn.commit()
        producto_id = cursor.lastrowid
        if imagen_original:
            regenerar_imagen_producto(conn, producto_id)
    finally:
        conn.close()

    flash(f"Neumatico {marca} {modelo} agregado al catalogo.", "ok")
    return redirect(url_for("catalogo"))


@app.route("/catalogo/<int:producto_id>/editar", methods=["GET", "POST"])
@auth.requiere_admin
def producto_editar(producto_id):
    conn = database.get_connection()
    try:
        producto = conn.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
        if not producto:
            flash("Ese neumatico no existe.", "error")
            return redirect(url_for("catalogo"))

        if request.method == "GET":
            return render_template("producto_form.html", producto=producto)

        marca = request.form.get("marca", "").strip()
        modelo = request.form.get("modelo", "").strip()
        medida = request.form.get("medida", "").strip()
        precio = float(request.form.get("precio") or 0)
        stock = int(request.form.get("stock") or 0)
        descripcion = request.form.get("descripcion", "").strip()

        if not marca or not modelo or not medida:
            flash("Marca, modelo y medida son obligatorios.", "error")
            return render_template("producto_form.html", producto=producto)

        imagen_nueva = _guardar_imagen_subida(request.files.get("imagen"))
        imagen_original = imagen_nueva or producto["imagen_original"]

        conn.execute(
            "UPDATE productos SET marca=?, modelo=?, medida=?, precio=?, stock=?, "
            "imagen_original=?, descripcion=? WHERE id=?",
            (marca, modelo, medida, precio, stock, imagen_original, descripcion, producto_id),
        )
        conn.commit()
        if imagen_original:
            regenerar_imagen_producto(conn, producto_id)
    finally:
        conn.close()

    flash("Neumatico actualizado.", "ok")
    return redirect(url_for("catalogo"))


@app.route("/catalogo/<int:producto_id>/eliminar", methods=["POST"])
@auth.requiere_admin
def producto_eliminar(producto_id):
    conn = database.get_connection()
    try:
        conn.execute("UPDATE productos SET activo = 0 WHERE id = ?", (producto_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Neumatico eliminado del catalogo.", "ok")
    return redirect(url_for("catalogo"))


@app.route("/catalogo/importar", methods=["POST"])
@auth.requiere_admin
def catalogo_importar():
    archivo = request.files.get("archivo")
    if not archivo or not archivo.filename:
        flash("Selecciona un archivo .xlsx o .csv para importar.", "error")
        return redirect(url_for("catalogo"))

    try:
        productos, errores = importador.importar_catalogo(archivo.filename, archivo.read())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("catalogo"))

    conn = database.get_connection()
    insertados = 0
    try:
        for p in productos:
            imagen_original = p["imagen"] if p["imagen"] and (database.IMAGENES_DIR / p["imagen"]).exists() else None
            cursor = conn.execute(
                "INSERT INTO productos (marca, modelo, medida, precio, stock, "
                "imagen_original, activo, creado_en) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (p["marca"], p["modelo"], p["medida"], p["precio"], p["stock"], imagen_original, database.ahora_iso()),
            )
            conn.commit()
            if imagen_original:
                regenerar_imagen_producto(conn, cursor.lastrowid)
            insertados += 1
    finally:
        conn.close()

    mensaje = f"Se importaron {insertados} neumaticos."
    if errores:
        mensaje += f" ({len(errores)} filas con problemas: {'; '.join(errores[:5])})"
    flash(mensaje, "ok" if insertados else "error")
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
        conn.execute("UPDATE contactos SET estado = ? WHERE id = ?", (estado, contacto_id))
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
            "SELECT co.*, ct.vendedor_id AS contacto_vendedor_id "
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
        conn.commit()
        contacto_id = cursor.lastrowid
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
        conn.execute(
            "UPDATE contactos SET estado = ?, notas = ? WHERE id = ?",
            (estado, notas, contacto_id),
        )
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
            "SELECT * FROM productos WHERE activo = 1 ORDER BY marca, modelo"
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


def _items_desde_json(conn, items_json):
    items = []
    for it in items_json:
        producto = conn.execute("SELECT * FROM productos WHERE id = ?", (it["producto_id"],)).fetchone()
        if not producto:
            continue
        cantidad = max(1, int(it.get("cantidad") or 1))
        items.append(
            {
                "producto_id": producto["id"],
                "marca": producto["marca"],
                "modelo": producto["modelo"],
                "medida": producto["medida"],
                "precio_unitario": producto["precio"],
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
        else:
            conn.execute(
                "UPDATE usuarios SET nombre=?, rol=?, activo=? WHERE id=?",
                (nombre, rol, activo, usuario_id),
            )
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
        return redirect(url_for("cotizar"))
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
