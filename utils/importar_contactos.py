"""Importacion masiva de contactos desde Excel (.xlsx) o CSV, para cargar de
un tirón la lista de clientes en vez de crearlos uno por uno.

Columnas esperadas (no distingue mayusculas/minusculas):
    nombre, telefono, origen, notas

Solo "nombre" y "telefono" son obligatorias; "origen" y "notas" son opcionales.
"""
from utils.importar_archivo import leer_tabla

COLUMNAS_REQUERIDAS = {"nombre", "telefono"}


def importar_contactos(nombre_archivo: str, contenido: bytes):
    encabezados, filas_crudas = leer_tabla(nombre_archivo, contenido)
    faltantes = COLUMNAS_REQUERIDAS - set(encabezados)
    if faltantes:
        raise ValueError(
            "Faltan columnas obligatorias en el archivo: " + ", ".join(sorted(faltantes))
        )

    contactos = []
    errores = []
    for numero_fila, fila in enumerate(filas_crudas, start=2):
        datos = dict(zip(encabezados, fila))
        nombre = str(datos.get("nombre") or "").strip()
        telefono = str(datos.get("telefono") or "").strip()
        if not nombre and not telefono:
            continue  # fila vacia, se ignora
        if not nombre or not telefono:
            errores.append(f"Fila {numero_fila}: falta nombre o telefono.")
            continue

        origen = str(datos.get("origen") or "").strip()
        notas = str(datos.get("notas") or "").strip()
        contactos.append(
            {
                "nombre": nombre,
                "telefono": telefono,
                "origen": origen,
                "notas": notas,
            }
        )
    return contactos, errores
