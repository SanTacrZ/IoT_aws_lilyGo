"""Simulador de la placa de riego (lilygo_irrigation.ino) para pruebas e2e.
Reproduce: POST readings -> GET config -> poll commands -> ejecutar relé simulado
(escala de tiempo SIM_MIN_SCALE) -> POST done con litros. No simula offline (usar
POST /api/v2/irrigation/report manual para probar ese flujo).
Uso: python3 sim_actuator.py http://localhost:8001 [ciclos=1]
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001").rstrip("/")
CICLOS = int(sys.argv[2]) if len(sys.argv) > 2 else 1
API_KEY = os.getenv("DEVICE_API_KEY", "demo-key-cambiar")
HMAC_SECRET = os.getenv("HMAC_SECRET", "demo-secret-cambiar")
SCALE = float(os.getenv("SIM_MIN_SCALE", "0.05"))   # 1 min real = SCALE*60 s sim
DEV = "lilygo-01"

def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Api-Key": API_KEY}
    if data is not None:  # firmware firma todos sus POST (HMAC de timestamp.body)
        ts = str(int(time.time()))
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = hmac.new(HMAC_SECRET.encode(), ts.encode() + b"." + data,
                                          hashlib.sha256).hexdigest()
    req = urllib.request.Request(BASE + path, data=data,
                                 method="POST" if body is not None else "GET", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode() or "{}")

soil = 20.0  # arranca seco para disparar reglas
for c in range(CICLOS):
    soil = max(15, min(70, soil + (12 if c % 2 else -3)))  # oscila
    call("/api/v2/readings", {"device_id": DEV, "name": "Sim riego", "fw": "sim-v3",
                              "sensors": [{"sensor_id": "soil1", "type": "soil", "unit": "%", "value": soil},
                                          {"sensor_id": "batt1", "type": "battery", "unit": "V", "value": 4.1}]})
    print(f"ciclo {c}: soil={soil}")
    cfg = call(f"/api/v2/config?device_id={DEV}")
    print("  config:", cfg)
    cmds = call(f"/api/v2/commands/pending?device_id={DEV}")
    for cm in cmds:
        dur = cm["payload"].get("duration_min", 0) if cm["action"] == "on" else 0
        lit = round(dur * 0.8, 1)  # bomba simulada: 0.8 L/min
        print(f"  cmd {cm['cmd_id']} {cm['action']} {dur}min -> ejectuando...")
        if dur:
            time.sleep(dur * SCALE)  # relay simulado
        call(f"/api/v2/commands/{cm['cmd_id']}/done", {"result": "ok", "liters": lit})
        print(f"    done, litros={lit}")
    time.sleep(1)
print("sim_actuator fin")
