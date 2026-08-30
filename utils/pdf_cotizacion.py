"""Genera el PDF de la cotizacion (proforma) replicando el formato real de
Enllantados S.A.: logo, numero de proforma, saludo personalizado, tabla de
precio unitario y por 4 unidades, notas de pago/envio y firma del vendedor."""
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

COLOR_OSCURO = colors.HexColor("#14161a")
COLOR_ROJO = colors.HexColor("#e5233d")
COLOR_AZUL = colors.HexColor("#4472c4")
COLOR_GRIS = colors.HexColor("#6b7280")
COLOR_BORDE = colors.HexColor("#000000")
COLOR_FILA_PAR = colors.HexColor("#f7f8fa")

MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

ANCHO_CONTENIDO = 178 * mm
TASA_IVA_DEFECTO = 0.15


def _fecha_larga(fecha_iso: str) -> str:
    """'2026-08-15 10:00:00' -> '15 de agosto de 2026'."""
    try:
        parte_fecha = fecha_iso.split(" ")[0]
        anio, mes, dia = parte_fecha.split("-")
        return f"{int(dia)} de {MESES_ES[int(mes) - 1]} de {anio}"
    except Exception:
        return fecha_iso


def _slug(texto: str) -> str:
    texto = re.sub(r"[^a-zA-Z0-9]+", "-", texto.strip().lower())
    return texto.strip("-") or "cliente"


def nombre_archivo_pdf(cotizacion_id: int, nombre_cliente: str, tipo: str = "proforma") -> str:
    prefijo = "PRO" if tipo == "proforma" else "COT"
    return f"{prefijo}-{cotizacion_id:05d}_{_slug(nombre_cliente)}.pdf"


