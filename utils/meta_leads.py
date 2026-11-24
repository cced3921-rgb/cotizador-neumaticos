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

GRAPH_API_VERSION = "v21.0"


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


def extraer_nombre_telefono(datos_lead: dict):
    """El formulario de Meta puede traer distintos nombres de campo segun
    como se armo (full_name vs first_name+last_name, phone_number vs
    phone) - aqui se normaliza a lo que necesita nuestro CRM. Si el
    formulario uso preguntas personalizadas (no los campos estandar de
    Meta), se busca por palabras clave en el nombre de la pregunta."""
    campos = {}
    for campo in datos_lead.get("field_data", []):
        valores = campo.get("values") or []
        campos[campo.get("name", "")] = valores[0] if valores else ""

    nombre = (
        campos.get("full_name")
        or f"{campos.get('first_name', '')} {campos.get('last_name', '')}".strip()
        or ""
    )
    telefono = campos.get("phone_number") or campos.get("phone") or ""

    if not nombre or not telefono:
        for clave, valor in campos.items():
            clave_normalizada = _sin_acentos(clave)
            if not telefono and any(
                palabra in clave_normalizada
                for palabra in ("telefono", "phone", "celular", "whatsapp")
            ):
                telefono = valor
            if not nombre and any(
                palabra in clave_normalizada for palabra in ("nombre", "name")
            ):
                nombre = valor

    return (nombre or "Lead de Facebook").strip(), telefono.strip()
