"""Importacion masiva del catalogo desde Excel (.xlsx) o CSV, para no tener
que dar de alta cada producto a mano.

Columnas esperadas (no distingue mayusculas/minusculas):
    marca, modelo, medida, costo, precio, stock, imagen, categoria

"categoria" es opcional: si no se incluye (o queda vacia en una fila), el
producto se guarda como "llantas" - asi un archivo de precios viejo, sin esa
columna, sigue funcionando igual que antes. Para cargar aros, aceites u otra
seccion nueva del catalogo, se pone el nombre de esa seccion en esa columna.

"costo" y "precio" son opcionales, pero hace falta al menos una de las dos
por fila:
  - Si se da "costo" y no "precio" (o "precio" queda vacio/0): el precio de
    venta se calcula solo, como costo + el margen de ganancia configurado
    en Ajustes (por defecto 10%).
  - Si se da "precio" explicito: se usa tal cual, sin tocarlo (por si algun
    neumatico puntual necesita un precio distinto al calculado).

La columna "imagen" es el nombre de archivo de una foto que ya debe estar
copiada en data/imagenes/ (por ejemplo "205-55-r16.jpg"). Si se deja vacia,
el producto queda sin foto y se puede subir despues editandolo.

El catalogo se muestra despues en el mismo orden en que aparecen las filas
en este archivo (no alfabetico) - asi el listado coincide con la lista de
precios real del negocio.
"""
from utils.importar_archivo import leer_tabla

COLUMNAS_REQUERIDAS = {"marca", "modelo", "medida"}


def importar_catalogo(nombre_archivo: str, contenido: bytes, margen_defecto: float = 10.0):
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

        costo_crudo = datos.get("costo")
        precio_crudo = datos.get("precio")
        try:
            costo = float(costo_crudo) if costo_crudo not in (None, "") else 0.0
        except (TypeError, ValueError):
            errores.append(f"Fila {numero_fila}: costo invalido.")
            continue
        try:
            precio_explicito = float(precio_crudo) if precio_crudo not in (None, "") else None
        except (TypeError, ValueError):
            errores.append(f"Fila {numero_fila}: precio invalido.")
            continue

        if precio_explicito and precio_explicito > 0:
            precio = round(precio_explicito, 2)
        elif costo > 0:
            precio = round(costo * (1 + margen_defecto / 100), 2)
        else:
            errores.append(f"Fila {numero_fila}: falta precio o costo.")
            continue

        try:
            stock = int(float(datos.get("stock") or 0))
        except (TypeError, ValueError):
            stock = 0

        imagen = str(datos.get("imagen") or "").strip()
        categoria = str(datos.get("categoria") or "").strip().lower() or "llantas"
        productos.append(
            {
                "marca": marca,
                "modelo": modelo,
                "medida": medida,
                "costo": costo,
                "precio": precio,
                "stock": stock,
                "imagen": imagen,
                "categoria": categoria,
                # Posicion dentro del archivo (1, 2, 3...), para que el
                # catalogo se muestre despues en el mismo orden exacto de la
                # lista de precios, no alfabetico.
                "orden": len(productos) + 1,
            }
        )
    return productos, errores
