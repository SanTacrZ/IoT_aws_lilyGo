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
from datetime import datetime, timedelta, timezone

import psycopg2
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iot-v2")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_BODY_BYTES", "8192"))

DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "")
HMAC_SECRET = os.getenv("HMAC_SECRET", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
MAX_SKEW_S = int(os.getenv("MAX_SKEW_S", "300"))
DB_SECRET_ARN = os.getenv("DB_SECRET_ARN", "")  # AWS: secret {username,password,host,port,dbname}
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
  id BIGSERIAL,
  device_id TEXT NOT NULL,
  sensor_id TEXT NOT NULL,
  value DOUBLE PRECISION NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id, ts)
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

def check_key():
    return request.headers.get("X-Api-Key") == DEVICE_API_KEY and bool(DEVICE_API_KEY)

def admin_required():
    """Guard de gestion humana (fase JWT: reemplazar por auth_required(perm))."""
    if not ADMIN_KEY or request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify(error="admin-key invalida"), 401
    return None

def page_args():
    try:
        return min(max(int(request.args.get("limit", 50)), 1), 200), max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return 50, 0

def body_json():
    try:
        d = request.get_json(force=True)
        return d if isinstance(d, dict) else None
    except Exception:
        return None

# ---------------- CRUD: farms ----------------

@app.get("/api/v2/farms")
def farms_list():
    g = admin_required()
    if g: return g
    limit, off = page_args()
    with db().cursor() as cur:
        cur.execute("SELECT count(*) FROM farms")
        total = cur.fetchone()[0]
        cur.execute("SELECT farm_id, name, location, lat, lon, created_at FROM farms "
                    "ORDER BY farm_id LIMIT %s OFFSET %s", (limit, off))
        rows = [{"farm_id": r[0], "name": r[1], "location": r[2], "lat": r[3], "lon": r[4],
                 "created_at": r[5].isoformat()} for r in cur.fetchall()]
    resp = jsonify(rows)
    resp.headers["X-Total-Count"] = total
    return resp

@app.post("/api/v2/farms")
def farms_create():
    g = admin_required()
    if g: return g
    d = body_json() or {}
    if not d.get("name"):
        return jsonify(error="name requerido"), 422
    with db().cursor() as cur:
        cur.execute("INSERT INTO farms (name, location, lat, lon) VALUES (%s,%s,%s,%s) RETURNING farm_id",
                    (str(d["name"])[:100], d.get("location", ""), d.get("lat"), d.get("lon")))
        fid = cur.fetchone()[0]
    return jsonify(status="created", farm_id=fid), 201

@app.get("/api/v2/farms/<int:fid>")
def farms_get(fid):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("SELECT farm_id, name, location, lat, lon, created_at FROM farms WHERE farm_id=%s", (fid,))
        r = cur.fetchone()
        if not r: return jsonify(error="not found"), 404
    return jsonify(farm_id=r[0], name=r[1], location=r[2], lat=r[3], lon=r[4], created_at=r[5].isoformat())

@app.patch("/api/v2/farms/<int:fid>")
def farms_patch(fid):
    g = admin_required()
    if g: return g
    d = body_json() or {}
    sets = [(f, d[f]) for f in ("name", "location", "lat", "lon") if f in d]
    if not sets:
        return jsonify(error="nada para actualizar"), 422
    clause = ", ".join(f"{f}=%s" for f, _ in sets)
    with db().cursor() as cur:
        cur.execute(f"UPDATE farms SET {clause} WHERE farm_id=%s", (*[v for _, v in sets], fid))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="updated", farm_id=fid)

@app.delete("/api/v2/farms/<int:fid>")
def farms_delete(fid):
    g = admin_required()
    if g: return g
    force = request.args.get("force") == "1"
    with db().cursor() as cur:
        cur.execute("SELECT count(*) FROM zones WHERE farm_id=%s", (fid,))
        if cur.fetchone()[0] > 0 and not force:
            return jsonify(error="la parcela tiene zonas; use ?force=1 para eliminar todo"), 409
        cur.execute("DELETE FROM farms WHERE farm_id=%s", (fid,))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="deleted", farm_id=fid)

# ---------------- CRUD: zones ----------------

@app.get("/api/v2/zones")
def zones_list():
    if not (admin_required() is None or check_key()):
        return jsonify(error="auth invalida"), 401
    limit, off = page_args()
    q, args = "", []
    if request.args.get("farm_id"):
        q, args = "WHERE farm_id=%s", [request.args.get("farm_id")]
    with db().cursor() as cur:
        cur.execute(f"SELECT count(*) FROM zones {q}", args)
        total = cur.fetchone()[0]
        cur.execute(f"""SELECT zone_id, farm_id, name, crop, area_m2, soil_min_pct, soil_max_pct,
                        hysteresis_pct, max_irrigation_min, enabled FROM zones {q}
                        ORDER BY zone_id LIMIT %s OFFSET %s""", (*args, limit, off))
        rows = [dict(zip(["zone_id", "farm_id", "name", "crop", "area_m2", "soil_min_pct",
                          "soil_max_pct", "hysteresis_pct", "max_irrigation_min", "enabled"], r))
                for r in cur.fetchall()]
    resp = jsonify(rows)
    resp.headers["X-Total-Count"] = total
    return resp

@app.post("/api/v2/zones")
def zones_create():
    g = admin_required()
    if g: return g
    d = body_json() or {}
    if not d.get("farm_id") or not d.get("name"):
        return jsonify(error="farm_id y name requeridos"), 422
    try:
        with db().cursor() as cur:
            cur.execute("""INSERT INTO zones (farm_id, name, crop, area_m2, soil_min_pct, soil_max_pct,
                           hysteresis_pct, max_irrigation_min) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING zone_id""",
                        (d["farm_id"], str(d["name"])[:64], d.get("crop", ""), d.get("area_m2"),
                         d.get("soil_min_pct", 30), d.get("soil_max_pct", 60),
                         d.get("hysteresis_pct", 3), d.get("max_irrigation_min", 20)))
            zid = cur.fetchone()[0]
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify(error="farm_id no existe"), 422
    except psycopg2.errors.UniqueViolation:
        return jsonify(error="ya existe una zona con ese nombre en la parcela"), 409
    return jsonify(status="created", zone_id=zid), 201

@app.get("/api/v2/zones/<int:zid>")
def zones_get(zid):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("""SELECT zone_id, farm_id, name, crop, area_m2, soil_min_pct, soil_max_pct,
                       hysteresis_pct, max_irrigation_min, enabled FROM zones WHERE zone_id=%s""", (zid,))
        r = cur.fetchone()
        if not r: return jsonify(error="not found"), 404
    return jsonify(dict(zip(["zone_id", "farm_id", "name", "crop", "area_m2", "soil_min_pct",
                             "soil_max_pct", "hysteresis_pct", "max_irrigation_min", "enabled"], r)))

@app.patch("/api/v2/zones/<int:zid>")
def zones_patch(zid):
    g = admin_required()
    if g: return g
    d = body_json() or {}
    allowed = ("name", "crop", "area_m2", "soil_min_pct", "soil_max_pct",
               "hysteresis_pct", "max_irrigation_min", "enabled")
    sets = [(f, d[f]) for f in allowed if f in d]
    if not sets:
        return jsonify(error="nada para actualizar"), 422
    clause = ", ".join(f"{f}=%s" for f, _ in sets)
    with db().cursor() as cur:
        cur.execute(f"UPDATE zones SET {clause} WHERE zone_id=%s", (*[v for _, v in sets], zid))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="updated", zone_id=zid)

@app.delete("/api/v2/zones/<int:zid>")
def zones_delete(zid):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("UPDATE zones SET enabled=FALSE WHERE zone_id=%s", (zid,))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="disabled", zone_id=zid)

# ---------------- CRUD: devices / sensors ----------------

@app.get("/api/v2/devices")
def devices_list():
    g = admin_required()
    if g: return g
    limit, off = page_args()
    q, args = "", []
    if request.args.get("zone_id"):
        q, args = "WHERE d.zone_id=%s", [request.args.get("zone_id")]
    with db().cursor() as cur:
        cur.execute(f"SELECT count(*) FROM devices_v2 d {q}", args)
        total = cur.fetchone()[0]
        cur.execute(f"""SELECT d.device_id, d.name, d.fw, d.enabled, d.last_seen, z.name
                        FROM devices_v2 d LEFT JOIN zones z USING (zone_id) {q}
                        ORDER BY d.device_id LIMIT %s OFFSET %s""", (*args, limit, off))
        rows = [{"device_id": r[0], "name": r[1], "fw": r[2], "enabled": r[3],
                 "last_seen": r[4].isoformat() if r[4] else None,
                 "zone": r[5], "status": status_of(r[4]) if r[3] else "disabled"}
                for r in cur.fetchall()]
    resp = jsonify(rows)
    resp.headers["X-Total-Count"] = total
    return resp

