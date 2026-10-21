"""Prueba de punta a punta contra el stack corriendo (docker compose).

Nodo "prueba-e2e" (credencial propia en DEVICE_CREDENTIALS) envia una lectura
firmada con suelo seco -> iot-agent -> TimescaleDB -> alert-agent abre la
alerta, la escala, el bot la silencia con /ack y se resuelve al regar.

Aislada: el loop de fondo ignora "prueba-*" (ALERT_IGNORAR) y aqui el ciclo
corre con solo={"prueba-e2e"}, asi que las alertas reales no se tocan.
Si hay Telegram configurado, los mensajes de la prueba llegan al celular.

    pytest -m e2e -s tests/test_e2e.py
"""
import hashlib
import hmac
import json
import subprocess
import time

import pytest
import requests

from conftest import RAIZ

pytestmark = pytest.mark.e2e
NODO = "prueba-e2e"
URL = "http://localhost:5000"


def _env():
    return dict(linea.split("=", 1) for linea in (RAIZ / ".env").read_text().splitlines()
                if "=" in linea and not linea.lstrip().startswith("#"))


def _credencial():
    for item in _env().get("DEVICE_CREDENTIALS", "").split(","):
        d, k, s = (item.split(":") + ["", "", ""])[:3]
        if d == NODO:
            return k, s
    pytest.skip("sin credencial prueba-e2e: python3 scripts/nuevo_nodo.py prueba-e2e")


def sql(consulta):
    return subprocess.run(["docker", "exec", "db-postgres", "psql", "-U", "iot", "-d", "iot", "-tAc", consulta],
                          check=True, capture_output=True, text=True).stdout.strip()


def en_alertas(codigo):
    """Ejecuta Python dentro del contenedor alert-agent; devuelve el JSON de la ultima linea."""
    r = subprocess.run(["docker", "exec", "-i", "alert-agent", "python", "-"], input=codigo,
                       check=True, capture_output=True, text=True)
    return json.loads(r.stdout.strip().splitlines()[-1])


def ciclo(minutos_adelante=0):
    return en_alertas(f"""
import json, motor, notificadores, reglas
from datetime import datetime, timedelta, timezone
ahora = datetime.now(timezone.utc) + timedelta(minutes={minutos_adelante})
d = notificadores.Despachador(motor.registrar_notificacion)
print(json.dumps(motor.ciclo(d, reglas.Umbrales(ignorar=()), ahora, solo={{"{NODO}"}})))
""")


def bot(comando):
    return en_alertas(f"import bot, json; print(json.dumps(bot.responder({comando!r}, 'e2e', 'telegram:e2e')))")


def enviar(suelo, lectura_id, dispositivo=NODO, firmar_como=None):
    clave, secreto = _credencial()
    body = json.dumps({"temperatura_aire": 24.3, "humedad_aire": 68.0, "luz_lux": 140.0, "luz_pct": 70,
                       "humedad_suelo_pct": suelo, "humedad_suelo_raw": 3000, "alerta_seco": suelo < 30,
                       "latitud": 0, "longitud": 0, "altitud": 0, "satelites": 0, "uptime_s": 3600,
                       "wifi_rssi": -61, "heap_libre": 180000, "arranques": 3, "pendientes": 0,
                       "bateria_v": 3.92}).encode()
    recurso = f"/api/plantas?dispositivo={dispositivo}&lectura_id={lectura_id}"
    ts = str(int(time.time()))
    firmado = f"/api/plantas?dispositivo={firmar_como or dispositivo}&lectura_id={lectura_id}"
    sig = hmac.new(secreto.encode(), f"{ts}.POST.{firmado}.".encode() + body, hashlib.sha256).hexdigest()
    return requests.post(URL + recurso, data=body, timeout=10, headers={
        "Content-Type": "application/json", "X-Api-Key": clave, "X-Timestamp": ts,
        "X-Firma-Version": "2", "X-Signature": sig})


def limpiar():
    sql(f"DELETE FROM alertas WHERE entidad = '{NODO}'")
    sql(f"DELETE FROM lecturas WHERE dispositivo = '{NODO}'")
    requests.delete(f"http://localhost:1026/v2/entities/planta:{NODO}", timeout=5)


@pytest.fixture(scope="module", autouse=True)
def stack():
    try:
        requests.get(URL + "/api/v1/health", timeout=3)
    except requests.RequestException:
        pytest.skip("stack no levantado (docker compose up -d)")
    limpiar()
    yield
    limpiar()


def test_flujo_completo_de_alerta():
    t0 = int(time.time())

    # 1. Seguridad del ingreso
    assert enviar(20, t0, firmar_como="otro-nodo").status_code == 401        # query alterada
    assert enviar(20, t0, dispositivo="lilygo-tbeam-01").status_code == 403  # clave de otro nodo
    r = enviar(20, t0)
    assert r.status_code == 200 and r.json()["insertada"], r.text
    assert enviar(20, t0).json()["insertada"] is False                     # reintento: no duplica
    assert sql(f"SELECT count(*) FROM lecturas WHERE dispositivo='{NODO}'") == "1"

    # 2. Se abre la alerta y se notifica
    assert f"suelo_seco:{NODO}" in ciclo()["abiertas"]
    alerta_id = int(sql(f"SELECT id FROM alertas WHERE clave='suelo_seco:{NODO}' AND resuelta_en IS NULL"))
    assert ciclo() == {"abiertas": [], "recordadas": [], "escaladas": [], "resueltas": []}  # sin spam

    # 3. Critica y sin reconocer 11 min despues -> escala (SMS)
    assert f"suelo_seco:{NODO}" in ciclo(11)["escaladas"]

    # 4. Bot: /estado y /alertas la muestran; /ack la silencia
    estado = bot("/estado")
    print("\n--- /estado ---\n" + estado)
    # Los nodos de prueba (ALERT_IGNORAR) no salen en /estado, pero su alerta si en /alertas.
    assert "Estado de la finca" in estado and NODO not in estado
    alertas = bot("/alertas")
    print("--- /alertas ---\n" + alertas)
    assert f"#A{alerta_id}" in alertas and "Humedad del suelo 20%" in alertas
    assert f"#A{alerta_id}" in bot(f"/ack {alerta_id}")
    assert ciclo(75)["recordadas"] == []                                  # silenciada: sin recordatorio

    # 5. Se riega -> la alerta se cierra sola
    assert enviar(62, t0 + 1).status_code == 200
    assert f"suelo_seco:{NODO}" in ciclo()["resueltas"]

    print("--- notificaciones ---\n" + sql(
        f"SELECT canal || ' | ' || replace(texto, E'\\n', ' / ') FROM notificaciones "
        f"WHERE alerta_id={alerta_id} ORDER BY id"))
    # nueva -> escalado -> resuelta. Sin canales configurados quedan como
    # "registro:<nivel>"; con Telegram/SMS, una fila por canal y destino.
    canales = sql(f"SELECT string_agg(DISTINCT canal, ',') FROM notificaciones WHERE alerta_id={alerta_id}")
    textos = sql(f"SELECT string_agg(texto, ' || ' ORDER BY id) FROM notificaciones WHERE alerta_id={alerta_id}")
    assert "registro:escalado" in canales or "sms" in canales
    assert textos.index("CRÍTICO · Suelo seco") < textos.index("FINCA CRÍTICO") < textos.index("Resuelta")
