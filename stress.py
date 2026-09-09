"""Mini prueba de estres GET/POST con solo libreria estandar.
Uso: python3 stress.py [URL] [TOTAL] [CONCURRENCIA]
"""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = sys.argv[1] if len(sys.argv) > 1 else "http://54.70.219.37/"
TOTAL = int(sys.argv[2]) if len(sys.argv) > 2 else 200
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 20


def do_get(_):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(URL, timeout=15) as r:
            r.read()
            return ("GET", r.status, time.perf_counter() - t0)
    except Exception as e:
        return ("GET", f"ERROR {e}", time.perf_counter() - t0)


def do_post(i):
    body = json.dumps({"sensor": "stress", "n": i, "temperatura": 25.5}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
            return ("POST", r.status, time.perf_counter() - t0)
    except Exception as e:
        return ("POST", f"ERROR {e}", time.perf_counter() - t0)


def run(name, fn):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(fn, range(TOTAL)))
    dt = time.perf_counter() - t0
    ok = sum(1 for _, s, _ in results if s in (200, 201))
    lat = sorted(t for _, s, t in results if s in (200, 201))
    print(f"--- {name}: {TOTAL} req x {WORKERS} hilos en {dt:.1f}s ---")
    print(f"Exitosas: {ok}/{TOTAL} | {TOTAL / dt:.1f} req/s")
    if lat:
        print(f"Latencia ms: min={lat[0]*1000:.0f} p50={lat[len(lat)//2]*1000:.0f} "
              f"p95={lat[int(len(lat)*0.95)]*1000:.0f} max={lat[-1]*1000:.0f}")
    bad = {}
    for _, s, _ in results:
        if s not in (200, 201):
            bad[str(s)] = bad.get(str(s), 0) + 1
    if bad:
        print("Fallos:", bad)


run("GET", do_get)
run("POST", do_post)
