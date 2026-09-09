"""Backend v2 escalable: registro autonomo de equipos/sensores + metricas genericas.

Convive con app.py (v1). Tablas nuevas: devices_v2, sensors_v2, measurements_v2.
Ejecutar con: gunicorn app_v2:app  o  python app_v2.py
Reusa las mismas env vars de auth: DEVICE_API_KEY, HMAC_SECRET, MAX_SKEW_S, DB_*.
"""
import hashlib
import hmac as hmac_lib
import json
import logging
import os
import time
from datetime import datetime, timezone

import psycopg2
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iot-v2")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_BODY_BYTES", "8192"))

DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "")
HMAC_SECRET = os.getenv("HMAC_SECRET", "")
MAX_SKEW_S = int(os.getenv("MAX_SKEW_S", "300"))
ONLINE_AFTER_S = int(os.getenv("ONLINE_AFTER_S", "180"))
STALE_AFTER_S = int(os.getenv("STALE_AFTER_S_V2", "900"))
RATE_PER_MIN = int(os.getenv("RATE_PER_MIN", "12"))

DDL = """
CREATE TABLE IF NOT EXISTS devices_v2 (
  device_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  fw TEXT NOT NULL DEFAULT '',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  last_seen TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sensors_v2 (
  device_id TEXT NOT NULL REFERENCES devices_v2(device_id) ON DELETE CASCADE,
  sensor_id TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT '',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  last_value DOUBLE PRECISION,
  last_seen TIMESTAMPTZ,
  PRIMARY KEY (device_id, sensor_id)
);
CREATE TABLE IF NOT EXISTS measurements_v2 (
  id BIGSERIAL PRIMARY KEY,
  device_id TEXT NOT NULL,
  sensor_id TEXT NOT NULL,
  value DOUBLE PRECISION NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meas_dev_sens_ts ON measurements_v2 (device_id, sensor_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_sens_dev ON sensors_v2 (device_id);
"""

_db = None

def db():
    global _db
    if _db is None or _db.closed:
        _db = psycopg2.connect(host=os.getenv("DB_HOST", "db"),
                               port=int(os.getenv("DB_PORT", "5432")),
                               dbname=os.getenv("DB_NAME", "iot"),
                               user=os.getenv("DB_USER", "iot"),
                               password=os.getenv("DB_PASSWORD", "iot"),
                               connect_timeout=5)
        _db.autocommit = True
    return _db

def init_db():
    with db().cursor() as cur:
        cur.execute(DDL)
    log.info("v2 DB init ok")

_rate: dict[str, list[float]] = {}

def check_rate(device: str) -> bool:
    now = time.time()
    hits = [t for t in _rate.get(device, []) if now - t < 60]
    hits.append(now)
    _rate[device] = hits[-RATE_PER_MIN:]
    return len(hits) <= RATE_PER_MIN

def verify_auth(payload: bytes):
    if not DEVICE_API_KEY or not HMAC_SECRET:
        return False, "server auth no configurado"
    if request.headers.get("X-Api-Key") != DEVICE_API_KEY:
        return False, "api-key invalida"
    ts, sig = request.headers.get("X-Timestamp", ""), request.headers.get("X-Signature", "")
    try:
        if abs(time.time() - int(ts)) > MAX_SKEW_S:
            return False, "timestamp expirado (replay?)"
    except ValueError:
        return False, "timestamp invalido"
    expect = hmac_lib.new(HMAC_SECRET.encode(), ts.encode() + b"." + payload,
                          hashlib.sha256).hexdigest()
    if not hmac_lib.compare_digest(expect, sig):
        return False, "firma HMAC invalida"
    return True, "ok"

def check_key():
    return request.headers.get("X-Api-Key") == DEVICE_API_KEY and bool(DEVICE_API_KEY)

def status_of(last_seen) -> str:
    if not last_seen:
        return "offline"
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
    if age <= ONLINE_AFTER_S:
        return "online"
    if age <= STALE_AFTER_S:
        return "stale"
    return "offline"

@app.get("/health")
def health():
    try:
        with db().cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ok", db="up", api="v2")
    except Exception as e:
        return jsonify(status="degraded", db=str(e)), 503

