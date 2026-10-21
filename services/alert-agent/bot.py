"""Bot de Telegram por long polling: no abre ningun puerto ni necesita webhook
publico, la conexion siempre sale desde el servidor.

Solo atiende a los chats de TELEGRAM_CHAT_IDS. A cualquier otro le contesta su
chat_id (para que el dueno pueda autorizarlo) y no le muestra ningun dato.
"""
import logging
import re
import time
from datetime import datetime, timezone
from fnmatch import fnmatch
from html import escape

import motor
from mensajes import NIVEL, duracion, hora
from notificadores import Telegram

logger = logging.getLogger("alertas.bot")

AYUDA = ("<b>Bot de la finca</b>\n"
         "/estado — nodos, sensores y último dictamen IA\n"
         "/alertas — alertas abiertas\n"
         "/ack &lt;id&gt; — silenciar una alerta (sigue cerrándose sola)\n"
         "/ack — silenciar todas\n"
         "/ayuda — este mensaje")


def _fmt(valor, unidad, decimales=0):
    return "—" if valor is None else f"{float(valor):.{decimales}f}{unidad}"


def texto_estado(nodos, ia, ahora):
    lineas = ["<b>🌿 Estado de la finca</b>"]
    u = motor.Umbrales.desde_env()
    nodos = [n for n in nodos if not any(fnmatch(n["dispositivo"], p) for p in u.ignorar)]
    if not nodos:
        lineas.append("Sin nodos registrados.")
    for n in nodos:
        p = n["payload"]
        vivo = n["segundos_sin_datos"] < u.nodo_caido_min * 60
        lineas.append(
            f"{'🟢' if vivo else '🔴'} <code>{escape(n['dispositivo'])}</code> · "
            f"hace {duracion(n['segundos_sin_datos'])}\n"
            f"   💧 suelo {_fmt(p.get('humedad_suelo_pct'), '%')} · 🌡 {_fmt(p.get('temperatura_aire'), '°C', 1)}"
            f" · 💨 {_fmt(p.get('humedad_aire'), '%')}"
            + (f" · 🔋 {_fmt(p.get('bateria_v'), ' V', 2)}" if p.get("bateria_v") is not None else ""))
    if ia:
        creado, estado, diag, modelo = ia
        lineas.append(f"\n🤖 IA ({escape(modelo or '')}, {hora(creado)}): <b>{escape(estado)}</b>\n"
                      f"{escape(diag or '')}")
    abiertas = motor.abiertas_ordenadas()
    lineas.append(f"\n🔔 Alertas abiertas: <b>{len(abiertas)}</b>" + (" — /alertas" if abiertas else ""))
    return "\n".join(lineas)


def texto_alertas(abiertas, ahora):
    if not abiertas:
        return "✅ No hay alertas abiertas."
    lineas = ["<b>🔔 Alertas abiertas</b>"]
    for a in abiertas:
        silenciada = " · 🔕" if a["reconocida_en"] else ""
        lineas.append(f"#A{a['id']} {NIVEL[a['severidad']]} · {escape(a['titulo'])} · "
                      f"<code>{escape(a['entidad'])}</code> · {duracion((ahora - a['abierta_en']).total_seconds())}"
                      f"{silenciada}\n   {escape(a['detalle'])}")
    return "\n".join(lineas)


def responder(texto, chat_id, quien):
    """Devuelve el HTML de respuesta a un comando de un chat ya autorizado."""
    ahora = datetime.now(timezone.utc)
    m = re.match(r"^/(\w+)(?:@\w+)?(?:\s+#?A?(\d+))?", texto.strip(), re.I)
    if not m:
        return AYUDA
    cmd, arg = m.group(1).lower(), m.group(2)
    if cmd == "estado":
        return texto_estado(*motor.resumen_finca(), ahora)
    if cmd == "alertas":
        return texto_alertas(motor.abiertas_ordenadas(), ahora)
    if cmd == "ack":
        ids = motor.reconocer(int(arg) if arg else None, quien)
        if not ids:
            return "No había alertas pendientes de reconocer" + (f" con id {arg}." if arg else ".")
        return "🔕 Silenciadas: " + ", ".join(f"#A{i}" for i in ids) + ". Te aviso cuando se resuelvan."
    return AYUDA


def bucle(detener):
    """Corre hasta que detener() sea True. Reintenta con espera si Telegram falla."""
    tg = Telegram()
    if not tg.token:
        logger.info("Bot de Telegram desactivado (sin TELEGRAM_BOT_TOKEN)")
        return
    autorizados = set(tg.chats)
    offset = None
    logger.info("Bot de Telegram escuchando (%d chats autorizados)", len(autorizados))
    while not detener():
        try:
            updates = tg.llamar("getUpdates", timeout=25, offset=offset, allowed_updates=["message"])
        except Exception as e:
            logger.warning("getUpdates fallo: %s", e)
            time.sleep(10)
            continue
        for u in updates or []:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            texto = msg.get("text") or ""
            if not chat_id or not texto.startswith("/"):
                continue
            if chat_id not in autorizados:
                logger.warning("Comando de chat no autorizado %s", chat_id)
                respuesta = (f"Este bot es privado. Tu chat_id es <code>{escape(chat_id)}</code>: "
                             "agrégalo a TELEGRAM_CHAT_IDS en el .env del servidor.")
            else:
                quien = (msg.get("from") or {}).get("username") or chat_id
                try:
                    respuesta = responder(texto, chat_id, f"telegram:{quien}")
                except Exception:
                    logger.exception("Fallo atendiendo %r", texto)
                    respuesta = "Error interno consultando la finca; revisa los logs de alert-agent."
            try:
                tg.llamar("sendMessage", chat_id=chat_id, text=respuesta, parse_mode="HTML")
            except Exception as e:
                logger.error("No se pudo responder: %s", e)
