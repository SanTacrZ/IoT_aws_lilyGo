"""Canales de alerta contra un servidor HTTP falso que imita Telegram, Twilio y
WhatsApp: se comprueba lo que de verdad saldria por la red, sin credenciales."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

import notificadores


class FalsoAPI(BaseHTTPRequestHandler):
    recibidos = []
    fallar = set()

    def do_POST(self):
        largo = int(self.headers.get("Content-Length", 0))
        cuerpo = self.rfile.read(largo).decode()
        tipo = self.headers.get("Content-Type", "")
        datos = json.loads(cuerpo) if "json" in tipo else {k: v[0] for k, v in parse_qs(cuerpo).items()}
        FalsoAPI.recibidos.append({"ruta": self.path, "auth": self.headers.get("Authorization"), "datos": datos})
        codigo = 500 if any(f in self.path for f in FalsoAPI.fallar) else 200
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": codigo == 200, "result": {}}).encode())

    def log_message(self, *_):
        pass


@pytest.fixture
def api(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FalsoAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    FalsoAPI.recibidos, FalsoAPI.fallar = [], set()
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_IDS": "111,222", "TELEGRAM_API_BASE": base,
        "WHATSAPP_TOKEN": "wa-token", "WHATSAPP_PHONE_ID": "999", "WHATSAPP_TO": "573001112233",
        "WHATSAPP_API_BASE": base,
        "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tw", "TWILIO_FROM": "+15550001",
        "SMS_TO": "+573004445566", "TWILIO_API_BASE": base,
    }.items():
        monkeypatch.setenv(k, v)
    yield FalsoAPI
    srv.shutdown()


def bitacora():
    filas = []
    return filas, lambda *f: filas.append(f)


def test_aviso_va_a_telegram_y_whatsapp(api):
    filas, registrar = bitacora()
    d = notificadores.Despachador(registrar)
    assert d.activos() == {"aviso": ["telegram", "whatsapp"], "escalado": ["sms"]}
    assert d.enviar(1, "texto plano", "<b>html</b>") == 3

    tg = [r for r in api.recibidos if "/bot123:abc/sendMessage" in r["ruta"]]
    assert {r["datos"]["chat_id"] for r in tg} == {"111", "222"}
    assert tg[0]["datos"]["parse_mode"] == "HTML" and tg[0]["datos"]["text"] == "<b>html</b>"
    wa = [r for r in api.recibidos if r["ruta"] == "/999/messages"][0]
    assert wa["auth"] == "Bearer wa-token" and wa["datos"]["text"]["body"] == "texto plano"
    # La bitacora no guarda destinos completos.
    assert {f[2] for f in filas} == {"111", "222", "…2233"}


def test_escalado_va_por_sms(api):
    filas, registrar = bitacora()
    notificadores.Despachador(registrar).enviar(7, "FINCA CRITICO", "x", nivel="escalado")
    sms = api.recibidos[0]
    assert sms["ruta"] == "/2010-04-01/Accounts/AC1/Messages.json"
    assert sms["auth"] == "Basic " + base64.b64encode(b"AC1:tw").decode()
    assert sms["datos"] == {"From": "+15550001", "To": "+573004445566", "Body": "FINCA CRITICO"}


def test_un_canal_caido_no_tumba_a_los_demas(api):
    api.fallar.add("/messages")                     # WhatsApp responde 500
    filas, registrar = bitacora()
    assert notificadores.Despachador(registrar).enviar(1, "t", "t") == 2
    fallidos = [f for f in filas if not f[4]]
    assert len(fallidos) == 1 and fallidos[0][1] == "whatsapp"


def test_sin_canales_queda_en_registro(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "WHATSAPP_TOKEN", "TWILIO_ACCOUNT_SID"):
        monkeypatch.delenv(k, raising=False)
    filas, registrar = bitacora()
    assert notificadores.Despachador(registrar).enviar(3, "hola", "hola") == 1
    assert filas == [(3, "registro:aviso", "", "hola", True, None)]


def test_error_de_telegram_no_filtra_el_token(api):
    api.fallar.add("sendMessage")
    with pytest.raises(RuntimeError) as e:
        notificadores.Telegram().llamar("sendMessage", chat_id="1", text="x")
    assert "123:abc" not in str(e.value)
