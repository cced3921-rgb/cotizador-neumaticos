"""Integracion con Meta Lead Ads (formularios de clientes potenciales de
Facebook e Instagram): recibe el webhook y trae los datos reales del lead
desde la Graph API - el webhook de Meta solo manda el ID del lead, no el
nombre ni el telefono, hay que pedirlos aparte."""
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GRAPH_API_VERSION = "v21.0"

# Ecuador no cambia de horario en el año (no tiene horario de verano), asi
# que un offset fijo es suficiente para convertir el "created_time" de Meta
# (siempre en UTC) a la hora local real en que la persona lleno el formulario.
ZONA_ECUADOR = timezone(timedelta(hours=-5))


def fecha_lead_a_local(created_time: str) -> str:
    """Convierte el 'created_time' que manda Meta (ej.
    '2026-11-24T05:04:56+0000') a 'YYYY-MM-DD HH:MM:SS' en hora de Ecuador,
    para que coincida con el resto de fechas del sistema. Si no se puede
    leer, devuelve cadena vacia (quien llama usa la hora actual como respaldo)."""
    if not created_time:
        return ""
    try:
        momento = datetime.strptime(created_time, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return ""
    return momento.astimezone(ZONA_ECUADOR).strftime("%Y-%m-%d %H:%M:%S")


def firma_valida(cuerpo: bytes, firma_recibida: str, app_secret: str) -> bool:
    """Confirma que el webhook realmente viene de Meta (y no de cualquiera
    que le haya adivinado la URL): Meta firma cada envio con el App Secret,
    nosotros repetimos la misma firma y comparamos."""
    if not app_secret or not firma_recibida:
        return False
    esperada = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), cuerpo, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(firma_recibida, esperada)


def obtener_datos_lead(leadgen_id: str, page_access_token: str) -> dict:
    """Pide a la Graph API los campos del formulario que lleno la persona."""
    query = urllib.parse.urlencode({"access_token": page_access_token})
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{leadgen_id}?{query}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sin_acentos(texto: str) -> str:
    equivalencias = str.maketrans("áéíóúñ", "aeioun")
    return texto.lower().translate(equivalencias)


# Campos del formulario que no aportan nada util al vendedor (metadatos
# internos de Meta, no respuestas de la persona) - se descartan al armar las
# notas con "lo demas que respondio".
_CAMPOS_IGNORADOS_EN_NOTAS = {"inbox_url", "platform", "partner_name", "vehicle_make", "is_organic"}


def extraer_datos_contacto(datos_lead: dict):
    """El formulario de Meta puede traer distintos nombres de campo segun
    como se armo (full_name vs first_name+last_name, phone_number vs
    phone) - aqui se normaliza a lo que necesita nuestro CRM. Si el
    formulario uso preguntas personalizadas (no los campos estandar de
    Meta), se busca por palabras clave en el nombre de la pregunta.
    Cualquier otra pregunta que haya respondido (ej. "que rin busca",
    "medidas") se guarda aparte como notas, para que el vendedor no tenga
    que volver a preguntarselo. Devuelve (nombre, telefono, email, notas)."""
    campos = {}
    for campo in datos_lead.get("field_data", []):
        valores = campo.get("values") or []
        campos[campo.get("name", "")] = valores[0] if valores else ""

    claves_usadas = set()
    nombre = campos.get("full_name")
    if nombre:
        claves_usadas.add("full_name")
    elif campos.get("first_name") or campos.get("last_name"):
        nombre = f"{campos.get('first_name', '')} {campos.get('last_name', '')}".strip()
        claves_usadas.update({"first_name", "last_name"})
    else:
        nombre = ""

    if campos.get("phone_number"):
        telefono, clave_telefono = campos["phone_number"], "phone_number"
    elif campos.get("phone"):
        telefono, clave_telefono = campos["phone"], "phone"
    else:
        telefono, clave_telefono = "", None
    if clave_telefono:
        claves_usadas.add(clave_telefono)

    email = campos.get("email") or ""
    if email:
        claves_usadas.add("email")

    if not nombre or not telefono or not email:
        for clave, valor in campos.items():
            if clave in claves_usadas:
                continue
            clave_normalizada = _sin_acentos(clave)
            if not telefono and any(
                palabra in clave_normalizada
                for palabra in ("telefono", "phone", "celular", "whatsapp")
            ):
                telefono, clave_telefono = valor, clave
                claves_usadas.add(clave)
            elif not nombre and any(
                palabra in clave_normalizada for palabra in ("nombre", "name")
            ):
                nombre = valor
                claves_usadas.add(clave)
            elif not email and any(
                palabra in clave_normalizada for palabra in ("correo", "email", "mail")
            ):
                email = valor
                claves_usadas.add(clave)

    lineas_notas = []
    for clave, valor in campos.items():
        if clave in claves_usadas or clave in _CAMPOS_IGNORADOS_EN_NOTAS or not valor:
            continue
        etiqueta = clave.replace("_", " ").strip().capitalize()
        lineas_notas.append(f"{etiqueta}: {valor}")
    notas = "\n".join(lineas_notas)

    return (nombre or "Lead de Facebook").strip(), telefono.strip(), email.strip(), notas
