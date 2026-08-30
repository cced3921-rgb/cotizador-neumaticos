"""Importacion masiva del catalogo desde Excel (.xlsx) o CSV, para no tener
que dar de alta cada neumatico a mano.

Columnas esperadas (no distingue mayusculas/minusculas):
    marca, modelo, medida, precio, stock, imagen

La columna "imagen" es el nombre de archivo de una foto que ya debe estar
copiada en data/imagenes/ (por ejemplo "205-55-r16.jpg"). Si se deja vacia,
el producto queda sin foto y se puede subir despues editandolo.
"""
from utils.importar_archivo import leer_tabla

COLUMNAS_REQUERIDAS = {"marca", "modelo", "medida", "precio"}


def importar_catalogo(nombre_archivo: str, contenido: bytes):
    encabezados, filas_crudas = leer_tabla(nombre_archivo, contenido)
    faltantes = COLUMNAS_REQUERIDAS - set(encabezados)
    if faltantes:
        raise ValueError(
            "Faltan columnas obligatorias en el archivo: " + ", ".join(sorted(faltantes))
        )

    productos = []
    errores = []
    for numero_fila, fila in enumerate(filas_crudas, start=2):
        datos = dict(zip(encabezados, fila))
        marca = str(datos.get("marca") or "").strip()
        modelo = str(datos.get("modelo") or "").strip()
        medida = str(datos.get("medida") or "").strip()
        if not marca and not modelo and not medida:
            continue  # fila vacia, se ignora
        if not marca or not modelo or not medida:
            errores.append(f"Fila {numero_fila}: falta marca, modelo o medida.")
            continue
        try:
            precio = float(datos.get("precio") or 0)
        except (TypeError, ValueError):
            errores.append(f"Fila {numero_fila}: precio invalido.")
            continue
        try:
            stock = int(float(datos.get("stock") or 0))
        except (TypeError, ValueError):
            stock = 0

        imagen = str(datos.get("imagen") or "").strip()
        productos.append(
            {
                "marca": marca,
                "modelo": modelo,
                "medida": medida,
                "precio": precio,
                "stock": stock,
                "imagen": imagen,
            }
        )
    return productos, errores
