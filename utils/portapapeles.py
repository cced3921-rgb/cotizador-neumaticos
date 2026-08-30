"""Copia una imagen o un archivo directo al portapapeles de Windows para
poder pegarlo (Ctrl+V) en WhatsApp sin tener que guardarlo ni arrastrarlo
manualmente."""
import ctypes
import io
from pathlib import Path

from PIL import Image


def copiar_imagen_al_portapapeles(ruta_imagen: Path):
    import win32clipboard  # import local: solo existe en Windows

    imagen = Image.open(ruta_imagen).convert("RGB")
    buffer = io.BytesIO()
    imagen.save(buffer, "BMP")
    datos_dib = buffer.getvalue()[14:]  # quita la cabecera BMP; deja el DIB
    buffer.close()

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, datos_dib)
    finally:
        win32clipboard.CloseClipboard()


class _DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", ctypes.c_uint32),
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
        ("fNC", ctypes.c_int32),
        ("fWide", ctypes.c_int32),
    ]


def copiar_archivo_al_portapapeles(ruta_archivo: Path):
    """Copia un archivo (ej. el PDF de la cotizacion) al portapapeles como si
    se hubiera copiado desde el Explorador de Windows: al pegar (Ctrl+V) en
    WhatsApp, Outlook, etc. se adjunta el archivo, no su contenido."""
    import win32clipboard  # import local: solo existe en Windows
    import win32con

    ruta_absoluta = str(Path(ruta_archivo).resolve())

    cabecera = _DROPFILES()
    cabecera.pFiles = ctypes.sizeof(_DROPFILES)
    cabecera.fWide = 1  # la lista de rutas viene en UTF-16 (wide chars)

    lista_rutas = ctypes.create_unicode_buffer(ruta_absoluta + "\0\0")
    datos = bytes(cabecera) + bytes(lista_rutas)

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, datos)
    finally:
        win32clipboard.CloseClipboard()
