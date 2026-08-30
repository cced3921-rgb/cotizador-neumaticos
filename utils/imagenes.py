"""Genera automaticamente la imagen 'lista para enviar' de cada neumatico:
la foto original + una franja con marca, medida y precio superpuestos.
Esto reemplaza la edicion manual imagen por imagen.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ANCHO_OBJETIVO = 1000
ALTO_FRANJA = 170

FUENTES_CANDIDATAS = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
]

_CACHE_FUENTES = {}


def _cargar_fuente(tamano: int):
    if tamano in _CACHE_FUENTES:
        return _CACHE_FUENTES[tamano]
    fuente = None
    for ruta in FUENTES_CANDIDATAS:
        if Path(ruta).exists():
            try:
                fuente = ImageFont.truetype(ruta, tamano)
                break
            except Exception:
                continue
    if fuente is None:
        fuente = ImageFont.load_default()
    _CACHE_FUENTES[tamano] = fuente
    return fuente


def generar_imagen_lista(
    ruta_origen: Path,
    ruta_destino: Path,
    marca: str,
    modelo: str,
    medida: str,
    precio: float,
    nombre_negocio: str = "",
    ruta_logo: Path | None = None,
):
    """Crea la imagen final (foto + franja con datos) y la guarda en ruta_destino."""
    imagen = Image.open(ruta_origen).convert("RGB")
    ancho, alto = imagen.size
    if ancho != ANCHO_OBJETIVO:
        alto_nuevo = int(alto * (ANCHO_OBJETIVO / ancho))
        imagen = imagen.resize((ANCHO_OBJETIVO, alto_nuevo))
        ancho, alto = ANCHO_OBJETIVO, alto_nuevo

    lienzo = Image.new("RGB", (ancho, alto + ALTO_FRANJA), "white")
    lienzo.paste(imagen, (0, 0))

    draw = ImageDraw.Draw(lienzo)
    draw.rectangle([0, alto, ancho, alto + ALTO_FRANJA], fill=(18, 18, 20))

    fuente_titulo = _cargar_fuente(40)
    fuente_sub = _cargar_fuente(26)
    fuente_precio = _cargar_fuente(50)
    fuente_negocio = _cargar_fuente(20)

    titulo = f"{marca} {modelo}".strip()
    draw.text((25, alto + 14), titulo, font=fuente_titulo, fill="white")
    draw.text((25, alto + 64), f"Medida: {medida}", font=fuente_sub, fill=(210, 210, 210))

    texto_precio = f"${precio:,.2f}"
    caja = draw.textbbox((0, 0), texto_precio, font=fuente_precio)
    ancho_precio = caja[2] - caja[0]
    draw.text(
        (ancho - ancho_precio - 25, alto + 14),
        texto_precio,
        font=fuente_precio,
        fill=(90, 220, 130),
    )

    espacio_logo = 0
    if ruta_logo and Path(ruta_logo).exists():
        try:
            logo = Image.open(ruta_logo).convert("RGBA")
            alto_logo = 60
            ancho_logo = int(logo.width * (alto_logo / logo.height))
            logo = logo.resize((ancho_logo, alto_logo))
            lienzo.paste(
                logo,
                (ancho - ancho_logo - 20, alto + ALTO_FRANJA - alto_logo - 15),
                logo,
            )
            espacio_logo = ancho_logo + 30
        except Exception:
            espacio_logo = 0

    if nombre_negocio:
        draw.text(
            (25, alto + ALTO_FRANJA - 38),
            nombre_negocio,
            font=fuente_negocio,
            fill=(170, 170, 170),
        )

    ruta_destino.parent.mkdir(parents=True, exist_ok=True)
    lienzo.save(ruta_destino, quality=90)
    return ruta_destino
