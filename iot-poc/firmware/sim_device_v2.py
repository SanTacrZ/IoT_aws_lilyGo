"""Simulador v2: placa generica LilyGo para /api/v2/readings (firmado HMAC).
Uso: DEVICE_API_KEY=... HMAC_SECRET=... python3 sim_device_v2.py http://localhost:8001 [device_id]
Envia un rango de sensores distinto por equipo para demostrar el registro autonomo.
"""
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"
DEVICE_ID = sys.argv[2] if len(sys.argv) > 2 else "lilygo-01"
API_KEY = os.getenv("DEVICE_API_KEY", "cambiar-api-key-larga")
HMAC_SECRET = os.getenv("HMAC_SECRET", "cambiar-hmac-largo")
INTERVAL = int(os.getenv("SIM_INTERVAL_S", "30"))

def post(body: dict) -> None:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{BASE}/api/v2/readings", data=raw, method="POST",
                                 headers={"Content-Type": "application/json", "X-Api-Key": API_KEY,
                                          "X-Timestamp": ts, "X-Signature": sig})
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.status, r.read().decode())

while True:
    sensors = [
        {"sensor_id": "temp1", "type": "temperature", "unit": "C", "value": round(20 + random.random() * 8, 2)},
        {"sensor_id": "hum1", "type": "humidity", "unit": "%", "value": round(45 + random.random() * 20, 2)},
        {"sensor_id": "soil1", "type": "soil", "unit": "%", "value": round(18 + random.random() * 35, 1)},
        {"sensor_id": "batt1", "type": "battery", "unit": "V", "value": round(3.4 + random.random() * 0.7, 2)},
    ]
    if DEVICE_ID == "lilygo-01":  # equipo completo: solar + segundo sensor de suelo
        sensors.append({"sensor_id": "solar1", "type": "solar", "unit": "W/m2",
                        "value": round(100 + random.random() * 700, 1)})
        sensors.append({"sensor_id": "soil2", "type": "soil", "unit": "%",
                        "value": round(25 + random.random() * 30, 1)})
    try:
        post({"device_id": DEVICE_ID, "name": f"Equipo {DEVICE_ID}", "fw": "v2-dev", "sensors": sensors})
    except Exception as e:
        print("fallo:", e)
    time.sleep(INTERVAL)