@app.patch("/api/v2/devices/<dev>")
def devices_patch(dev):
    g = admin_required()
    if g: return g
    d = body_json() or {}
    sets = [(f, d[f]) for f in ("name", "fw", "zone_id", "enabled") if f in d]
    if not sets:
        return jsonify(error="nada para actualizar"), 422
    clause = ", ".join(f"{f}=%s" for f, _ in sets)
    try:
        with db().cursor() as cur:
            cur.execute(f"UPDATE devices_v2 SET {clause} WHERE device_id=%s", (*[v for _, v in sets], dev))
            if cur.rowcount == 0: return jsonify(error="not found"), 404
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify(error="zone_id no existe"), 422
    return jsonify(status="updated", device_id=dev)

@app.get("/api/v2/devices/<dev>/sensors")
def sensors_list(dev):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("""SELECT sensor_id, type, unit, enabled, last_value, last_seen
                       FROM sensors_v2 WHERE device_id=%s ORDER BY sensor_id""", (dev,))
        rows = [{"sensor_id": r[0], "type": r[1], "unit": r[2], "enabled": r[3],
                 "last_value": r[4], "last_seen": r[5].isoformat() if r[5] else None}
                for r in cur.fetchall()]
    return jsonify(rows)

# ---------------- CRUD: actuators ----------------

@app.get("/api/v2/actuators")
def actuators_list():
    g = admin_required()
    if g: return g
    q, args = "", []
    if request.args.get("device_id"):
        q, args = "WHERE device_id=%s", [request.args.get("device_id")]
    elif request.args.get("zone_id"):
        q, args = "WHERE zone_id=%s", [request.args.get("zone_id")]
    with db().cursor() as cur:
        cur.execute(f"""SELECT actuator_id, device_id, zone_id, kind, label, pin, state, enabled
                        FROM actuators {q} ORDER BY actuator_id""", args)
        rows = [dict(zip(["actuator_id", "device_id", "zone_id", "kind", "label", "pin",
                          "state", "enabled"], r)) for r in cur.fetchall()]
    return jsonify(rows)

@app.post("/api/v2/actuators")
def actuators_create():
    g = admin_required()
    if g: return g
    d = body_json() or {}
    if not d.get("device_id") or d.get("kind") not in ("pump", "valve", "fan", "other") or not d.get("label"):
        return jsonify(error="device_id, kind (pump|valve|fan|other) y label requeridos"), 422
    try:
        with db().cursor() as cur:
            cur.execute("INSERT INTO actuators (device_id, zone_id, kind, label, pin) VALUES (%s,%s,%s,%s,%s) RETURNING actuator_id",
                        (d["device_id"], d.get("zone_id"), d["kind"], str(d["label"])[:32], d.get("pin")))
            aid = cur.fetchone()[0]
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify(error="device_id o zone_id no existen"), 422
    except psycopg2.errors.UniqueViolation:
        return jsonify(error="ya existe ese label en el dispositivo"), 409
    return jsonify(status="created", actuator_id=aid), 201

@app.patch("/api/v2/actuators/<int:aid>")
def actuators_patch(aid):
    g = admin_required()
    if g: return g
    d = body_json() or {}
    sets = [(f, d[f]) for f in ("label", "pin", "zone_id", "enabled", "state") if f in d]
    if not sets:
        return jsonify(error="nada para actualizar"), 422
    clause = ", ".join(f"{f}=%s" for f, _ in sets)
    with db().cursor() as cur:
        cur.execute(f"UPDATE actuators SET {clause} WHERE actuator_id=%s", (*[v for _, v in sets], aid))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="updated", actuator_id=aid)

@app.delete("/api/v2/actuators/<int:aid>")
def actuators_delete(aid):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("UPDATE actuators SET enabled=FALSE WHERE actuator_id=%s", (aid,))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="disabled", actuator_id=aid)

# ---------------- CRUD: rules ----------------

@app.get("/api/v2/rules")
def rules_list():
    g = admin_required()
    if g: return g
    q, args = "", []
    if request.args.get("zone_id"):
        q, args = "WHERE zone_id=%s", [request.args.get("zone_id")]
    with db().cursor() as cur:
        cur.execute(f"""SELECT rule_id, zone_id, name, sensor_type, condition, threshold, action,
                        enabled, cooldown_min, last_fired FROM rules {q} ORDER BY rule_id""", args)
        rows = [{"rule_id": r[0], "zone_id": r[1], "name": r[2], "sensor_type": r[3],
                 "condition": r[4], "threshold": r[5], "action": r[6], "enabled": r[7],
                 "cooldown_min": r[8], "last_fired": r[9].isoformat() if r[9] else None}
                for r in cur.fetchall()]
    return jsonify(rows)

@app.post("/api/v2/rules")
def rules_create():
    g = admin_required()
    if g: return g
    d = body_json() or {}
    if not d.get("zone_id") or not d.get("name") or d.get("condition") not in ("below", "above", "outside") \
            or "threshold" not in d or d.get("action", "irrigate") not in ("irrigate", "alert", "irrigate+alert"):
        return jsonify(error="zone_id, name, condition (below|above|outside), threshold y "
                             "action (irrigate|alert|irrigate+alert) requeridos"), 422
    try:
        with db().cursor() as cur:
            cur.execute("""INSERT INTO rules (zone_id, name, sensor_type, condition, threshold, action, cooldown_min)
                           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING rule_id""",
                        (d["zone_id"], str(d["name"])[:64], d.get("sensor_type", "soil"),
                         d["condition"], float(d["threshold"]), d.get("action", "irrigate"),
                         d.get("cooldown_min", 30)))
            rid = cur.fetchone()[0]
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify(error="zone_id no existe"), 422
    return jsonify(status="created", rule_id=rid), 201

@app.patch("/api/v2/rules/<int:rid>")
def rules_patch(rid):
    g = admin_required()
    if g: return g
    d = body_json() or {}
    sets = [(f, d[f]) for f in ("name", "condition", "threshold", "action", "enabled", "cooldown_min") if f in d]
    if not sets:
        return jsonify(error="nada para actualizar"), 422
    clause = ", ".join(f"{f}=%s" for f, _ in sets)
    with db().cursor() as cur:
        cur.execute(f"UPDATE rules SET {clause} WHERE rule_id=%s", (*[v for _, v in sets], rid))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="updated", rule_id=rid)

@app.delete("/api/v2/rules/<int:rid>")
def rules_delete(rid):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("UPDATE rules SET enabled=FALSE WHERE rule_id=%s", (rid,))
        if cur.rowcount == 0: return jsonify(error="not found"), 404
    return jsonify(status="disabled", rule_id=rid)

# ---------------- Alerts / riego manual / eventos ----------------

@app.get("/api/v2/alerts")
def alerts_list():
    if not (admin_required() is None or check_key()):
        return jsonify(error="auth invalida"), 401
    limit, off = page_args()
    q, args = "WHERE acked_at IS NULL", []
    if request.args.get("open") == "0":
        q = ""
    with db().cursor() as cur:
        cur.execute(f"""SELECT alert_id, zone_id, device_id, severity, kind, message, created_at, acked_at
                        FROM alerts {q} ORDER BY created_at DESC LIMIT %s OFFSET %s""", (*args, limit, off))
        rows = [{"alert_id": r[0], "zone_id": r[1], "device_id": r[2], "severity": r[3],
                 "kind": r[4], "message": r[5], "created_at": r[6].isoformat(),
                 "acked_at": r[7].isoformat() if r[7] else None} for r in cur.fetchall()]
    return jsonify(rows)

@app.post("/api/v2/alerts/<int:aid>/ack")
def alerts_ack(aid):
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("UPDATE alerts SET acked_at=now() WHERE alert_id=%s AND acked_at IS NULL", (aid,))
        if cur.rowcount == 0: return jsonify(error="not found o ya acked"), 404
    return jsonify(status="acked", alert_id=aid)

@app.post("/api/v2/zones/<int:zid>/irrigate")
def zone_irrigate(zid):
    """Riego manual (boton del dashboard). {"duration_min": 10} o {"stop": true}."""
    g = admin_required()
    if g: return g
    d = body_json() or {}
    with db().cursor() as cur:
        cur.execute("SELECT actuator_id, label FROM actuators WHERE zone_id=%s AND kind='pump' AND enabled ORDER BY actuator_id LIMIT 1", (zid,))
        r = cur.fetchone()
        if not r:
            return jsonify(error="la zona no tiene bomba habilitada"), 409
        aid, label = r
        if d.get("stop"):
            cur.execute("INSERT INTO commands (actuator_id, action, payload) VALUES (%s,'off','{\"why\":\"manual\"}') RETURNING cmd_id", (aid,))
            cid = cur.fetchone()[0]
            cur.execute("UPDATE irrigation_events SET ended_at=now(), duration_min=EXTRACT(EPOCH FROM (now()-started_at))/60 "
                        "WHERE zone_id=%s AND ended_at IS NULL", (zid,))
            return jsonify(status="stop queued", cmd_id=cid, actuator=label)
        dur = min(int(d.get("duration_min", 10)), 120)
        if dur <= 0:
            return jsonify(error="duration_min debe ser > 0 (o stop=true)"), 422
        cur.execute("INSERT INTO commands (actuator_id, action, payload) VALUES (%s,'on',%s) RETURNING cmd_id",
                    (aid, json.dumps({"duration_min": dur, "why": "manual"})))
        cid = cur.fetchone()[0]
        cur.execute("INSERT INTO irrigation_events (zone_id, actuator_id, trigger, notes) VALUES (%s,%s,'manual',%s)",
                    (zid, aid, f"manual {dur} min (cmd {cid})"))
    return jsonify(status="irrigate queued", cmd_id=cid, actuator=label, duration_min=dur), 201

