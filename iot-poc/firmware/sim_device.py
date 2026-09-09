"""Simulador LilyGo (Moisture + HyT + RadiacionSolar) con envio SEGURO.
No envia plano: firma HMAC-SHA256 + API-Key + timestamp (anti-replay). Usar solo sobre HTTPS en prod.
Uso: DEVICE_API_KEY=... HMAC_SECRET=... python3 sim_device.py https://TU-HOST/api/v1/readings
"""
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://44.251.195.44:8000/api/v1/readings"
API_KEY = os.getenv("DEVICE_API_KEY", "cambiar-api-key-larga")
HMAC_SECRET = os.getenv("HMAC_SECRET", "cambiar-hmac-largo")

payload = {"device_id": "lilygo-01", "temperature_c": round(22 + random.random() * 6, 2),
           "humidity_pct": round(45 + random.random() * 20, 2),
           "soil_moisture_pct": round(30 + random.random() * 40, 2),
           "solar_w_m2": round(200 + random.random() * 600, 1), "battery_v": 4.02}
body = json.dumps(payload).encode()
ts = str(int(time.time()))
sig = hmac.new(HMAC_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
req = urllib.request.Request(URL, data=body, method="POST",
                             headers={"Content-Type": "application/json", "X-Api-Key": API_KEY,
                                      "X-Timestamp": ts, "X-Signature": sig})
with urllib.request.urlopen(req, timeout=10) as r:
    print(r.status, r.read().decode())