@app.post("/api/v2/readings")
def ingest():
    raw = request.get_data()
    ok, msg = verify_auth(raw)
    if not ok:
        return jsonify(error=msg), 401
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return jsonify(error="JSON invalido"), 400
    dev = data.get("device_id", "")
    sensors = data.get("sensors", [])
    if not isinstance(dev, str) or not 1 <= len(dev) <= 64:
        return jsonify(error="device_id invalido"), 422
    if not isinstance(sensors, list) or not 1 <= len(sensors) <= 32:
        return jsonify(error="sensors debe ser lista de 1..32"), 422
    if not check_rate(dev):
        return jsonify(error="rate limit excedido"), 429
    now = datetime.now(timezone.utc)
    with db().cursor() as cur:
        cur.execute("INSERT INTO devices_v2 (device_id, name, fw, enabled, last_seen) "
                    "VALUES (%s,%s,%s,TRUE,%s) ON CONFLICT (device_id) DO UPDATE SET "
                    "name=EXCLUDED.name, fw=EXCLUDED.fw, enabled=TRUE, last_seen=EXCLUDED.last_seen",
                    (dev, str(data.get("name", dev))[:64], str(data.get("fw", ""))[:32], now))
        for s in sensors:
            try:
                sid, val = str(s["sensor_id"])[:64], float(s["value"])
            except (KeyError, TypeError, ValueError):
                return jsonify(error=f"sensor invalido: {s}"), 422
            cur.execute("INSERT INTO sensors_v2 (device_id, sensor_id, type, unit, enabled, last_value, last_seen) "
                        "VALUES (%s,%s,%s,%s,TRUE,%s,%s) ON CONFLICT (device_id, sensor_id) DO UPDATE SET "
                        "type=EXCLUDED.type, unit=EXCLUDED.unit, enabled=TRUE, last_value=EXCLUDED.last_value, last_seen=EXCLUDED.last_seen",
                        (dev, sid, str(s.get("type", ""))[:32], str(s.get("unit", ""))[:16], val, now))
            cur.execute("INSERT INTO measurements_v2 (device_id, sensor_id, value) VALUES (%s,%s,%s)",
                        (dev, sid, val))
    return jsonify(status="stored", device_id=dev, count=len(sensors), ts=now.isoformat()), 201

@app.get("/api/v2/state")
def state():
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    out = []
    with db().cursor() as cur:
        cur.execute("SELECT device_id, name, fw, enabled, last_seen FROM devices_v2 ORDER BY device_id")
        devs = cur.fetchall()
        for device_id, name, fw, enabled, last_seen in devs:
            cur.execute("SELECT sensor_id, type, unit, enabled, last_value, last_seen FROM sensors_v2 "
                        "WHERE device_id=%s ORDER BY sensor_id", (device_id,))
            sens = [{"sensor_id": r[0], "type": r[1], "unit": r[2], "enabled": r[3],
                     "last_value": r[4], "last_seen": r[5].isoformat() if r[5] else None}
                    for r in cur.fetchall()]
            out.append({"device_id": device_id, "name": name, "fw": fw, "enabled": enabled,
                        "last_seen": last_seen.isoformat() if last_seen else None,
                        "status": status_of(last_seen) if enabled else "disabled",
                        "sensors": [s for s in sens if s["enabled"]]})
    return jsonify(devices=out)

@app.get("/api/v2/history")
def history():
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    dev, sid = request.args.get("device_id", ""), request.args.get("sensor_id", "")
    limit = min(int(request.args.get("limit", 100)), 1000)
    if not dev or not sid:
        return jsonify(error="device_id y sensor_id requeridos"), 422
    with db().cursor() as cur:
        cur.execute("SELECT value, ts FROM measurements_v2 WHERE device_id=%s AND sensor_id=%s "
                    "ORDER BY ts DESC LIMIT %s", (dev, sid, limit))
        return jsonify([{"value": r[0], "ts": r[1].isoformat()} for r in cur.fetchall()])

@app.delete("/api/v2/devices/<dev>/sensors/<sid>")
def disable_sensor(dev, sid):
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    with db().cursor() as cur:
        cur.execute("UPDATE sensors_v2 SET enabled=FALSE WHERE device_id=%s AND sensor_id=%s", (dev, sid))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(status="disabled", device_id=dev, sensor_id=sid)

@app.delete("/api/v2/devices/<dev>")
def disable_device(dev):
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    with db().cursor() as cur:
        cur.execute("UPDATE devices_v2 SET enabled=FALSE WHERE device_id=%s", (dev,))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(status="disabled", device_id=dev)

@app.get("/api/v2/commands/pending")
def pending_commands():
    """El firmware consulta sus ordenes (riego) tras cada POST de datos."""
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    dev = request.args.get("device_id", "")
    if not dev:
        return jsonify(error="device_id requerido"), 422
    out = []
    with db().cursor() as cur:
        # expirar ordenes viejas (nunca regar con orden antigua)
        cur.execute("UPDATE commands SET status='expired' WHERE status='pending' "
                    "AND created_at < now() - interval '30 minutes'")
        cur.execute("""SELECT c.cmd_id, a.label, c.action, c.payload
                       FROM commands c JOIN actuators a USING (actuator_id)
                       WHERE a.device_id = %s AND c.status = 'pending'
                       ORDER BY c.cmd_id""", (dev,))
        for cmd_id, label, action, payload in cur.fetchall():
            out.append({"cmd_id": cmd_id, "actuator": label, "action": action, "payload": payload})
            cur.execute("UPDATE commands SET status='delivered', delivered_at=now() WHERE cmd_id=%s", (cmd_id,))
    return jsonify(out)

