"""Capa de base de datos (SQLite) para el Cotizador de Neumaticos."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# En local, los datos viven dentro del proyecto (carpeta data/). En un
# servidor (Render, etc.) hay que apuntar esto a un disco persistente
# (variable de entorno DATA_DIR) para que la base, los PDFs y las imagenes
# no se borren cada vez que se actualiza el codigo.
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data"))
DB_PATH = DATA_DIR / "catalogo.db"
IMAGENES_DIR = DATA_DIR / "imagenes"
PROCESADAS_DIR = DATA_DIR / "procesadas"
LOGO_DIR = DATA_DIR / "logo"
COTIZACIONES_DIR = DATA_DIR / "cotizaciones"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS productos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marca TEXT NOT NULL,
    modelo TEXT NOT NULL,
    medida TEXT NOT NULL,
    precio REAL NOT NULL DEFAULT 0,
    stock INTEGER NOT NULL DEFAULT 0,
    imagen_original TEXT,
    imagen_procesada TEXT,
    descripcion TEXT,
    activo INTEGER NOT NULL DEFAULT 1,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    nombre TEXT NOT NULL,
    rol TEXT NOT NULL DEFAULT 'vendedor',
    activo INTEGER NOT NULL DEFAULT 1,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contactos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    telefono TEXT NOT NULL,
    origen TEXT,
    estado TEXT NOT NULL DEFAULT 'nuevo',
    notas TEXT,
    creado_en TEXT NOT NULL,
    ultimo_contacto TEXT,
    vendedor_id INTEGER REFERENCES usuarios(id)
);

CREATE TABLE IF NOT EXISTS cotizaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contacto_id INTEGER NOT NULL,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'proforma',
    subtotal REAL NOT NULL DEFAULT 0,
    iva REAL NOT NULL DEFAULT 0,
    iva_manual INTEGER NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    mensaje_texto TEXT,
    pdf_archivo TEXT,
    FOREIGN KEY (contacto_id) REFERENCES contactos(id)
);

CREATE TABLE IF NOT EXISTS cotizacion_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cotizacion_id INTEGER NOT NULL,
    producto_id INTEGER,
    marca TEXT,
    modelo TEXT,
    medida TEXT,
    cantidad INTEGER NOT NULL DEFAULT 1,
    precio_unitario REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (cotizacion_id) REFERENCES cotizaciones(id)
);

CREATE TABLE IF NOT EXISTS ajustes (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

CREATE TABLE IF NOT EXISTS actividad (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER,
    usuario_nombre TEXT NOT NULL,
    accion TEXT NOT NULL,
    descripcion TEXT NOT NULL,
    fecha TEXT NOT NULL,
    FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
);
"""

AJUSTES_POR_DEFECTO = {
    "nombre_negocio": "Mi Distribuidora de Neumaticos",
    "ciudad_negocio": "",
    "telefono_negocio": "",
    "direccion_negocio": "",
    "prefijo_telefono": "593",
    "logo_archivo": "",
    "validez_dias": "3",
    "vendedor_nombre": "",
    "vendedor_cargo": "Ejecutivo de ventas",
    "vendedor_telefono": "",
    "texto_intro": (
        "Con atento saludo me permito remitir para su consideracion, proforma "
        "comercial con costos de los neumaticos que usted requiera."
    ),
    "formas_pago": (
        "Efectivo (100%) contra entrega\n"
        "Transferencia bancaria / Deposito\n"
        "Tarjeta de credito"
    ),
    "plantilla_mensaje": (
        "Hola {nombre} \U0001F44B, aqui tu cotizacion de neumaticos en PDF. "
        "Total: ${total}"
    ),
    "pie_mensaje": (
        "Los valores indicados en esta proforma incluyen IVA. Garantia de "
        "fabrica. Escribenos para coordinar el envio y la entrega."
    ),
}

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Crea carpetas y tablas si no existen, e inserta ajustes por defecto."""
    for carpeta in (DATA_DIR, IMAGENES_DIR, PROCESADAS_DIR, LOGO_DIR, COTIZACIONES_DIR):
        carpeta.mkdir(parents=True, exist_ok=True)

    conn = get_connection()
    try:
        conn.executescript(ESQUEMA)
        for columna_sql in (
            "ALTER TABLE cotizaciones ADD COLUMN pdf_archivo TEXT",
            "ALTER TABLE cotizaciones ADD COLUMN subtotal REAL NOT NULL DEFAULT 0",
            "ALTER TABLE cotizaciones ADD COLUMN iva REAL NOT NULL DEFAULT 0",
            "ALTER TABLE cotizaciones ADD COLUMN iva_manual INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cotizaciones ADD COLUMN tipo TEXT NOT NULL DEFAULT 'proforma'",
            "ALTER TABLE contactos ADD COLUMN vendedor_id INTEGER REFERENCES usuarios(id)",
        ):
            try:
                conn.execute(columna_sql)
            except sqlite3.OperationalError:
                pass  # la columna ya existe (base de datos creada con una version anterior)

        # No se crea ningun usuario por defecto (ni contraseñas predefinidas):
        # si todavia no hay ningun usuario, app.py redirige a /configurar para
        # que la primera persona que abra el sistema cree su propia cuenta de
        # administrador con su propia contraseña.

        # Para cotizaciones ya guardadas antes de este cambio, el total
        # historico equivale al total con IVA incluido: reconstruimos
        # subtotal/iva a partir de el para no dejar ceros en el historial.
        conn.execute(
            "UPDATE cotizaciones SET subtotal = total / 1.15, iva = total - (total / 1.15) "
            "WHERE subtotal = 0 AND iva = 0 AND total > 0"
        )
        for clave, valor in AJUSTES_POR_DEFECTO.items():
            fila = conn.execute(
                "SELECT 1 FROM ajustes WHERE clave = ?", (clave,)
            ).fetchone()
            if not fila:
                conn.execute(
                    "INSERT INTO ajustes (clave, valor) VALUES (?, ?)",
                    (clave, valor),
                )

        # Migracion: la plantilla del mensaje usada antes de que la cotizacion
        # se enviara como PDF incluia {detalle} (el listado de neumaticos) en
        # el propio texto de WhatsApp. Ahora ese detalle va dentro del PDF, asi
        # que si detectamos esa plantilla vieja la reemplazamos por la nueva.
        fila = conn.execute(
            "SELECT valor FROM ajustes WHERE clave = 'plantilla_mensaje'"
        ).fetchone()
        if fila and "{detalle}" in (fila["valor"] or ""):
            conn.execute(
                "UPDATE ajustes SET valor = ? WHERE clave = 'plantilla_mensaje'",
                (AJUSTES_POR_DEFECTO["plantilla_mensaje"],),
            )

        conn.commit()
    finally:
        conn.close()


def ahora_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def obtener_ajustes():
    conn = get_connection()
    try:
        filas = conn.execute("SELECT clave, valor FROM ajustes").fetchall()
        ajustes = dict(AJUSTES_POR_DEFECTO)
        ajustes.update({fila["clave"]: fila["valor"] for fila in filas})
        return ajustes
    finally:
        conn.close()


def guardar_ajustes(cambios: dict):
    conn = get_connection()
    try:
        for clave, valor in cambios.items():
            conn.execute(
                "INSERT INTO ajustes (clave, valor) VALUES (?, ?) "
                "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
                (clave, valor),
            )
        conn.commit()
    finally:
        conn.close()
