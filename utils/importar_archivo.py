"""Lectura generica de un archivo Excel (.xlsx/.xlsm) o CSV: devuelve los
encabezados (en minusculas) y las filas de datos. Lo usan tanto la
importacion de catalogo como la de contactos, para no duplicar el parseo."""
import csv
import io
from pathlib import Path

import openpyxl

EXTENSIONES_SOPORTADAS = {".xlsx", ".xlsm", ".csv"}


def _normalizar_encabezados(encabezados):
    return [str(h).strip().lower() if h is not None else "" for h in encabezados]


def leer_tabla(nombre_archivo: str, contenido: bytes):
    """Devuelve (encabezados_normalizados, filas_de_datos). Lanza ValueError
    si el formato no esta soportado o el archivo esta vacio."""
    extension = Path(nombre_archivo).suffix.lower()
    if extension not in EXTENSIONES_SOPORTADAS:
        raise ValueError("Formato no soportado. Usa un archivo .xlsx o .csv")

    if extension == ".csv":
        texto = contenido.decode("utf-8-sig", errors="replace")
        filas = list(csv.reader(io.StringIO(texto)))
    else:
        libro = openpyxl.load_workbook(io.BytesIO(contenido), data_only=True)
        hoja = libro.active
        filas = list(hoja.iter_rows(values_only=True))

    if not filas:
        raise ValueError("El archivo esta vacio.")

    encabezados, *resto = filas
    return _normalizar_encabezados(encabezados), resto