@app.post("/api/v2/commands/<int:cmd_id>/done")
def command_done(cmd_id: int):
    """El firmware reporta el resultado de ejecutar un comando."""
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    try:
        data = json.loads(request.get_data() or b"{}")
    except json.JSONDecodeError:
        return jsonify(error="JSON invalido"), 400
    with db().cursor() as cur:
        cur.execute("UPDATE commands SET status=%s, done_at=now(), result=%s WHERE cmd_id=%s",
                    ("done" if data.get("result", "ok") == "ok" else "failed",
                     str(data.get("result", "ok")), cmd_id))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(status="ok", cmd_id=cmd_id)

@app.get("/api/v2/devices/<dev>/sensors/<sid>/health")
def sensor_health(dev, sid):
    """Diagnostico: edad de la ultima muestra del sensor."""
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    with db().cursor() as cur:
        cur.execute("SELECT last_value, last_seen, enabled FROM sensors_v2 WHERE device_id=%s AND sensor_id=%s", (dev, sid))
        r = cur.fetchone()
        if not r:
            return jsonify(error="not found"), 404
        return jsonify(sensor_id=sid, last_value=r[0],
                       last_seen=r[1].isoformat() if r[1] else None,
                       enabled=r[2], status=status_of(r[1]))

@app.get("/dashboard-v2")
def dashboard_v2():
    return """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>IoT - Dashboard v2</title>
<style>body{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;background:#0f172a;color:#e2e8f0}
.dev{background:#1e293b;border-radius:.75rem;padding:1rem;margin:1rem 0}.cards{display:flex;gap:.6rem;flex-wrap:wrap}
.card{flex:1;min-width:140px;background:#334155;border-radius:.6rem;padding:.8rem;text-align:center}
.card span{font-size:.75rem;color:#94a3b8}.card b{font-size:1.4rem;display:block}
.online{color:#4ade80}.stale{color:#facc15}.offline{color:#f87171}.disabled{color:#94a3b8}
button{background:#475569;color:#fff;border:0;border-radius:.4rem;padding:.3rem .6rem;cursor:pointer;font-size:.75rem}
small{color:#94a3b8}</style></head><body>
<h1>🌱 IoT Autónomo v2 <small>(poll 5s · API-Key en prompt)</small></h1>
<p><small>Quitar sensor/equipo = baja lógica. Si vuelve a enviar datos, se re-habilita solo.</small></p>
<div id="root">Cargando…</div>
<script>
const KEY = sessionStorage.KEY || (sessionStorage.KEY = prompt("API-Key del dashboard:") || "");
async function api(p, o={}) {
  const r = await fetch(p, {...o, headers: {...(o.headers||{}), "X-Api-Key": KEY}});
  if (!r.ok) throw new Error(r.status + " " + await r.text());
  return r.json();
}
async function refresh() {
  try {
    const {devices} = await api("/api/v2/state");
    document.getElementById("root").innerHTML = devices.length ? devices.map(d => `
      <div class="dev"><h3>${d.name || d.device_id} <span class="${d.status}">● ${d.status}</span>
      <small>${d.device_id} · ${d.fw||""} · ${d.last_seen||"sin datos"}</small></h3>
      <button onclick="rmDev('${d.device_id}')">Quitar equipo</button>
      <div class="cards">${d.sensors.map(s => `
        <div class="card"><span>${s.type||s.sensor_id} (${s.unit||""})<br>${s.sensor_id}</span>
        <b>${s.last_value ?? "--"}</b><br>
        <button onclick="rmSens('${d.device_id}','${s.sensor_id}')">Quitar</button></div>`).join("") || "<small>Sin sensores habilitados</small>"}</div></div>`).join("")
      : "Sin equipos todavía — enciende una placa.";
  } catch(e) { document.getElementById("root").innerHTML = "Error: " + e.message; }
}
async function rmSens(d, s){ if(confirm(`Quitar ${s} de ${d}?`)){ await api(`/api/v2/devices/${d}/sensors/${s}`, {method:"DELETE"}); refresh(); } }
async function rmDev(d){ if(confirm(`Quitar equipo ${d}?`)){ await api(`/api/v2/devices/${d}`, {method:"DELETE"}); refresh(); } }
refresh(); setInterval(refresh, 5000);
</script></body></html>"""

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8000)
else:
    try:
        init_db()
    except Exception as e:
        log.warning("init_db diferido: %s", e)
