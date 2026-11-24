"""Envio y lectura de mensajes por la API oficial de WhatsApp Business
(Meta Cloud API) - distinto de utils/whatsapp.py, que solo arma links
"wa.me" para que un vendedor abra el chat a mano. Este modulo manda
mensajes de verdad, sin que nadie tenga que abrir WhatsApp."""
import json
import re
import urllib.error
import urllib.request

GRAPH_API_VERSION = "v21.0"


def normalizar_numero(numero: str, prefijo_defecto: str = "593") -> str:
    """WhatsApp Cloud API exige el numero en formato E.164 sin '+' ni
    espacios (ej. 593991234567). Si el numero viene local (empieza en 0,
    como se guardan en el CRM), le cambia el 0 inicial por el prefijo del
    pais configurado en Ajustes."""
    limpio = re.sub(r"[^0-9]", "", numero or "")
    if limpio.startswith("00"):
        limpio = limpio[2:]
    if limpio.startswith("0"):
        limpio = prefijo_defecto + limpio[1:]
    return limpio


def enviar_mensaje_texto(numero_destino: str, texto: str, phone_number_id: str, access_token: str) -> str:
    """Manda un mensaje de texto simple. Devuelve el wa_message_id de Meta
    (para no reprocesar el "echo" del mismo mensaje si el webhook lo reenvia)."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    cuerpo = json.dumps({
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto},
    }).encode("utf-8")
    peticion = urllib.request.Request(
        url, data=cuerpo, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(peticion, timeout=15) as resp:
        datos = json.loads(resp.read().decode("utf-8"))
    return (datos.get("messages") or [{}])[0].get("id", "")