def generar_pdf_cotizacion(
    ruta_destino: Path,
    *,
    folio: int,
    fecha: str,
    ajustes: dict,
    contacto: dict,
    items: list,
    subtotal: float,
    iva: float,
    total: float,
    ruta_logo: Path | None = None,
    tipo: str = "proforma",
):
    """items: lista de dicts con marca, modelo, medida, cantidad, precio_unitario.
    Se muestran con precio unitario y precio x4, igual que la proforma modelo;
    la cantidad elegida en el sistema no se refleja en esta tabla.

    tipo='proforma': documento comercial final, con el desglose de Subtotal/IVA/Total
    (el precio que se cobra una vez que el cliente decide).
    tipo='cotizacion': solo el listado informativo de precios, sin IVA ni totales,
    para cuando todavia se le estan mostrando las opciones al cliente."""
    es_proforma = tipo == "proforma"
    etiqueta_documento = "Proforma" if es_proforma else "Cotizacion"
    estilos = getSampleStyleSheet()
    estilo_normal = ParagraphStyle(
        "normal", parent=estilos["Normal"], fontName="Helvetica", fontSize=10.5, leading=15
    )
    estilo_derecha = ParagraphStyle(
        "derecha", parent=estilo_normal, alignment=TA_RIGHT
    )
    estilo_negrita = ParagraphStyle(
        "negrita", parent=estilo_normal, fontName="Helvetica-Bold"
    )

    def _pie_pagina(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(COLOR_GRIS)
        lineas_pie = []
        if ajustes.get("direccion_negocio"):
            lineas_pie.append(ajustes["direccion_negocio"])
        ciudad_pais = ajustes.get("ciudad_negocio", "")
        if ciudad_pais:
            lineas_pie.append(f"{ciudad_pais} - Ecuador")
        y = 14 * mm
        for linea in reversed(lineas_pie):
            canvas.drawRightString(A4[0] - 16 * mm, y, linea)
            y += 4.5 * mm
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(ruta_destino),
        pagesize=A4,
        topMargin=16 * mm,
        bottomMargin=22 * mm,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        title=f"{etiqueta_documento} {folio:05d}",
    )

    elementos = []

    # --- encabezado: logo a la izquierda, caja de numero de proforma a la derecha ---
    celda_logo = ""
    if ruta_logo and Path(ruta_logo).exists():
        try:
            img = Image(str(ruta_logo))
            proporcion = img.imageHeight / img.imageWidth
            img.drawWidth = 78 * mm
            img.drawHeight = 78 * mm * proporcion
            celda_logo = img
        except Exception:
            celda_logo = Paragraph(f"<b>{ajustes.get('nombre_negocio', '')}</b>", estilo_negrita)
    else:
        celda_logo = Paragraph(f"<b>{ajustes.get('nombre_negocio', '')}</b>", estilo_negrita)

    caja_folio = Table(
        [[Paragraph(f"{etiqueta_documento} N&deg;: <font color='#e5233d'><b>{folio:05d}</b></font>", estilo_normal)]],
        colWidths=[62 * mm],
    )
    caja_folio.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, COLOR_BORDE),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    tabla_encabezado = Table(
        [[celda_logo, caja_folio]],
        colWidths=[116 * mm, 62 * mm],
    )
    tabla_encabezado.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]
        )
    )
    elementos.append(tabla_encabezado)
    elementos.append(Spacer(1, 2 * mm))

    linea_azul = Table([[""]], colWidths=[ANCHO_CONTENIDO], rowHeights=[1])
    linea_azul.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1.6, COLOR_AZUL)]))
    elementos.append(linea_azul)
    elementos.append(Spacer(1, 5 * mm))

    # --- fecha, alineada a la derecha ---
    ciudad = ajustes.get("ciudad_negocio", "")
    texto_fecha = f"{ciudad}, {_fecha_larga(fecha)}" if ciudad else _fecha_larga(fecha)
    elementos.append(Paragraph(texto_fecha, estilo_derecha))
    elementos.append(Spacer(1, 8 * mm))

    # --- saludo e introduccion ---
    elementos.append(Paragraph(f"Estimado {contacto.get('nombre', '')},", estilo_normal))
    elementos.append(Spacer(1, 6 * mm))
    if ajustes.get("texto_intro"):
        texto_intro = ajustes["texto_intro"]
        if not es_proforma:
            texto_intro = texto_intro.replace("proforma", "cotizacion").replace("Proforma", "Cotizacion")
        elementos.append(Paragraph(texto_intro, estilo_normal))
    elementos.append(Spacer(1, 6 * mm))

    # --- tabla de precios: marca, descripcion, precio unidad, precio x4 ---
    encabezados = ["MARCA", "DESCRIPCION", "Precio Unidad", "Precio 4\nunidades"]
    filas = [encabezados]
    for it in items:
        descripcion = f"{it['modelo']} {it['medida']}".strip()
        filas.append(
            [
                it["marca"].upper(),
                descripcion,
                f"${it['precio_unitario']:,.2f}",
                f"${it['precio_unitario'] * 4:,.2f}",
            ]
        )

    tabla_items = Table(
        filas,
        colWidths=[30 * mm, 78 * mm, 35 * mm, 35 * mm],
        repeatRows=1,
    )
    estilo_tabla = [
        ("GRID", (0, 0), (-1, -1), 0.75, COLOR_BORDE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (0, 0), "CENTER"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (2, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for fila_idx in range(1, len(filas)):
        if fila_idx % 2 == 0:
            estilo_tabla.append(("BACKGROUND", (0, fila_idx), (-1, fila_idx), COLOR_FILA_PAR))
    tabla_items.setStyle(TableStyle(estilo_tabla))
    elementos.append(tabla_items)
    elementos.append(Spacer(1, 4 * mm))

    # --- resumen: subtotal, IVA y total (solo en la proforma; la cotizacion
    # es unicamente el listado informativo, sin montos finales) ---
    if es_proforma:
        porcentaje_iva = round((iva / subtotal) * 100, 1) if subtotal else round(TASA_IVA_DEFECTO * 100, 1)
        tabla_totales = Table(
            [
                ["", "Subtotal", f"${subtotal:,.2f}"],
                ["", f"IVA ({porcentaje_iva:g}%)", f"${iva:,.2f}"],
                ["", "TOTAL", f"${total:,.2f}"],
            ],
            colWidths=[108 * mm, 35 * mm, 35 * mm],
        )
        tabla_totales.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (1, 0), (-1, 1), "Helvetica"),
                    ("FONTNAME", (1, 2), (-1, 2), "Helvetica-Bold"),
                    ("FONTSIZE", (1, 0), (-1, -1), 10.5),
                    ("FONTSIZE", (1, 2), (-1, 2), 12),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("TEXTCOLOR", (2, 2), (2, 2), colors.HexColor("#128c4a")),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LINEABOVE", (1, 2), (-1, 2), 1, COLOR_OSCURO),
                    ("TOPPADDING", (1, 2), (-1, 2), 6),
                ]
            )
        )
        elementos.append(tabla_totales)
        elementos.append(Spacer(1, 8 * mm))

    # --- notas: formas de pago ---
    elementos.append(Paragraph("<b>Notas Importantes:</b>", estilo_normal))
    elementos.append(Spacer(1, 3 * mm))
    elementos.append(Paragraph("Formas de pago:", estilo_normal))
    for linea in (ajustes.get("formas_pago") or "").splitlines():
        linea = linea.strip()
        if linea:
            elementos.append(Paragraph(f"&nbsp;&nbsp;&nbsp;-&nbsp;&nbsp;{linea}", estilo_normal))
    elementos.append(Spacer(1, 4 * mm))

    # --- condiciones (IVA, envio, garantia, etc.): solo en la proforma, ya
    # que la cotizacion es unicamente informativa y todavia no habla de IVA ---
    if es_proforma:
        for parrafo in (ajustes.get("pie_mensaje") or "").split("\n\n"):
            parrafo = parrafo.strip()
            if parrafo:
                elementos.append(Paragraph(parrafo, estilo_normal))
                elementos.append(Spacer(1, 4 * mm))

    elementos.append(Spacer(1, 16 * mm))
    elementos.append(Paragraph("Atentamente.", estilo_normal))
    elementos.append(Spacer(1, 16 * mm))

    # --- firma del vendedor ---
    linea_firma = Table([[""]], colWidths=[60 * mm], rowHeights=[1])
    linea_firma.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.75, COLOR_OSCURO)]))
    elementos.append(linea_firma)
    elementos.append(Spacer(1, 2 * mm))
    for linea in [
        ajustes.get("vendedor_nombre"),
        ajustes.get("vendedor_cargo"),
        ajustes.get("nombre_negocio"),
        ajustes.get("vendedor_telefono") or ajustes.get("telefono_negocio"),
    ]:
        if linea:
            elementos.append(Paragraph(linea, estilo_normal))

    ruta_destino.parent.mkdir(parents=True, exist_ok=True)
    doc.build(elementos, onFirstPage=_pie_pagina, onLaterPages=_pie_pagina)
    return ruta_destino
