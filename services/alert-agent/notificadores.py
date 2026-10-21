"""Canales de salida: Telegram, WhatsApp (Cloud API de Meta) y SMS (Twilio).

Cada canal se activa solo si sus variables estan en .env. Si no hay ninguno,
queda el canal "registro": los mensajes se guardan en la tabla notificaciones
y en el log, sin salir a internet (util para probar sin credenciales).

Las URL base se pueden cambiar por variable de entorno para apuntar a un
servidor falso en las pruebas.
"""
import logging
import os

import requests

logger = logging.getLogger("alertas.canales")


def _lista(nombre):
    return [x.strip() for x in os.getenv(nombre, "").split(",") if x.strip()]


def enmascarar(destino):
    """En la bitacora no se guardan telefonos/chat ids completos."""
    destino = str(destino)
    return ("…" + destino[-4:]) if len(destino) > 4 else destino


class Canal:
    nombre = "base"

    def configurado(self):
        raise NotImplementedError

    def destinos(self):
        raise NotImplementedError

    def enviar_a(self, destino, texto, html_texto):
        raise NotImplementedError


class Telegram(Canal):
    nombre = "telegram"

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chats = _lista("TELEGRAM_CHAT_IDS")
        self.base = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

    def configurado(self):
        return bool(self.token and self.chats)

    def destinos(self):
        return self.chats

    def llamar(self, metodo, **datos):
        r = requests.post(f"{self.base}/bot{self.token}/{metodo}", json=datos, timeout=35)
        cuerpo = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not cuerpo.get("ok"):
            # Nunca se loguea la URL: lleva el token.
            raise RuntimeError(f"Telegram {metodo} HTTP {r.status_code}: {cuerpo.get('description', '')[:120]}")
        return cuerpo.get("result")

    def enviar_a(self, destino, texto, html_texto):
        self.llamar("sendMessage", chat_id=destino, text=html_texto, parse_mode="HTML",
                    disable_web_page_preview=True)


class WhatsApp(Canal):
    """Cloud API de Meta. Ojo: fuera de la ventana de 24 h desde el ultimo
    mensaje del usuario, Meta solo permite plantillas aprobadas; un texto libre
    falla con error 131047. Para avisos fuera de esa ventana, usar plantilla."""
    nombre = "whatsapp"

    def __init__(self):
        self.token = os.getenv("WHATSAPP_TOKEN", "")
        self.phone_id = os.getenv("WHATSAPP_PHONE_ID", "")
        self.numeros = _lista("WHATSAPP_TO")
        self.base = os.getenv("WHATSAPP_API_BASE", "https://graph.facebook.com/v21.0").rstrip("/")

    def configurado(self):
        return bool(self.token and self.phone_id and self.numeros)

    def destinos(self):
        return self.numeros

    def enviar_a(self, destino, texto, html_texto):
        r = requests.post(
            f"{self.base}/{self.phone_id}/messages",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"messaging_product": "whatsapp", "to": destino, "type": "text",
                  "text": {"body": texto[:4000]}},
            timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"WhatsApp HTTP {r.status_code}: {r.text[:160]}")


class SmsTwilio(Canal):
    nombre = "sms"

    def __init__(self):
        self.sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.origen = os.getenv("TWILIO_FROM", "")
        self.numeros = _lista("SMS_TO")
        self.base = os.getenv("TWILIO_API_BASE", "https://api.twilio.com").rstrip("/")

    def configurado(self):
        return bool(self.sid and self.token and self.origen and self.numeros)

    def destinos(self):
        return self.numeros

    def enviar_a(self, destino, texto, html_texto):
        r = requests.post(
            f"{self.base}/2010-04-01/Accounts/{self.sid}/Messages.json",
            auth=(self.sid, self.token),
            data={"From": self.origen, "To": destino, "Body": texto[:480]},
            timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Twilio HTTP {r.status_code}: {r.text[:160]}")


CANALES = {c.nombre: c for c in (Telegram, WhatsApp, SmsTwilio)}


class Despachador:
    """Envia un mensaje por los canales de un nivel y deja todo en la bitacora.

    aviso:    cada alerta nueva, recordatorio y resolucion (por defecto Telegram + WhatsApp)
    escalado: alerta critica sin reconocer (por defecto SMS)
    """

    def __init__(self, registrar):
        self.registrar = registrar          # f(alerta_id, canal, destino, texto, ok, error)
        self.niveles = {
            "aviso": self._crear(_lista("ALERT_CANALES_AVISO") or ["telegram", "whatsapp"]),
            "escalado": self._crear(_lista("ALERT_CANALES_ESCALADO") or ["sms"]),
        }

    @staticmethod
    def _crear(nombres):
        canales = []
        for n in nombres:
            if n in CANALES:
                c = CANALES[n]()
                if c.configurado():
                    canales.append(c)
        return canales

    def activos(self):
        return {nivel: [c.nombre for c in cs] for nivel, cs in self.niveles.items()}

    def enviar(self, alerta_id, texto, html_texto, nivel="aviso"):
        """Devuelve cuantos envios salieron bien. Sin canales -> canal 'registro'."""
        canales = self.niveles.get(nivel, [])
        if not canales:
            logger.info("[registro/%s] %s", nivel, texto.replace("\n", " | "))
            self.registrar(alerta_id, f"registro:{nivel}", "", texto, True, None)
            return 1
        enviados = 0
        for canal in canales:
            for destino in canal.destinos():
                try:
                    canal.enviar_a(destino, texto, html_texto)
                    self.registrar(alerta_id, canal.nombre, enmascarar(destino), texto, True, None)
                    enviados += 1
                except Exception as e:      # un canal caido no tumba a los demas
                    logger.error("Fallo %s -> %s: %s", canal.nombre, enmascarar(destino), e)
                    self.registrar(alerta_id, canal.nombre, enmascarar(destino), texto, False, str(e)[:300])
        return enviados
