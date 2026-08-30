"""Arma el texto corto (caption) y el link de WhatsApp (wa.me) listo para
abrir el chat con el mensaje ya escrito. El detalle de la cotizacion va en
el PDF adjunto, no en el texto del chat."""
import urllib.parse


class _SinFaltantes(dict):
    """Permite formatear plantillas viejas que aun tengan {detalle} sin
    romper, aunque ya no pasemos ese dato."""

    def __missing__(self, clave):
        return ""


def normalizar_telefono(telefono: str, prefijo: str = "593") -> str:
    """Convierte numeros ecuatorianos en distintos formatos al formato
    internacional que espera wa.me (solo digitos, con codigo de pais)."""
    limpio = "".join(c for c in telefono if c.isdigit())
    if not limpio:
        return limpio
    if limpio.startswith("00"):
        limpio = limpio[2:]
    if limpio.startswith(prefijo):
        return limpio
    if limpio.startswith("0"):
        return prefijo + limpio[1:]
    return prefijo + limpio


def construir_mensaje_corto(nombre_contacto: str, total: float, plantilla: str) -> str:
    """Texto que abre el chat de WhatsApp junto con el PDF de la cotizacion.
    Variables disponibles en la plantilla: {nombre} y {total}."""
    valores = _SinFaltantes(nombre=nombre_contacto or "", total=f"{total:,.2f}")
    return plantilla.format_map(valores)


def construir_link_wa(telefono: str, mensaje: str = "", prefijo: str = "593") -> str:
    """Link de WhatsApp para abrir el chat de un contacto. Si no se pasa
    mensaje, abre el chat vacio (util para escribirle desde el pipeline)."""
    numero = normalizar_telefono(telefono, prefijo)
    if not mensaje:
        return f"https://wa.me/{numero}"
    texto = urllib.parse.quote(mensaje)
    return f"https://wa.me/{numero}?text={texto}"
