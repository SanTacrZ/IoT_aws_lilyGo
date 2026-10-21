"""Formato de los mensajes de alerta."""
from datetime import datetime, timedelta, timezone

import mensajes

AHORA = datetime(2026, 10, 21, 21, 40, tzinfo=timezone.utc)
ALERTA = {"id": 12, "severidad": "critico", "titulo": "Suelo seco", "entidad": "zona-1",
          "detalle": "Humedad del suelo 22% (umbral 30%).", "accion": "Regar la zona hoy.",
          "abierta_en": AHORA - timedelta(minutes=80)}


def test_nueva_alerta_en_hora_local():
    txt, html = mensajes.nueva(ALERTA, AHORA)
    assert txt.splitlines()[0] == "🚨 CRÍTICO · Suelo seco"
    assert "21/10 16:40" in txt                    # UTC-5 (America/Bogota)
    assert "/ack 12" in txt and "<b>CRÍTICO · Suelo seco</b>" in html


def test_html_escapa_datos():
    """Un diagnostico con etiquetas no puede romper/inyectar el HTML de Telegram."""
    a = dict(ALERTA, detalle="<a href='x'>clic</a>")
    _, html = mensajes.nueva(a, AHORA)
    assert "<a href" not in html and "&lt;a href" in html


def test_resuelta_con_duracion():
    txt, _ = mensajes.resuelta(ALERTA, AHORA)
    assert "✅ Resuelta · Suelo seco" in txt and "Duró 1 h 20 min" in txt


def test_sms_corto_y_sin_emojis():
    txt, _ = mensajes.escalado(ALERTA, AHORA)
    assert txt.startswith("FINCA CRÍTICO: Suelo seco en zona-1.")
    assert len(txt) <= 160 * 3
    assert "🚨" not in txt
