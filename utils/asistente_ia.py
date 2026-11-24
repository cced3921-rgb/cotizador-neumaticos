"""Asistente de IA que atiende a los clientes por WhatsApp: lee cada
mensaje, hace las preguntas necesarias para preparar al cliente para la
venta (que vehiculo, que medida de llanta, con que urgencia) y le pone una
prioridad para que el vendedor sepa a quien atender primero. Usa la API de
Anthropic (Claude) con "tool use" para que la respuesta venga siempre en un
formato fijo (mensaje + prioridad), en vez de tener que adivinarlo de un
texto libre."""
import anthropic

MODELO = "claude-sonnet-5-5"

PRIORIDADES_VALIDAS = {"caliente", "tibio", "frio"}

HERRAMIENTA_RESPUESTA = {
    "name": "responder_cliente",
    "description": (
        "Envia la respuesta para el cliente de WhatsApp y clasifica que tan "
        "listo esta para comprar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mensaje": {
                "type": "string",
                "description": (
                    "El mensaje de WhatsApp para el cliente. Corto, calido, "
                    "en espanol de Ecuador, como lo escribiria una persona "
                    "(no una lista ni un parrafo largo). Maximo 2-3 oraciones."
                ),
            },
            "prioridad": {
                "type": "string",
                "enum": ["caliente", "tibio", "frio"],
                "description": (
                    "caliente: ya dijo que vehiculo/medida necesita y quiere "
                    "comprar pronto. tibio: dio algo de info pero falta "
                    "concretar (medida, cuando, cuantas llantas). frio: "
                    "recien empieza, solo saludo o pregunta generica."
                ),
            },
            "listo_para_vendedor": {
                "type": "boolean",
                "description": (
                    "true solo cuando ya se sabe que necesita (vehiculo o "
                    "medida de llanta) y esta listo para que un vendedor "
                    "humano le confirme precio y cierre la venta - en ese "
                    "caso el asistente se pausa solo y deja de contestar."
                ),
            },
            "resumen_interno": {
                "type": "string",
                "description": (
                    "1 linea para el vendedor: que necesita el cliente hasta "
                    "ahora (ej. 'Busca 2 llantas 195/65 R15 para Corolla, "
                    "quiere comprar esta semana'). Vacio si todavia no hay "
                    "nada util que resumir."
                ),
            },
        },
        "required": ["mensaje", "prioridad", "listo_para_vendedor", "resumen_interno"],
    },
}


def _instrucciones_sistema(ajustes: dict, contacto: dict) -> str:
    negocio = ajustes.get("nombre_negocio") or "la distribuidora"
    ciudad = ajustes.get("ciudad_negocio") or ""
    notas = (contacto.get("notas") or "").strip()
    contexto_notas = f"\nDatos que ya dejo en el formulario:\n{notas}" if notas else ""
    return (
        f"Eres el asistente de ventas por WhatsApp de {negocio}, una distribuidora "
        f"de neumaticos{' en ' + ciudad if ciudad else ''} (Ecuador). Hablas con "
        f"{contacto.get('nombre') or 'un cliente'} que escribio por WhatsApp.\n\n"
        "Tu trabajo es preparar al cliente para la venta, no cerrarla tu: "
        "pregunta (una cosa a la vez, nunca varias preguntas juntas) lo que "
        "haga falta para saber que necesita - el vehiculo (marca/modelo/ano) "
        "o la medida exacta de la llanta, cuantas llantas necesita, y si "
        "tiene apuro. No inventes precios, stock ni plazos de entrega - eso "
        "lo confirma un vendedor humano. Cuando ya tengas lo esencial "
        "(vehiculo o medida), dilo de forma natural y avisa que un asesor le "
        "va a escribir para confirmar precio y disponibilidad - ahi marca "
        "listo_para_vendedor en true.\n\n"
        "Escribe como una persona real por WhatsApp: corto, calido, sin "
        f"emojis en exceso, en espanol de Ecuador.{contexto_notas}"
    )


def generar_respuesta(ajustes: dict, contacto: dict, historial: list[dict]) -> dict:
    """historial: lista de {'autor': 'cliente'|'ia'|'vendedor', 'texto': str},
    en orden cronologico, terminando en el mensaje nuevo del cliente.
    Devuelve {'mensaje', 'prioridad', 'listo_para_vendedor', 'resumen_interno'}."""
    cliente = anthropic.Anthropic(api_key=ajustes["anthropic_api_key"])

    mensajes = []
    for item in historial:
        rol = "assistant" if item["autor"] in ("ia", "vendedor") else "user"
        # Anthropic no permite dos mensajes seguidos del mismo rol - si el
        # anterior era del mismo rol, se juntan en un solo mensaje.
        if mensajes and mensajes[-1]["role"] == rol:
            mensajes[-1]["content"] += "\n" + item["texto"]
        else:
            mensajes.append({"role": rol, "content": item["texto"]})

    respuesta = cliente.messages.create(
        model=MODELO,
        max_tokens=500,
        system=_instrucciones_sistema(ajustes, contacto),
        tools=[HERRAMIENTA_RESPUESTA],
        tool_choice={"type": "tool", "name": "responder_cliente"},
        messages=mensajes,
    )

    for bloque in respuesta.content:
        if bloque.type == "tool_use" and bloque.name == "responder_cliente":
            datos = bloque.input
            if datos.get("prioridad") not in PRIORIDADES_VALIDAS:
                datos["prioridad"] = "tibio"
            return datos

    # No deberia pasar (forzamos tool_choice), pero por si acaso no se
    # rompe la conversacion - se contesta algo neutro y se marca tibio.
    return {
        "mensaje": "Gracias por escribirnos, en un momento te ayudamos.",
        "prioridad": "tibio",
        "listo_para_vendedor": False,
        "resumen_interno": "",
    }