@app.get("/api/v2/irrigation-events")
def irrigation_events_list():
    if not (admin_required() is None or check_key()):
        return jsonify(error="auth invalida"), 401
    limit, off = page_args()
    q, args = "", []
    if request.args.get("zone_id"):
        q, args = "WHERE i.zone_id=%s", [request.args.get("zone_id")]
    with db().cursor() as cur:
        cur.execute(f"""SELECT i.event_id, i.zone_id, z.name, i.actuator_id, i.started_at, i.ended_at,
                        i.duration_min, i.liters, i.trigger FROM irrigation_events i
                        JOIN zones z USING (zone_id) {q}
                        ORDER BY i.started_at DESC LIMIT %s OFFSET %s""", (*args, limit, off))
        rows = [{"event_id": r[0], "zone_id": r[1], "zone": r[2], "actuator_id": r[3],
                 "started_at": r[4].isoformat(), "ended_at": r[5].isoformat() if r[5] else None,
                 "duration_min": r[6], "liters": r[7], "trigger": r[8]} for r in cur.fetchall()]
    return jsonify(rows)

# ---------------- users (fase JWT) ----------------

@app.get("/api/v2/users")
def users_list():
    g = admin_required()
    if g: return g
    with db().cursor() as cur:
        cur.execute("SELECT user_id, email, name, role, created_at FROM users ORDER BY user_id")
        rows = [{"user_id": r[0], "email": r[1], "name": r[2], "role": r[3],
                 "created_at": r[4].isoformat()} for r in cur.fetchall()]
    return jsonify(rows)

@app.post("/api/v2/users")
def users_create():
    g = admin_required()
    if g: return g
    d = body_json() or {}
    if not d.get("email") or d.get("role", "viewer") not in ("viewer", "farmer", "admin"):
        return jsonify(error="email y role (viewer|farmer|admin) requeridos"), 422
    try:
        with db().cursor() as cur:
            cur.execute("INSERT INTO users (email, name, role) VALUES (%s,%s,%s) RETURNING user_id",
                        (str(d["email"])[:128], d.get("name", ""), d.get("role", "viewer")))
            uid = cur.fetchone()[0]
    except psycopg2.errors.UniqueViolation:
        return jsonify(error="email ya existe"), 409
    return jsonify(status="created", user_id=uid), 201

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
    # 2 queries totales (devices + sensors en lote) en vez de N+1
    with db().cursor() as cur:
        cur.execute("SELECT device_id, name, fw, enabled, last_seen, zone_id FROM devices_v2 ORDER BY device_id")
        devs = cur.fetchall()
        ids = [d[0] for d in devs]
        sens_by_dev: dict[str, list] = {}
        if ids:
            cur.execute("""SELECT device_id, sensor_id, value, rn FROM (
                           SELECT device_id, sensor_id, value,
                                  row_number() OVER (PARTITION BY device_id, sensor_id
                                                     ORDER BY ts DESC) AS rn
                           FROM measurements_v2) t WHERE rn <= 2
                           ORDER BY device_id, sensor_id, rn""")
            prev_by_dev: dict[tuple, float] = {}
            for r in cur.fetchall():
                if r[3] == 2:  # rn=2 = penultima lectura = tendencia
                    prev_by_dev[(r[0], r[1])] = float(r[2])
            cur.execute("""SELECT device_id, sensor_id, type, unit, enabled, last_value, last_seen
                           FROM sensors_v2 WHERE device_id = ANY(%s) ORDER BY device_id, sensor_id""",
                        (ids,))
            for r in cur.fetchall():
                sens_by_dev.setdefault(r[0], []).append(
                    {"sensor_id": r[1], "type": r[2], "unit": r[3], "enabled": r[4],
                     "last_value": r[5], "last_seen": r[6].isoformat() if r[6] else None,
                     "prev": prev_by_dev.get((r[0], r[1]))})
    out = []
    for device_id, name, fw, enabled, last_seen, zone_id in devs:
        out.append({"device_id": device_id, "name": name, "fw": fw, "enabled": enabled,
                    "last_seen": last_seen.isoformat() if last_seen else None,
                    "zone_id": zone_id,
                    "status": status_of(last_seen) if enabled else "disabled",
                    "sensors": [s for s in sens_by_dev.get(device_id, []) if s["enabled"]]})
    return jsonify(devices=out)

@app.get("/api/v2/stats")
def stats():
    """Resumen global para el header del dashboard (1 endpoint, 3 queries)."""
    if not (admin_required() is None or check_key()):
        return jsonify(error="auth invalida"), 401
    with db().cursor() as cur:
        cur.execute("SELECT count(*) FILTER (WHERE enabled AND last_seen > now() - make_interval(secs => %s)),"
                    " count(*) FILTER (WHERE enabled) FROM devices_v2", (ONLINE_AFTER_S,))
        online, total = cur.fetchone()
        cur.execute("SELECT count(*), COALESCE(sum(liters),0) FROM irrigation_events "
                    "WHERE started_at > date_trunc('day', now())")
        irrig_today, liters_today = cur.fetchone()
        cur.execute("SELECT count(*) FROM alerts WHERE acked_at IS NULL")
        alerts_open = cur.fetchone()[0]
    return jsonify(devices_online=online, devices_total=total, irrigations_today=irrig_today,
                   liters_today=round(float(liters_today), 1), alerts_open=alerts_open)

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
    """Baja logica de sensor: acepta admin-key (gestion) o api-key (autonomia)."""
    if not (admin_required() is None or check_key()):
        return jsonify(error="auth invalida"), 401
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
    """El firmware reporta el resultado de ejecutar un comando; cierra el evento de riego abierto."""
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
        # cerrar el riego abierto de ese actuador y registrar litros del caudalimetro
        cur.execute("""UPDATE irrigation_events SET ended_at=now(),
                       duration_min=EXTRACT(EPOCH FROM (now()-started_at))/60,
                       liters=COALESCE(%s, liters)
                       WHERE actuator_id=(SELECT actuator_id FROM commands WHERE cmd_id=%s)
                       AND ended_at IS NULL""", (data.get("liters"), cmd_id))
    return jsonify(status="ok", cmd_id=cmd_id)

@app.get("/api/v2/config")
def device_config():
    """Umbrales de la zona para el modo degradado (firmware los cachea en NVS)."""
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    dev = request.args.get("device_id", "")
    with db().cursor() as cur:
        cur.execute("""SELECT z.zone_id, z.soil_min_pct, z.soil_max_pct, z.max_irrigation_min
                       FROM devices_v2 d JOIN zones z ON z.zone_id = d.zone_id
                       WHERE d.device_id=%s AND z.enabled""", (dev,))
        r = cur.fetchone()
        if not r:
            return jsonify({})
    return jsonify(zone_id=r[0], soil_min_pct=r[1], soil_max_pct=r[2], max_irrigation_min=r[3])

@app.post("/api/v2/irrigation/report")
def irrigation_report():
    """La placa reporta riegos hechos en modo offline (sin internet) al reconectar."""
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    d = body_json() or {}
    dev, events = str(d.get("device_id", "")), d.get("events", [])
    if not dev or not isinstance(events, list) or not 1 <= len(events) <= 100:
        return jsonify(error="device_id y events (1..100) requeridos"), 422
    with db().cursor() as cur:
        cur.execute("""SELECT actuator_id FROM actuators WHERE device_id=%s AND kind='pump'
                       AND enabled ORDER BY actuator_id LIMIT 1""", (dev,))
        r = cur.fetchone()
        if not r:
            return jsonify(error="el dispositivo no tiene bomba habilitada"), 409
        aid = r[0]
        n = 0
        for e in events:
            try:
                dur = min(max(float(e["duration_min"]), 0.1), 240.0)
                lit = float(e.get("liters", 0))
            except (KeyError, TypeError, ValueError):
                return jsonify(error=f"evento invalido: {e}"), 422
            # started_at acepta epoch (firmware) o ISO 8601
            raw = e.get("started_at")
            try:
                started = datetime.fromtimestamp(float(raw), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError):
                try:
                    started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    return jsonify(error=f"started_at invalido: {raw}"), 422
            ended = started + timedelta(minutes=dur)
            cur.execute("""INSERT INTO irrigation_events
                           (zone_id, actuator_id, started_at, ended_at, duration_min, liters, trigger, notes)
                           SELECT d.zone_id, %s, %s, %s, %s, %s, 'offline', 'reportado al reconectar'
                           FROM devices_v2 d WHERE d.device_id=%s""",
                        (aid, started, ended, dur, lit, dev))
            n += 1
    return jsonify(status="stored", count=n), 201

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

