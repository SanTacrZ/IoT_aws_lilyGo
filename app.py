"""Servidor Flask s2: recibe GET/POST en / y los muestra en /dashboard."""
import html
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone

from flask import Flask, request

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "requests.jsonl")
MAX_KEEP = 200  # ultimos N registros en memoria

lock = threading.Lock()
records = deque(maxlen=MAX_KEEP)
stats = {"GET": 0, "POST": 0}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_request(method, data):
    entry = {"ts": now_iso(), "method": method,
             "ip": request.remote_addr, "data": data}
    with lock:
        records.append(entry)
        stats[method] = stats.get(method, 0) + 1
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


def load_history():
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                records.append(r)
                stats[r.get("method", "GET")] = stats.get(r.get("method", "GET"), 0) + 1
    except FileNotFoundError:
        pass


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        data = request.get_json(silent=True)
        if data is None:
            data = request.form.to_dict() or {"raw": request.data.decode("utf-8", "replace")}
        log_request("POST", data)
        return f"POST recibido: {data}", 201
    log_request("GET", dict(request.args))
    return "GET recibido. Usa POST para enviar datos. Mira /dashboard"


@app.route("/dashboard")
def dashboard():
    with lock:
        rows = list(reversed(records))
        n_get, n_post = stats.get("GET", 0), stats.get("POST", 0)
    trs = []
    for r in rows:
        trs.append(
            "<tr><td>{ts}</td><td>{m}</td><td>{ip}</td><td>{d}</td></tr>".format(
                ts=html.escape(str(r.get("ts", ""))),
                m=html.escape(str(r.get("method", ""))),
                ip=html.escape(str(r.get("ip", ""))),
                d=html.escape(json.dumps(r.get("data", ""), ensure_ascii=False)),
            )
        )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>s2 - Monitor IoT</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;background:#0f172a;color:#e2e8f0}}
.cards{{display:flex;gap:1rem;margin-bottom:1.5rem}}
.card{{flex:1;background:#1e293b;border-radius:.75rem;padding:1rem;text-align:center}}
.card b{{font-size:2rem;display:block}}
table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{text-align:left;padding:.5rem;border-bottom:1px solid #334155;vertical-align:top}}
td:last-child{{word-break:break-all}}
.GET{{color:#4ade80}}.POST{{color:#60a5fa}}
small{{color:#94a3b8}}
</style></head><body>
<h1>&#128268; s2 - Monitor IoT <small>(auto-actualiza cada 5s)</small></h1>
<div class="cards">
<div class="card"><b>{n_get + n_post}</b>Total peticiones</div>
<div class="card"><b class="GET">{n_get}</b>GET</div>
<div class="card"><b class="POST">{n_post}</b>POST</div>
</div>
<table><tr><th>Fecha/hora</th><th>Metodo</th><th>IP origen</th><th>Datos</th></tr>
{''.join(trs) or '<tr><td colspan="4">Sin peticiones todavia. Haz un GET o POST a /</td></tr>'}
</table></body></html>"""


load_history()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
