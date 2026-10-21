"""Textos de las alertas. Cada mensaje sale en dos formas: texto plano (SMS,
WhatsApp, bitacora) y HTML (Telegram). Todo lo que viene de datos se escapa:
un diagnostico del agente IA no puede meter etiquetas en el mensaje."""
import os
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

ZONA = ZoneInfo(os.getenv("TZ_FINCA", "America/Bogota"))
ICONO = {"critico": "🚨", "atencion": "⚠️"}
NIVEL = {"critico": "CRÍTICO", "atencion": "ATENCIÓN"}


def hora(momento: datetime):
    return momento.astimezone(ZONA).strftime("%d/%m %H:%M")


def duracion(segundos):
    minutos = int(segundos // 60)
    if minutos < 60:
        return f"{minutos} min"
    horas, minutos = divmod(minutos, 60)
    return f"{horas} h {minutos} min" if horas < 48 else f"{horas // 24} días"


def _dos(lineas_txt, lineas_html):
    return "\n".join(lineas_txt), "\n".join(lineas_html)


def nueva(a, momento):
    """a: dict con id, severidad, titulo, entidad, detalle, accion."""
    icono, nivel = ICONO[a["severidad"]], NIVEL[a["severidad"]]
    txt = [f"{icono} {nivel} · {a['titulo']}", f"Zona/nodo: {a['entidad']}", a["detalle"]]
    htm = [f"{icono} <b>{nivel} · {escape(a['titulo'])}</b>",
           f"Zona/nodo: <code>{escape(a['entidad'])}</code>", escape(a["detalle"])]
    if a.get("accion"):
        txt.append(f"👉 {a['accion']}")
        htm.append(f"👉 {escape(a['accion'])}")
    txt.append(f"#A{a['id']} · {hora(momento)} · /ack {a['id']} para silenciar")
    htm.append(f"<i>#A{a['id']} · {hora(momento)}</i> · /ack {a['id']} para silenciar")
    return _dos(txt, htm)


def recordatorio(a, momento):
    abierta = duracion((momento - a["abierta_en"]).total_seconds())
    txt = [f"⏰ Sigue activa ({abierta}) · {a['titulo']}", f"Zona/nodo: {a['entidad']}", a["detalle"],
           f"#A{a['id']} · /ack {a['id']} para silenciar"]
    htm = [f"⏰ <b>Sigue activa ({abierta}) · {escape(a['titulo'])}</b>",
           f"Zona/nodo: <code>{escape(a['entidad'])}</code>", escape(a["detalle"]),
           f"<i>#A{a['id']}</i> · /ack {a['id']} para silenciar"]
    return _dos(txt, htm)


def escalado(a, momento):
    """SMS: corto, sin emojis (cuentan doble y algunos telefonos no los muestran)."""
    sin_ack = duracion((momento - a["abierta_en"]).total_seconds())
    txt = (f"FINCA {NIVEL[a['severidad']]}: {a['titulo']} en {a['entidad']}. {a['detalle']} "
           f"Sin reconocer hace {sin_ack}. #A{a['id']}")
    return txt, escape(txt)


def resuelta(a, momento):
    dura = duracion((momento - a["abierta_en"]).total_seconds())
    txt = [f"✅ Resuelta · {a['titulo']}", f"Zona/nodo: {a['entidad']}", f"Duró {dura}. #A{a['id']}"]
    htm = [f"✅ <b>Resuelta · {escape(a['titulo'])}</b>",
           f"Zona/nodo: <code>{escape(a['entidad'])}</code>", f"<i>Duró {dura}. #A{a['id']}</i>"]
    return _dos(txt, htm)