@app.get("/api/v2/history-agg")
def history_agg():
    """Tendencia desde el continuous aggregate horario de TimescaleDB.
    bucket: 1h|1d  ·  hours: ventana hacia atras (max 8760 = 1 anio)."""
    if not check_key():
        return jsonify(error="api-key invalida"), 401
    dev, sid = request.args.get("device_id", ""), request.args.get("sensor_id", "")
    if not dev or not sid:
        return jsonify(error="device_id y sensor_id requeridos"), 422
    bucket = {"1h": "1 hour", "1d": "1 day"}.get(request.args.get("bucket", "1h"), "1 hour")
    try:
        hours = min(max(int(request.args.get("hours", 168)), 1), 8760)
    except ValueError:
        return jsonify(error="hours invalido"), 422
    try:
        with db().cursor() as cur:
            if bucket == "1 day":  # re-agregar el agg horario a diario
                cur.execute(f"""SELECT time_bucket('1 day', bucket) AS d, avg(avg_v), min(min_v),
                                max(max_v), sum(n) FROM readings_hourly
                                WHERE device_id=%s AND sensor_id=%s
                                AND bucket > now() - interval '{hours} hours'
                                GROUP BY d ORDER BY d""", (dev, sid))
            else:
                cur.execute(f"""SELECT bucket, avg_v, min_v, max_v, n FROM readings_hourly
                                WHERE device_id=%s AND sensor_id=%s AND bucket > now() - interval '{hours} hours'
                                ORDER BY bucket""", (dev, sid))
            return jsonify([{"ts": r[0].isoformat(), "avg": r[1], "min": r[2], "max": r[3], "n": r[4]}
                            for r in cur.fetchall()])
    except psycopg2.errors.UndefinedTable:
        return jsonify(error="readings_hourly no existe (correr seed)"), 503

