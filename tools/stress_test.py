"""Prueba de estres para AgroSense API (stdlib, sin dependencias).
Modos:
  reads  -> GET /api/v2/state + /history (con X-Api-Key)
  writes -> POST /api/v2/readings firmado HMAC (N dispositivos para eludir rate-limit)
  dash   -> GET /dashboard-v2 (HTML, sin auth)
  mixed  -> reads+writes 70/30
Uso:
  python3 tools/stress_test.py http://localhost:8001 reads 2000 50
  python3 tools/stress_test.py http://localhost:8001 writes 300 30
"""
import hashlib
import hmac
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8001"
MODE = sys.argv[2] if len(sys.argv) > 2 else "reads"
TOTAL = int(sys.argv[3]) if len(sys.argv) > 3 else 500
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 30

API_KEY = os.getenv("DEVICE_API_KEY", "demo-key-cambiar")
HMAC_SECRET = os.getenv("HMAC_SECRET", "demo-secret-cambiar")
N_DEVICES = max(1, TOTAL // 10)  # 1 device cada 10 writes -> bajo rate-limit (12/min)

lat = []
codes: dict[int, int] = {}


def req(url, data=None, headers=None):
    t0 = time.perf_counter()
    try:
        r = urllib.request.Request(url, data=data, headers=headers or {})
        with urllib.request.urlopen(r, timeout=30) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = 0
    dt = time.perf_counter() - t0
    lat.append(dt)
    codes[code] = codes.get(code, 0) + 1
    return code


def signed_post(device_id: str):
    sensors = [{"sensor_id": "soil1", "type": "soil", "unit": "%",
                "value": round(20 + (device_id.__hash__() % 400) / 10, 1)},
               {"sensor_id": "temp1", "type": "temperature", "unit": "C", "value": 25.0}]
    body = json.dumps({"device_id": device_id, "name": f"stress-{device_id}", "fw": "stress",
                       "sensors": sensors}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-Api-Key": API_KEY,
                  "X-Timestamp": ts, "X-Signature": sig}


def task(i: int):
    if MODE == "reads" or (MODE == "mixed" and i % 10 < 7):
        h = {"X-Api-Key": API_KEY}
        if i % 2:
            return req(f"{BASE}/api/v2/state", headers=h)
        return req(f"{BASE}/api/v2/history?device_id=stress-{i % N_DEVICES}&sensor_id=soil1&limit=50", headers=h)
    if MODE == "dash":
        return req(f"{BASE}/dashboard-v2")
    dev = f"stress-{i % N_DEVICES}"
    body, h = signed_post(dev)
    return req(f"{BASE}/api/v2/readings", data=body, headers=h)


t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(task, range(TOTAL)))
dt = time.perf_counter() - t0

lat.sort()
p = lambda q: lat[min(int(len(lat) * q), len(lat) - 1)] * 1000
print(f"modo={MODE} total={TOTAL} conc={WORKERS} tiempo={dt:.1f}s rps={TOTAL/dt:.1f}")
print(f"latencia ms: p50={p(.5):.0f} p90={p(.9):.0f} p95={p(.95):.0f} p99={p(.99):.0f} max={lat[-1]*1000:.0f}")
print("codigos:", dict(sorted(codes.items())))