DASHBOARD_HTML = """<!doctype html>
<html lang="es" class="h-full bg-slate-950 text-slate-100 antialiased">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AgroSense · Inteligencia y Automatización Agrícola</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <script src="https://unpkg.com/lucide@latest"></script>

  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'system-ui', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: {
              50: '#ecfdf5', 100: '#d1fae5', 400: '#34d399', 500: '#10b981',
              600: '#059669', 900: '#064e3b',
            },
            surface: {
              900: '#0d131f', 850: '#111927', 800: '#162235', 750: '#1e2d42',
              700: '#283850', border: '#1f2e45',
            }
          }
        }
      }
    }
  </script>
  <style>
    body {
      font-feature-settings: "cv02", "cv03", "cv04", "cv11";
      background: #090d16;
      background-image: radial-gradient(at 100% 0%, rgba(16, 185, 129, 0.05) 0px, transparent 50%),
                        radial-gradient(at 0% 20%, rgba(14, 165, 233, 0.04) 0px, transparent 40%);
    }
    .custom-scroll::-webkit-scrollbar { width: 6px; height: 6px; }
    .custom-scroll::-webkit-scrollbar-track { background: rgba(15, 23, 42, 0.4); }
    .custom-scroll::-webkit-scrollbar-thumb { background: rgba(51, 65, 85, 0.6); border-radius: 4px; }
    .custom-scroll::-webkit-scrollbar-thumb:hover { background: rgba(100, 116, 139, 0.8); }
    /* --- guardas anti-overflow (proyecto completo) --- */
    html, body { overflow-x: hidden; max-width: 100vw; }
    .stat, .zone, .card, .ev, .alert, #chartbox { min-width: 0; max-width: 100%; }
    .card *, .ev *, .alert * { min-width: 0; }
    .card .val, #charttitle, .ev b, .alert b { word-break: break-word; }
    .gauge { max-width: 100%; }
    canvas { display: block; max-width: 100%; }
  </style>
</head>
<body class="min-h-full flex flex-col font-sans selection:bg-emerald-500/20 selection:text-emerald-300">

  <!-- Banner de modo demo (solo cuando la API no responde) -->
  <div id="demoBanner" class="hidden sticky top-0 z-50 bg-amber-500/90 text-slate-950 text-xs font-semibold text-center py-1.5 tracking-wide">
    MODO DEMOSTRACIÓN — datos simulados (no se pudo conectar a la API real)
  </div>

  <!-- Top Navigation Bar -->
  <header class="sticky top-0 z-40 bg-slate-950/80 backdrop-blur-md border-b border-slate-800/80 px-4 sm:px-8 py-3.5">
    <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3.5">
        <div class="w-11 h-11 rounded-xl bg-gradient-to-br from-emerald-500 to-teal-700 flex items-center justify-center shadow-lg shadow-emerald-500/20 ring-1 ring-white/20">
          <i data-lucide="sprout" class="w-5 h-5 text-white"></i>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <span class="font-bold text-lg sm:text-xl tracking-tight text-white">AgroSense</span>
            <span class="text-xs px-2 py-0.5 rounded-full font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">v2.4 Pro</span>
          </div>
          <p class="text-xs text-slate-400 flex items-center gap-1.5">
            <span class="relative flex h-2 w-2">
              <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
              <span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
            </span>
            Telemetría en tiempo real · Auto-refresh 5s
          </p>
        </div>
      </div>

      <div class="flex items-center flex-wrap gap-3 text-xs min-w-0">
        <div class="hidden md:flex items-center gap-3 px-3 py-1.5 rounded-lg bg-slate-900/90 border border-slate-800 text-slate-400 font-medium">
          <span class="text-slate-500 text-[11px] uppercase tracking-wider">Estado de suelo:</span>
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-rose-500 shadow-sm shadow-rose-500/50"></span> Seco</span>
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-emerald-400 shadow-sm shadow-emerald-400/50"></span> Óptimo</span>
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-sky-400 shadow-sm shadow-sky-400/50"></span> Saturado</span>
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-amber-400 shadow-sm shadow-amber-400/50"></span> Alerta</span>
        </div>

        <div class="flex items-center gap-2">
          <button onclick="refresh()" title="Actualizar datos ahora" class="p-2 rounded-lg bg-slate-900 hover:bg-slate-800 text-slate-300 border border-slate-800 transition active:scale-95 flex items-center gap-1.5">
            <i data-lucide="refresh-cw" class="w-4 h-4"></i>
            <span class="hidden sm:inline">Refrescar</span>
          </button>
          <button onclick="promptKey()" title="Configurar API Key" class="px-3 py-1.5 rounded-lg bg-slate-800/80 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-medium transition flex items-center gap-1.5">
            <i data-lucide="key" class="w-3.5 h-3.5 text-emerald-400"></i>
            <span id="keyLabel">API Key</span>
          </button>
        </div>
      </div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 lg:p-8 space-y-6">

    <!-- KPI Metric Cards Grid -->
    <section id="stats" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4 sm:gap-5">
      <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-4 animate-pulse">
        <div class="w-12 h-12 rounded-xl bg-slate-800"></div>
        <div class="space-y-2 flex-1"><div class="h-3 bg-slate-800 rounded w-1/2"></div><div class="h-6 bg-slate-800 rounded w-3/4"></div></div>
      </div>
      <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-4 animate-pulse">
        <div class="w-12 h-12 rounded-xl bg-slate-800"></div><div class="space-y-2 flex-1"><div class="h-3 bg-slate-800 rounded w-1/2"></div><div class="h-6 bg-slate-800 rounded w-3/4"></div></div>
      </div>
      <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-4 animate-pulse">
        <div class="w-12 h-12 rounded-xl bg-slate-800"></div><div class="space-y-2 flex-1"><div class="h-3 bg-slate-800 rounded w-1/2"></div><div class="h-6 bg-slate-800 rounded w-3/4"></div></div>
      </div>
      <div class="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-4 animate-pulse">
        <div class="w-12 h-12 rounded-xl bg-slate-800"></div><div class="space-y-2 flex-1"><div class="h-3 bg-slate-800 rounded w-1/2"></div><div class="h-6 bg-slate-800 rounded w-3/4"></div></div>
      </div>
    </section>

    <!-- Alert Banner Area -->
    <section class="rounded-2xl bg-slate-900/70 border border-slate-800/90 overflow-hidden shadow-lg shadow-black/20">
      <div class="px-5 py-3.5 border-b border-slate-800/80 flex items-center justify-between bg-slate-950/40">
        <div class="flex items-center gap-2.5">
          <div class="p-1.5 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <i data-lucide="bell" class="w-4 h-4"></i>
          </div>
          <h2 class="text-sm font-semibold tracking-wide text-slate-100">Alertas de Sensores y Umbrales</h2>
        </div>
        <span id="alertsSummaryCount" class="text-xs px-2.5 py-0.5 rounded-full bg-slate-800 text-slate-300 font-medium">0 activas</span>
      </div>
      <div id="alerts" class="p-4 space-y-2.5 max-h-56 overflow-y-auto custom-scroll">
        <div class="text-xs text-slate-400 flex items-center gap-2 py-1">
          <i data-lucide="check-circle-2" class="w-4 h-4 text-emerald-400"></i>
          <span>Todo en orden — no se detectan anomalías.</span>
        </div>
      </div>
    </section>

    <!-- Two-Column Layout -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <div class="lg:col-span-8 space-y-6">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <i data-lucide="layers" class="w-4 h-4 text-emerald-400"></i>
            <h2 class="text-base font-semibold text-slate-100">Zonas de Cultivo y Nodos de Telemetría</h2>
          </div>
          <span class="text-xs text-slate-400 hidden sm:inline">Pulsa cualquier tarjeta para historial analítico</span>
        </div>

        <div id="root" class="space-y-6">
          <div class="text-center py-12 bg-slate-900/40 border border-slate-800/60 rounded-2xl">
            <i data-lucide="satellite" class="w-8 h-8 text-slate-500 mx-auto animate-bounce"></i>
            <p class="mt-3 text-sm text-slate-300 font-medium">Sincronizando con nodos de campo...</p>
            <p class="text-xs text-slate-500 mt-1">Si tarda, configura tu API-Key en el botón superior.</p>
          </div>
        </div>
      </div>

      <div class="lg:col-span-4 space-y-6">
        <div class="bg-slate-900/70 border border-slate-800/90 rounded-2xl p-5 shadow-lg shadow-black/20 flex flex-col">
          <div class="flex items-center justify-between pb-3.5 mb-3 border-b border-slate-800/80">
            <div class="flex items-center gap-2">
              <i data-lucide="history" class="w-4 h-4 text-sky-400"></i>
              <h2 class="text-sm font-semibold text-slate-100">Bitácora de Riego</h2>
            </div>
            <span class="text-[11px] font-mono uppercase tracking-wider text-slate-400">Tiempo Real</span>
          </div>
          <div id="feed" class="space-y-2.5 overflow-y-auto max-h-[460px] custom-scroll pr-1">
            <div class="text-xs text-slate-400 py-4 text-center">Cargando eventos recientes...</div>
          </div>
        </div>

        <div class="bg-gradient-to-br from-emerald-950/40 via-slate-900/60 to-slate-900/80 border border-emerald-900/40 rounded-2xl p-5 shadow-lg shadow-black/20">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-8 h-8 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 flex items-center justify-center">
              <i data-lucide="droplet" class="w-4 h-4"></i>
            </div>
            <div>
              <h3 class="text-sm font-semibold text-emerald-100">Riego Inteligente</h3>
              <p class="text-[11px] text-emerald-400/80">Algoritmo de balance hídrico activo</p>
            </div>
          </div>
          <p class="text-xs text-slate-300 leading-relaxed">
            Las válvulas se activan automáticamente cuando la humedad del suelo cae bajo el umbral mínimo de la zona, con histéresis y tope de seguridad, preservando hasta un 35% de recursos hídricos.
          </p>
          <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between text-xs text-slate-400">
            <span>Seguridad de actuadores</span>
            <span class="text-emerald-400 font-mono font-medium">100% OK</span>
          </div>
        </div>
      </div>
    </div>
  </main>

  <!-- Modal Chart -->
  <div id="overlay" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 hidden items-center justify-center p-4 transition-all duration-200">
    <div id="chartbox" class="bg-slate-900 border border-slate-800 rounded-2xl max-w-4xl w-full p-6 shadow-2xl shadow-black relative max-h-[90vh] overflow-y-auto custom-scroll" onclick="event.stopPropagation()">
      <div class="flex items-start justify-between pb-4 border-b border-slate-800">
        <div>
          <div class="flex items-center gap-2">
            <span class="px-2 py-0.5 rounded text-[11px] font-mono bg-sky-500/10 text-sky-400 border border-sky-500/20">Telemetría histórica</span>
            <h3 id="charttitle" class="text-xl font-bold text-white tracking-tight break-words">Sensor</h3>
          </div>
          <p class="text-xs text-slate-400 mt-1">Inspección de tendencias por agregación temporal</p>
        </div>
        <button onclick="closeChartModal()" class="text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-800 transition">
          <i data-lucide="x" class="w-5 h-5"></i>
        </button>
      </div>

      <div class="flex items-center justify-between mt-4 mb-6 flex-wrap gap-3">
        <div class="flex items-center gap-1.5 text-xs text-slate-400">
          <i data-lucide="calendar" class="w-3.5 h-3.5"></i>
          <span>Rango de análisis:</span>
        </div>
        <div id="rangos" class="flex items-center p-1 bg-slate-950 rounded-xl border border-slate-800 gap-1 text-xs"></div>
      </div>

      <div class="space-y-6">
        <div class="bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
          <span class="text-xs font-semibold text-slate-300 flex items-center gap-1.5 mb-2">
            <span class="w-2.5 h-2.5 rounded-full bg-sky-400"></span> Promedio Horario
          </span>
          <div class="h-64 relative w-full"><canvas id="ch1" class="chartbig w-full"></canvas></div>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
            <span class="text-xs font-semibold text-slate-300 flex items-center gap-1.5 mb-2">
              <span class="w-2.5 h-2.5 rounded-full bg-rose-400"></span> Registro Mínimo
            </span>
            <div class="h-48 relative w-full"><canvas id="ch2" class="chartbig w-full"></canvas></div>
          </div>
          <div class="bg-slate-950/60 p-4 rounded-xl border border-slate-800/80">
            <span class="text-xs font-semibold text-slate-300 flex items-center gap-1.5 mb-2">
              <span class="w-2.5 h-2.5 rounded-full bg-emerald-400"></span> Muestras Crudas Recientes
            </span>
            <div class="h-48 relative w-full"><canvas id="ch3" class="chartbig w-full"></canvas></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div id="toasts" class="fixed bottom-5 right-5 flex flex-col gap-2.5 z-50 pointer-events-none"></div>

  <script>
    let KEY = sessionStorage.KEY || "";
    let USING_FALLBACK = false;

    function promptKey() {
      const val = prompt("Ingresa la API-Key del dashboard:", KEY);
      if (val !== null) {
        KEY = sessionStorage.KEY = val.trim();
        updateKeyUI(); refresh();
      }
    }

    function updateKeyUI() {
      const label = document.getElementById("keyLabel");
      if (label) {
        label.textContent = KEY ? "Conectado" : "Configurar Key";
        label.className = KEY ? "text-emerald-400 font-medium" : "text-amber-400 font-medium";
      }
    }
    updateKeyUI();

    async function api(p, o={}) {
      const headers = { ...(o.headers || {}) };
      if (KEY) headers["X-Api-Key"] = KEY;
      const r = await fetch(p, { ...o, headers });
      if (!r.ok) throw new Error(r.status + " " + (await r.text()).slice(0, 120));
      return r.json();
    }

    function toast(msg, ok=true) {
      const wrap = document.getElementById("toasts");
      const t = document.createElement("div");
      t.className = `pointer-events-auto flex items-center gap-2.5 px-4 py-3 rounded-xl shadow-xl text-xs font-medium border backdrop-blur-md transition-all duration-300 transform translate-y-2 opacity-0 ${
        ok ? "bg-slate-900/90 text-emerald-300 border-emerald-500/30" : "bg-slate-900/90 text-rose-300 border-rose-500/30"}`;
      t.innerHTML = `<i data-lucide="${ok ? "check-circle" : "alert-circle"}" class="w-4 h-4 shrink-0"></i> <span>${msg}</span>`;
      wrap.appendChild(t);
      if (window.lucide) lucide.createIcons();
      requestAnimationFrame(() => t.classList.remove("translate-y-2", "opacity-0"));
      setTimeout(() => { t.classList.add("opacity-0", "translate-x-4"); setTimeout(() => t.remove(), 300); }, 4000);
    }

    function ago(iso) {
      if (!iso) return "sin datos";
      const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
      if (s < 60) return "hace " + s + "s";
      const min = Math.round(s / 60);
      if (min < 60) return "hace " + min + "m";
      return "hace " + Math.round(min / 60) + "h";
    }

    const SENSOR_META = {
      soil: { icon: "sprout", name: "Humedad de suelo", unitDefault: "%" },
      temperature: { icon: "thermometer", name: "Temperatura", unitDefault: "°C" },
      humidity: { icon: "droplets", name: "Humedad ambiente", unitDefault: "%" },
      battery: { icon: "battery-charging", name: "Batería nodo", unitDefault: "V" },
      solar: { icon: "sun", name: "Radiación solar", unitDefault: "W/m²" },
      rain: { icon: "cloud-rain", name: "Precipitación", unitDefault: "mm" },
      flow: { icon: "waves", name: "Caudal hídrico", unitDefault: "L/min" },
      ph: { icon: "test-tube", name: "Nivel de pH", unitDefault: "" },
      ec: { icon: "zap", name: "Conductividad (EC)", unitDefault: "µS/cm" }
    };

    const RANGE = {
      temperature: [-5, 45], humidity: [0, 100], battery: [3.0, 4.2],
      solar: [0, 1000], rain: [0, 50], flow: [0, 50], ph: [0, 14], ec: [0, 3000]
    };

    const PALETTE = {
      ok:  { hex: "#10b981", bg: "bg-emerald-500/10", border: "border-emerald-500/20", text: "text-emerald-400", badge: "bg-emerald-500/20 text-emerald-300" },
      warn: { hex: "#f59e0b", bg: "bg-amber-500/10", border: "border-amber-500/20", text: "text-amber-400", badge: "bg-amber-500/20 text-amber-300" },
      crit: { hex: "#ef4444", bg: "bg-rose-500/10", border: "border-rose-500/20", text: "text-rose-400", badge: "bg-rose-500/20 text-rose-300" },
      def: { hex: "#38bdf8", bg: "bg-sky-500/10", border: "border-sky-500/20", text: "text-sky-400", badge: "bg-sky-500/20 text-sky-300" }
    };

    let zones = {}, stateData = [];

    async function refresh() {
      try {
        const [st, zs, stats] = await Promise.all([
          api("/api/v2/state"),
          api("/api/v2/zones?limit=200").catch(() => []),
          api("/api/v2/stats").catch(() => null)
        ]);
        USING_FALLBACK = false;
        document.getElementById("demoBanner").classList.add("hidden");
        zones = {}; (zs || []).forEach(z => zones[z.zone_id] = z);
        stateData = st.devices || [];
        renderStats(stats); renderRoot(); loadAlerts();
      } catch (e) {
        // Modo demo: datos simulados claramente marcados
        USING_FALLBACK = true;
        document.getElementById("demoBanner").classList.remove("hidden");
        const st = mockDefaultState(); const zs = mockDefaultZones();
        zones = {}; zs.forEach(z => zones[z.zone_id] = z);
        stateData = st.devices;
        renderStats(mockDefaultStats()); renderRoot(); loadAlerts();
      }
    }

    function mockDefaultStats() {
      return { devices_online: 8, devices_total: 8, irrigations_today: 14, liters_today: 1850, alerts_open: 1 };
    }
    function mockDefaultZones() {
      return [
        { zone_id: 1, name: "Sector Norte · Viñedo Cabernet", crop: "Vid", soil_min_pct: 35, soil_max_pct: 65 },
        { zone_id: 2, name: "Sector Sur · Olivar Intensivo", crop: "Olivo", soil_min_pct: 25, soil_max_pct: 55 }
      ];
    }
    function mockDefaultState() {
      return {
        devices: [
          { device_id: "node-01", name: "Sonda Humedad Central", zone_id: 1, status: "online", last_seen: new Date().toISOString(),
            sensors: [
              { sensor_id: "s1", type: "soil", last_value: 48.2, unit: "%", prev: 47.1, last_seen: new Date().toISOString() },
              { sensor_id: "s2", type: "temperature", last_value: 23.4, unit: "°C", prev: 24.0, last_seen: new Date().toISOString() },
              { sensor_id: "s3", type: "battery", last_value: 4.12, unit: "V", prev: 4.13, last_seen: new Date().toISOString() },
              { sensor_id: "s4", type: "solar", last_value: 780, unit: "W/m²", prev: 750, last_seen: new Date().toISOString() }
            ]},
          { device_id: "node-02", name: "Estación Clima y Caudal", zone_id: 2, status: "online", last_seen: new Date().toISOString(),
            sensors: [
              { sensor_id: "s5", type: "soil", last_value: 22.0, unit: "%", prev: 23.8, last_seen: new Date().toISOString() },
              { sensor_id: "s6", type: "flow", last_value: 14.5, unit: "L/min", prev: 0, last_seen: new Date().toISOString() },
              { sensor_id: "s7", type: "humidity", last_value: 58, unit: "%", prev: 60, last_seen: new Date().toISOString() }
            ]}
        ]
      };
    }

    function renderStats(s) {
      if (!s) return;
      const isAlarm = s.alerts_open > 0;
      document.getElementById("stats").innerHTML = `
        <div class="bg-slate-900/80 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-3.5 sm:gap-4 shadow-lg shadow-black/10 hover:border-slate-700 transition">
          <div class="w-12 h-12 rounded-xl bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 flex items-center justify-center shrink-0"><i data-lucide="radio" class="w-5 h-5"></i></div>
          <div><span class="text-xs font-medium uppercase tracking-wider text-slate-400">Nodos en línea</span>
          <div class="text-2xl sm:text-3xl font-bold font-mono tracking-tight text-white mt-0.5"><span class="text-emerald-400">${s.devices_online}</span><span class="text-slate-500 text-lg">/${s.devices_total}</span></div></div>
        </div>
        <div class="bg-slate-900/80 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-3.5 sm:gap-4 shadow-lg shadow-black/10 hover:border-slate-700 transition">
          <div class="w-12 h-12 rounded-xl bg-sky-500/10 text-sky-400 border border-sky-500/20 flex items-center justify-center shrink-0"><i data-lucide="droplets" class="w-5 h-5"></i></div>
          <div><span class="text-xs font-medium uppercase tracking-wider text-slate-400">Ciclos de Riego</span>
          <div class="text-2xl sm:text-3xl font-bold font-mono tracking-tight text-white mt-0.5">${s.irrigations_today} <span class="text-xs font-sans text-slate-400 font-normal">hoy</span></div></div>
        </div>
        <div class="bg-slate-900/80 border border-slate-800/80 rounded-2xl p-4 sm:p-5 flex items-center gap-3.5 sm:gap-4 shadow-lg shadow-black/10 hover:border-slate-700 transition">
          <div class="w-12 h-12 rounded-xl bg-blue-500/10 text-blue-400 border border-blue-500/20 flex items-center justify-center shrink-0"><i data-lucide="gauge" class="w-5 h-5"></i></div>
          <div><span class="text-xs font-medium uppercase tracking-wider text-slate-400">Consumo Hídrico</span>
          <div class="text-2xl sm:text-3xl font-bold font-mono tracking-tight text-white mt-0.5">${Number(s.liters_today).toLocaleString()} <span class="text-xs font-mono text-slate-400 font-normal">L</span></div></div>
        </div>
        <div onclick="loadAlerts()" class="cursor-pointer bg-slate-900/80 border ${isAlarm ? 'border-amber-500/40 hover:border-amber-500' : 'border-slate-800/80 hover:border-slate-700'} rounded-2xl p-4 sm:p-5 flex items-center gap-3.5 sm:gap-4 shadow-lg shadow-black/10 transition group">
          <div class="w-12 h-12 rounded-xl ${isAlarm ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30' : 'bg-slate-800 text-slate-400'} flex items-center justify-center shrink-0 group-hover:scale-105 transition"><i data-lucide="alert-triangle" class="w-5 h-5"></i></div>
          <div><span class="text-xs font-medium uppercase tracking-wider text-slate-400">Alertas Activas</span>
          <div class="text-2xl sm:text-3xl font-bold font-mono tracking-tight ${isAlarm ? 'text-amber-400' : 'text-slate-300'} mt-0.5">${s.alerts_open}</div></div>
        </div>`;
      if (window.lucide) lucide.createIcons();
    }

    function sensorState(s, zone) {
      const v = s.last_value;
      if (v == null) return {};
      if (s.type === "soil" && zone) {
        if (v < zone.soil_min_pct) return { cls: "crit", tag: "SECO", color: PALETTE.crit };
        if (v > zone.soil_max_pct) return { cls: "warn", tag: "SATURADO", color: PALETTE.warn };
        return { cls: "ok", tag: "ÓPTIMO", color: PALETTE.ok };
      }
      if (s.type === "battery" && v < 3.5) return { cls: "crit", tag: "BATERÍA BAJA", color: PALETTE.crit };
      if (s.type === "temperature" && v > 35) return { cls: "warn", tag: "CALOR ALTO", color: PALETTE.warn };
      return { cls: "def", tag: "", color: PALETTE.def };
    }

    function gaugeHtml(s, zone) {
      let lo, hi;
      if (s.type === "soil") {
        lo = zone ? zone.soil_min_pct : 0;
        hi = zone ? zone.soil_max_pct : 100;
      } else if (s.type in RANGE) {
        [lo, hi] = RANGE[s.type];
      } else return "";
      const v = Math.max(lo, Math.min(hi, s.last_value ?? lo));
      const pct = Math.max(0, Math.min(100, ((v - lo) / (hi - lo) * 100))).toFixed(1);
      return `
        <div class="mt-3.5 pt-2 border-t border-slate-800/60">
          <div class="flex items-center justify-between text-[11px] font-mono text-slate-400 mb-1.5">
            <span>Rango ${lo}</span>
            <span class="font-medium text-slate-300">${pct}%</span>
            <span>${hi}</span>
          </div>
          <div class="h-2 w-full bg-slate-950 rounded-full overflow-hidden p-0.5 border border-slate-800">
            <div class="h-full rounded-full transition-all duration-500" style="width: ${pct}%; background-color: var(--card-accent, #38bdf8)"></div>
          </div>
        </div>`;
    }

    function trend(s) {
      if (s.prev == null || s.last_value == null) return "";
      const d = +(s.last_value - s.prev).toFixed(1);
      if (Math.abs(d) < 0.05)
        return `<span class="inline-flex items-center text-[10px] font-mono text-slate-400 bg-slate-800/80 px-1.5 py-0.5 rounded">· Estable</span>`;
      return d > 0
        ? `<span class="inline-flex items-center text-[10px] font-mono text-emerald-400 bg-emerald-500/10 border border-emerald-500/20 px-1.5 py-0.5 rounded">↑ +${d}</span>`
        : `<span class="inline-flex items-center text-[10px] font-mono text-rose-400 bg-rose-500/10 border border-rose-500/20 px-1.5 py-0.5 rounded">↓ ${d}</span>`;
    }

    function renderRoot() {
      const byZone = {}; const noZone = [];
      stateData.forEach(d => {
        if (d.zone_id && zones[d.zone_id]) (byZone[d.zone_id] ||= []).push(d);
        else noZone.push(d);
      });
      let html = "";
      for (const zid in zones) { const devs = byZone[zid] || []; if (devs.length) html += zoneHtml(zones[zid], devs); }
      if (noZone.length) html += zoneHtml(null, noZone);
      document.getElementById("root").innerHTML = html || `
        <div class="p-8 text-center bg-slate-900/60 border border-slate-800 rounded-2xl">
          <p class="text-sm text-slate-400">No se encontraron dispositivos conectados actualmente.</p>
        </div>`;
      if (window.lucide) lucide.createIcons();
    }

    function zoneHtml(z, devs) {
      const headActions = z ? `
        <div class="flex items-center gap-2 mt-2 sm:mt-0 ml-auto">
          <button onclick="irrigate(${z.zone_id}, 15)" class="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-emerald-600/90 hover:bg-emerald-500 text-white text-[13px] font-semibold shadow-md shadow-emerald-950 transition active:scale-95">
            <i data-lucide="droplet" class="w-3.5 h-3.5"></i><span>Riego 15m</span>
          </button>
          <button onclick="irrigate(${z.zone_id}, 0)" class="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-slate-800 hover:bg-rose-900/50 text-slate-300 hover:text-rose-300 border border-slate-700 text-[13px] font-medium transition active:scale-95">
            <i data-lucide="square" class="w-3 h-3"></i><span>Detener</span>
          </button>
        </div>` : "";
      const badges = z ? `
        <div class="flex items-center flex-wrap gap-2 text-xs">
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 font-medium">
            <i data-lucide="leaf" class="w-3 h-3"></i> ${z.crop || "Cultivo general"}
          </span>
          <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-slate-800/80 border border-slate-700/80 text-slate-300 font-mono text-[11px]">
            Umbrales: <b class="text-white">${z.soil_min_pct}%</b> min · <b class="text-white">${z.soil_max_pct}%</b> max
          </span>
        </div>` : `<span class="text-xs px-2.5 py-1 rounded-lg bg-slate-800 text-slate-400">Sin zona asignada</span>`;

      return `
        <div class="bg-slate-900/70 border border-slate-800/90 rounded-2xl p-5 shadow-lg shadow-black/20 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-3 pb-3.5 border-b border-slate-800/80 min-w-0">
            <div class="min-w-0"><h3 class="text-lg font-semibold text-white tracking-tight break-words">${z ? z.name : "Equipos en campo"}</h3>
            <div class="mt-1.5">${badges}</div></div>
            ${headActions}
          </div>
          <div class="space-y-4">
            ${devs.length ? devs.map(devHtml).join("") : `<div class="p-4 text-xs text-slate-500 text-center">No hay sondas reportando en esta zona.</div>`}
          </div>
        </div>`;
    }

    function devHtml(d) {
      const isOnline = d.status === "online";
      return `
        <div class="bg-slate-950/40 border border-slate-800/60 rounded-xl p-4 sm:p-5">
          <div class="flex items-center justify-between gap-3 mb-3 flex-wrap min-w-0">
            <div class="flex items-center gap-2.5 min-w-0">
              <span class="w-2.5 h-2.5 rounded-full ${isOnline ? 'bg-emerald-400 shadow-sm shadow-emerald-400/50' : 'bg-rose-500'}"></span>
              <span class="text-[15px] font-semibold text-slate-100 break-words">${d.name || d.device_id}</span>
              <span class="text-[11px] font-mono text-slate-400 px-2 py-0.5 rounded bg-slate-900 border border-slate-800">${d.device_id}</span>
            </div>
            <div class="flex items-center gap-3">
              <span class="text-xs text-slate-400 font-mono">${ago(d.last_seen)}</span>
              <button onclick="rmDev('${d.device_id}')" title="Desvincular nodo" class="text-xs text-slate-500 hover:text-rose-400 p-1 rounded hover:bg-slate-900 transition"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
            </div>
          </div>
          <div class="grid gap-3.5 [grid-template-columns:repeat(auto-fill,minmax(215px,1fr))]">
            ${(d.sensors && d.sensors.length) ? d.sensors.map(s => sensorHtml(d, s)).join("") : `<div class="col-span-full text-[11px] text-slate-500 text-center py-2">Nodo sin sensores activos</div>`}
          </div>
        </div>`;
    }

    function sensorHtml(d, s) {
      const zone = zones[d.zone_id];
      const st = sensorState(s, zone);
      const colorObj = st.color || PALETTE.def;
      const meta = SENSOR_META[s.type] || { icon: "activity", name: s.type || s.sensor_id, unitDefault: "" };
      const displayUnit = s.unit || meta.unitDefault;
      return `
        <div class="relative group bg-slate-900/90 border border-slate-800 hover:border-slate-700 rounded-xl p-4 sm:p-4.5 cursor-pointer transition-all duration-200 hover:-translate-y-0.5 shadow-sm overflow-hidden"
             style="--card-accent: ${colorObj.hex}"
             onclick="openChart('${d.device_id}','${s.sensor_id}','${s.type || s.sensor_id}','${displayUnit}')"
             data-dev="${d.device_id}" data-sid="${s.sensor_id}" data-color="${colorObj.hex}">
          <div class="flex items-start justify-between gap-2">
            <div class="flex items-center gap-2">
              <div class="w-8 h-8 rounded-lg ${colorObj.bg} ${colorObj.text} border ${colorObj.border} flex items-center justify-center shrink-0">
                <i data-lucide="${meta.icon}" class="w-4 h-4"></i>
              </div>
              <div class="text-[13px] font-medium text-slate-200 truncate max-w-[128px]" title="${meta.name}">${meta.name}</div>
            </div>
            ${st.tag ? `<span class="text-[10px] font-bold px-1.5 py-0.5 rounded ${colorObj.badge} tracking-wider">${st.tag}</span>` : ''}
          </div>
          <div class="mt-2.5 flex items-baseline justify-between gap-2 flex-wrap">
            <div class="text-3xl font-bold font-mono text-white tracking-tight min-w-0 break-words text-left">${s.last_value ?? "--"}<span class="text-xs font-sans text-slate-400 font-normal">${displayUnit}</span></div>
            ${trend(s)}
          </div>
          ${gaugeHtml(s, zone)}
          <div class="h-10 mt-3 relative"><canvas id="sp-${d.device_id}-${s.sensor_id}"></canvas></div>
          <div class="mt-2 flex items-center justify-between text-[11px] text-slate-400 pt-1.5 border-t border-slate-800/40">
            <span class="font-mono text-[10px]">${ago(s.last_seen)}</span>
            <button onclick="event.stopPropagation(); rmSens('${d.device_id}','${s.sensor_id}')" class="text-slate-500 hover:text-rose-400 opacity-0 group-hover:opacity-100 transition p-0.5"><i data-lucide="x" class="w-3 h-3"></i></button>
          </div>
        </div>`;
    }

    async function sparklines() {
      for (const c of document.querySelectorAll(".card[data-dev]")) {
        const dev = c.dataset.dev, sid = c.dataset.sid, color = c.dataset.color || "#38bdf8";
        if (!dev || !sid) continue;
        try {
          const agg = await api(`/api/v2/history-agg?device_id=${dev}&sensor_id=${sid}&bucket=1h&hours=24`);
          const cv = document.getElementById(`sp-${dev}-${sid}`);
          if (!cv || !agg.length) continue;
          const old = Chart.getChart(cv); if (old) old.destroy();
          new Chart(cv, { type: "line",
            data: { labels: agg.map(() => ""), datasets: [{
              data: agg.map(x => x.avg), borderColor: color, borderWidth: 1.5,
              pointRadius: 0, tension: 0.35, fill: true, backgroundColor: color + "15" }]},
            options: { responsive: true, maintainAspectRatio: false, animation: false,
              plugins: { legend: { display: false }, tooltip: { enabled: false } },
              scales: { x: { display: false }, y: { display: false } } } });
        } catch (e) {}
      }
    }

    function ensureAdm() {
      return sessionStorage.ADM || (sessionStorage.ADM = prompt("Clave de Administrador (X-Admin-Key) requerida para gobernar riegos y alertas:") || "");
    }

    async function loadAlerts() {
      try {
        const list = await api("/api/v2/alerts");
        const box = document.getElementById("alerts");
        const countBadge = document.getElementById("alertsSummaryCount");
        if (countBadge) countBadge.textContent = `${list.length} activas`;
        if (!list.length) {
          box.innerHTML = `<div class="text-xs text-slate-400 flex items-center gap-2 py-2">
            <i data-lucide="check-circle-2" class="w-4 h-4 text-emerald-400"></i>
            <span>Todo en orden — sin alertas de estrés hídrico ni fallos en nodos.</span></div>`;
        } else {
          box.innerHTML = list.map(a => {
            const isCrit = a.severity === "critical";
            const borderClr = isCrit ? "border-rose-500/40 bg-rose-500/10 text-rose-300" : "border-amber-500/40 bg-amber-500/10 text-amber-300";
            const iconName = isCrit ? "alert-octagon" : "alert-triangle";
            return `
              <div class="flex items-center justify-between gap-3 p-3.5 rounded-xl border ${borderClr} text-[13px] flex-wrap">
                <div class="flex items-center gap-2.5 min-w-0 flex-1">
                  <i data-lucide="${iconName}" class="w-4 h-4 shrink-0"></i>
                  <div class="min-w-0"><span class="font-bold tracking-wide uppercase text-[11px] break-words">${a.kind}</span>
                  <p class="text-slate-300 mt-0.5 break-words">${a.message}</p></div>
                </div>
                ${sessionStorage.ADM ? `<button onclick="ack(${a.alert_id})" class="px-2.5 py-1 rounded-lg bg-slate-900/80 hover:bg-slate-800 text-slate-200 border border-slate-700 text-xs font-medium transition active:scale-95 shrink-0">Confirmar (Ack)</button>` : ""}
              </div>`;
          }).join("");
        }
        if (window.lucide) lucide.createIcons();
      } catch (e) {
        document.getElementById("alerts").innerHTML = `<div class="text-xs text-slate-400">No se pudieron consultar alertas: ${e.message}</div>`;
      }
    }

    async function ack(id) {
      const r = await fetch(`/api/v2/alerts/${id}/ack`, { method: "POST", headers: { "X-Admin-Key": ensureAdm() } });
      r.ok ? toast("Alerta reconocida exitosamente") : toast("Error al reconocer (verificar Admin Key)", false);
      loadAlerts();
    }

    async function irrigate(zid, min) {
      const k = ensureAdm(); if (!k) return;
      let body = min > 0 ? { duration_min: min } : { stop: true };
      if (min > 0) {
        const m = prompt("Duración del riego en minutos (1 a 120):", min);
        if (!m) return;
        body = { duration_min: Math.min(120, parseInt(m) || min) };
      }
      try {
        const r = await fetch(`/api/v2/zones/${zid}/irrigate`, { method: "POST",
          headers: { "Content-Type": "application/json", "X-Admin-Key": k }, body: JSON.stringify(body) });
        if (!r.ok) toast("Fallo al enviar comando: " + (await r.text()).slice(0, 80), false);
        else { toast(min > 0 ? `💧 Solicitud de riego (${body.duration_min} min) encolada` : "⏹ Orden de parada enviada"); refresh(); feed(); }
      } catch (err) {
        toast("No se pudo contactar el actuador: " + err.message, false);
      }
    }

    async function rmSens(d, s) {
      if (confirm(`¿Dar de baja lógica al sensor '${s}' del nodo '${d}'?`)) {
        await api(`/api/v2/devices/${d}/sensors/${s}`, { method: "DELETE" });
        toast("Sensor dado de baja"); refresh();
      }
    }
    async function rmDev(d) {
      if (confirm(`¿Desvincular nodo de campo '${d}'?`)) {
        await api(`/api/v2/devices/${d}`, { method: "DELETE" });
        toast("Dispositivo desvinculado"); refresh();
      }
    }

    const TRIGGER_ICONS = { rule: "cpu", manual: "user-check", offline: "wifi-off", schedule: "clock" };

    async function feed() {
      try {
        const evs = await api("/api/v2/irrigation-events?limit=8");
        document.getElementById("feed").innerHTML = (evs || []).map(e => {
          const done = e.duration_min != null;
          const icon = TRIGGER_ICONS[e.trigger] || "activity";
          return `
            <div class="p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-center justify-between text-xs gap-3 hover:border-slate-700 transition min-w-0">
              <div class="flex items-center gap-2.5 min-w-0 flex-1">
                <div class="w-7 h-7 rounded-lg bg-slate-900 text-slate-400 border border-slate-800 flex items-center justify-center shrink-0"><i data-lucide="${icon}" class="w-3.5 h-3.5"></i></div>
                <div>
                  <div class="font-medium text-slate-200 break-words">${e.zone} · <span class="text-[10px] font-mono uppercase px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">${e.trigger}</span></div>
                  <div class="text-[11px] text-slate-400 mt-0.5 break-words">${done ? `Riego completado: ${e.duration_min.toFixed(0)} min (${e.liters ? e.liters + " L" : ""})` : `<span class="text-emerald-400 font-medium animate-pulse">Riego en curso ⏳</span>`}</div>
                </div>
              </div>
              <span class="text-[10px] font-mono text-slate-400 shrink-0">${ago(e.started_at)}</span>
            </div>`;
        }).join("") || `<div class="text-xs text-slate-500 py-3 text-center">Sin actividad reciente</div>`;
        if (window.lucide) lucide.createIcons();
      } catch (e) {
        document.getElementById("feed").innerHTML = `<div class="text-xs text-slate-500 py-3 text-center">No se pudo cargar la bitácora</div>`;
      }
    }

    function closeChartModal() {
      document.getElementById("overlay").style.display = "none";
    }
    document.getElementById("overlay").addEventListener("click", function (e) { if (e.target === this) closeChartModal(); });

    function mkChart(id, labels, data, label, color) {
      const cv = document.getElementById(id);
      if (!cv) return;
      const old = Chart.getChart(cv); if (old) old.destroy();
      return new Chart(cv, { type: "line",
        data: { labels, datasets: [{ label, data, borderColor: color, borderWidth: 2,
          backgroundColor: color + "18", pointRadius: data.length > 30 ? 0 : 3,
          pointHoverRadius: 5, tension: 0.3, fill: true }] },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false },
            tooltip: { backgroundColor: "#0f172a", titleColor: "#94a3b8", bodyColor: "#f8fafc",
                       borderColor: "#334155", borderWidth: 1, padding: 10, boxPadding: 4 } },
          scales: { x: { grid: { color: "#1e293b" }, ticks: { color: "#64748b", maxTicksLimit: 8, font: { size: 10 } } },
                    y: { grid: { color: "#1e293b" }, ticks: { color: "#64748b", font: { size: 10 } } } } } });
    }

    async function openChart(dev, sid, type, unit, rango) {
      const ov = document.getElementById("overlay");
      ov.style.display = "flex";
      const meta = SENSOR_META[type] || { name: type };
      document.getElementById("charttitle").textContent = `${meta.name} · ${dev} / ${sid} (${unit || ""})`;
      const activeRange = rango || "7d";
      document.getElementById("rangos").innerHTML = ["24h", "7d", "1a"].map(r => `
        <button onclick="openChart('${dev}','${sid}','${type}','${unit}','${r}')"
                class="px-2.5 py-1 rounded-lg transition ${r === activeRange ? 'bg-emerald-500 text-slate-950 font-semibold' : 'text-slate-400 hover:text-white'}">${r}</button>`).join("");
      const horas = { "24h": 24, "7d": 168, "1a": 8760 }[activeRange] || 168;
      try {
        const agg = await api(`/api/v2/history-agg?device_id=${dev}&sensor_id=${sid}&bucket=1h&hours=${horas}`);
        const labels = agg.map(x => new Date(x.ts).toLocaleString("es", { day: "2-digit", month: "2-digit", hour: "2-digit" }));
        mkChart("ch1", labels, agg.map(x => x.avg), "Promedio horario", "#38bdf8");
        mkChart("ch2", labels, agg.map(x => x.min), "Mínimo registrado", "#f87171");
      } catch (e) { toast("Sin datos agregados para este rango", false); }
      try {
        const raw = await api(`/api/v2/history?device_id=${dev}&sensor_id=${sid}&limit=50`);
        const lb2 = raw.map(x => new Date(x.ts).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })).reverse();
        mkChart("ch3", lb2, raw.map(x => x.value).reverse(), "Muestras directas", "#10b981");
      } catch (e) {}
      if (window.lucide) lucide.createIcons();
    }

    window.addEventListener("DOMContentLoaded", () => {
      refresh(); sparklines(); feed();
      setInterval(refresh, 5000);
      setInterval(sparklines, 60000);
      setInterval(feed, 15000);
      if (window.lucide) lucide.createIcons();
    });
  </script>
</body>
</html>"""


@app.get("/")
@app.get("/dashboard")
def index_redirect():
    """La raiz siempre lleva al dashboard (evita 404 al compartir la URL a secas)."""
    from flask import redirect
    return redirect("/dashboard-v2")

@app.get("/dashboard-v2")
def dashboard_v2():
    """Dashboard informativo: zonas, estados agronomicos, tendencia, feed y toasts."""
    from flask import Response
    return Response(DASHBOARD_HTML, mimetype="text/html")



if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8000)
else:
    try:
        init_db()
    except Exception as e:
        log.warning("init_db diferido: %s", e)
