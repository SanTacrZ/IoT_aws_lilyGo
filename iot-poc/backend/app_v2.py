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

DASHBOARD_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AgroSense - Dashboard</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:1280px;margin:0 auto;padding:1rem;color:#e2e8f0;
  background:radial-gradient(1200px 800px at 80% -10%, #1e3a5f55, transparent), #0b1220}
h1{font-size:1.35rem;margin:.2rem 0;letter-spacing:-.02em}
h3{font-size:.95rem;margin:1.2rem 0 .5rem;color:#cbd5e1}
small{color:#94a3b8}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.6rem;margin:.8rem 0}
.stat{background:linear-gradient(160deg,#1c2a44,#16213a);border:1px solid #ffffff10;border-radius:14px;padding:.65rem .9rem;
  text-align:center;cursor:pointer;transition:transform .15s, box-shadow .15s}
.stat:hover{transform:translateY(-2px);box-shadow:0 6px 18px #0006}
.stat b{font-size:1.45rem;display:block;font-variant-numeric:tabular-nums}
.stat span{font-size:.7rem;color:#94a3b8;letter-spacing:.03em;text-transform:uppercase}
.stat.alarm b{color:#f87171}
.zone{background:linear-gradient(160deg,#1c2a44,#141d33);border:1px solid #ffffff10;border-radius:16px;
  padding:1rem;margin:1rem 0;box-shadow:0 10px 30px #0005}
.zonehead{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.zone h3{margin:0;font-size:1.05rem;color:#f1f5f9}
.chip{background:#0f172a99;border:1px solid #ffffff14;border-radius:999px;padding:.2rem .7rem;font-size:.7rem;color:#cbd5e1}
.chip b{color:#e2e8f0}
.badge{font-size:.78rem;font-weight:700}.online{color:#4ade80}.stale{color:#facc15}.offline{color:#f87171}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:.7rem;margin-top:.7rem}
.card{background:linear-gradient(165deg,#2a3a57,#1e293b);border:1px solid #ffffff12;border-radius:14px;
  padding:.8rem;text-align:center;cursor:pointer;position:relative;overflow:hidden;
  transition:transform .16s, box-shadow .16s, border-color .16s}
.card::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent,#38bdf8);opacity:.9}
.card:hover{transform:translateY(-3px);box-shadow:0 10px 24px #0007;border-color:#38bdf840}
.card.crit::before{background:#f87171}.card.warn::before{background:#facc15}.card.ok::before{background:#4ade80}
.cardtop{display:flex;align-items:center;gap:.5rem;justify-content:flex-start;text-align:left}
.icon{width:32px;height:32px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:1.05rem;
  background:#0f172a99;border:1px solid #ffffff14}
.card .lbl{font-size:.7rem;color:#94a3b8;text-align:left;line-height:1.15}
.card .lbl b{display:block;color:#e2e8f0;font-size:.72rem}
.card .val{font-size:1.7rem;font-weight:800;margin:.25rem 0 .1rem;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.card .val small{font-size:.8rem;font-weight:600;color:#94a3b8;margin-left:.15rem}
.meta{display:flex;gap:.35rem;justify-content:center;align-items:center;margin:.15rem 0}
.trend{font-size:.68rem;font-weight:700;border-radius:999px;padding:.1rem .5rem}
.trend.up{color:#4ade80;background:#4ade8016}.trend.down{color:#f87171;background:#f8717116}.trend.flat{color:#94a3b8;background:#ffffff0d}
.tchip{font-size:.65rem;color:#94a3b8}
.gauge{position:relative;height:6px;border-radius:999px;background:#0f172acc;margin:.45rem .2rem .2rem;overflow:visible}
.gauge .fill{position:absolute;inset:0 auto 0 0;border-radius:999px;background:var(--accent,#38bdf8)}
.gauge .mark{position:absolute;top:-2px;bottom:-2px;width:2px;background:#ffffff88}
.gauge .lab{position:absolute;top:9px;font-size:.58rem;color:#64748b}
.gauge .lab.l{left:0}.gauge .lab.r{right:0}
.spark{height:30px;margin-top:.5rem}
.cardfoot{display:flex;justify-content:space-between;align-items:center;margin-top:.35rem}
.cardfoot button{opacity:.85}
.feed{display:flex;flex-direction:column;gap:.35rem}
.ev{display:flex;gap:.6rem;align-items:center;background:linear-gradient(160deg,#1c2a44,#16213a);
  border:1px solid #ffffff10;border-radius:10px;padding:.5rem .8rem;font-size:.82rem}
.ev .t{margin-left:auto;font-size:.7rem;color:#94a3b8}
.pill{border-radius:999px;padding:.1rem .55rem;font-size:.7rem}
.pill.rule{background:#164e63;color:#67e8f9}.pill.manual{background:#3b0764;color:#d8b4fe}
.pill.offline{background:#450a0a;color:#fca5a5}
.alert{background:linear-gradient(160deg,#1c2a44,#16213a);border-radius:10px;padding:.5rem .8rem;margin:.3rem 0;font-size:.85rem}
.alert.warn{border-left:4px solid #facc15}.alert.critical{border-left:4px solid #f87171}
.alert.info{border-left:4px solid #38bdf8}
button{background:#334155;color:#fff;border:1px solid #ffffff14;border-radius:9px;padding:.32rem .6rem;cursor:pointer;
  font-size:.73rem;transition:filter .15s}
button:hover{filter:brightness(1.2)}
button.primary{background:#16a34a}button.stop{background:#dc2626}
#toasts{position:fixed;bottom:1rem;right:1rem;display:flex;flex-direction:column;gap:.4rem;z-index:99}
.toast{background:#16a34a;color:#fff;padding:.6rem 1rem;border-radius:10px;font-size:.85rem;box-shadow:0 8px 20px #0007}
.toast.err{background:#dc2626}
#overlay{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center;z-index:9}
#chartbox{background:linear-gradient(160deg,#1c2a44,#141d33);border:1px solid #ffffff14;border-radius:16px;
  padding:1rem;width:min(760px,95vw);box-shadow:0 20px 60px #0009}
canvas.chartbig{max-height:250px}
.legend{display:flex;gap:1rem;font-size:.68rem;color:#94a3b8;margin:.3rem 0}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:.3rem}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script></head><body>
<h1>🌱 AgroSense <small>— agricultura de precisión · auto-refresh 5s</small></h1>
<div class="legend">
  <span><i class="dot" style="background:#f87171"></i>seco</span>
  <span><i class="dot" style="background:#4ade80"></i>óptimo</span>
  <span><i class="dot" style="background:#38bdf8"></i>saturado</span>
  <span><i class="dot" style="background:#facc15"></i>revisar</span>
</div>
<div id="stats" class="stats">Cargando…</div>
<h3>🔔 Alertas abiertas</h3>
<div id="alerts"><small>Cargando…</small></div>
<h3>🗺️ Zonas y equipos</h3>
<div id="root">Cargando…</div>
<h3>📜 Actividad reciente</h3>
<div id="feed" class="feed">Cargando…</div>
<div id="toasts"></div>

<script>
const KEY = sessionStorage.KEY || (sessionStorage.KEY = prompt("API-Key del dashboard:") || "");
async function api(p, o={}) {
  const r = await fetch(p, {...o, headers: {...(o.headers||{}), "X-Api-Key": KEY}});
  if (!r.ok) throw new Error(r.status + " " + (await r.text()).slice(0,120));
  return r.json();
}
function toast(msg, ok=true){
  const t = document.createElement("div");
  t.className = "toast" + (ok ? "" : " err"); t.textContent = msg;
  document.getElementById("toasts").appendChild(t);
  setTimeout(()=>t.remove(), 4000);
}
function ago(iso){
  if(!iso) return "sin datos";
  const s = Math.round((Date.now() - new Date(iso).getTime())/1000);
  if (s < 90) return "hace " + s + "s";
  return "hace " + Math.round(s/60) + "min";
}
const ICON = {soil:"🌱", temperature:"🌡", humidity:"💧", battery:"🔋", solar:"☀️", rain:"🌧", flow:"🚰", ph:"⚗️", ec:"⚡"};
const NAME = {soil:"Humedad de suelo", temperature:"Temperatura", humidity:"Humedad aire",
              battery:"Batería", solar:"Radiación solar", rain:"Lluvia", flow:"Caudal", ph:"pH", ec:"EC"};
// rango del medidor por tipo de sensor
const RANGE = {temperature:[-5,45], humidity:[0,100], battery:[3.0,4.2], solar:[0,1000],
               rain:[0,50], flow:[0,50], ph:[0,14], ec:[0,3000]};
const COL = {ok:"#4ade80", warn:"#facc15", crit:"#f87171", def:"#38bdf8"};

let zones = {}, stateData = [];

async function refresh(){
  try{
    const [st, zs, stats] = await Promise.all([
      api("/api/v2/state"), api("/api/v2/zones?limit=200").catch(()=>[]), api("/api/v2/stats").catch(()=>null)
    ]);
    zones = {}; (zs || []).forEach(z => zones[z.zone_id] = z);
    stateData = st.devices;
    renderStats(stats); renderRoot();
    if(sessionStorage.ADM) loadAlerts();
  }catch(e){ document.getElementById("root").innerHTML = "Error: " + e.message; }
}
function renderStats(s){
  if(!s) return;
  document.getElementById("stats").innerHTML = `
    <div class="stat"><b><span class="online">${s.devices_online}</span>/${s.devices_total}</b><span>en línea</span></div>
    <div class="stat"><b>${s.irrigations_today}</b><span>riegos hoy</span></div>
    <div class="stat"><b>${s.liters_today}L</b><span>agua hoy</span></div>
    <div class="stat ${s.alerts_open ? "alarm" : ""}" onclick="loadAlerts()"><b>${s.alerts_open}</b><span>alertas</span></div>`;
}
function sensorState(s, zone){
  const v = s.last_value;
  if (v == null) return {};
  if (s.type === "soil" && zone){
    if (v < zone.soil_min_pct) return {cls:"crit", tag:"SECAS"};
    if (v > zone.soil_max_pct) return {cls:"warn", tag:"SATURADO"};
    return {cls:"ok", tag:"ÓPTIMO"};
  }
  if (s.type === "battery" && v < 3.5) return {cls:"crit", tag:"BAJA"};
  if (s.type === "temperature" && v > 35) return {cls:"warn", tag:"CALOR"};
  return {};
}
function gaugeHtml(s, zone){
  let lo, hi;
  if (s.type === "soil") { lo = zone ? zone.soil_min_pct : 0; hi = zone ? zone.soil_max_pct : 100; }
  else if (s.type in RANGE) { [lo, hi] = RANGE[s.type]; }
  else return "";
  const v = Math.max(lo, Math.min(hi, s.last_value ?? lo));
  const pct = ((v - lo) / (hi - lo) * 100).toFixed(1);
  return `<div class="gauge"><div class="fill" style="width:${pct}%"></div>
    <div class="mark" style="left:${pct}%"></div>
    <span class="lab l">${lo}</span><span class="lab r">${hi}</span></div>`;
}
function trend(s){
  if (s.prev == null || s.last_value == null) return "";
  const d = +(s.last_value - s.prev).toFixed(1);
  if (Math.abs(d) < 0.05) return `<span class="trend flat">• estable</span>`;
  return d > 0 ? `<span class="trend up">▲ +${d}</span>` : `<span class="trend down">▼ ${d}</span>`;
}
function renderRoot(){
  const byZone = {}; const noZone = [];
  stateData.forEach(d => {
    if (d.zone_id && zones[d.zone_id]) (byZone[d.zone_id] ||= []).push(d);
    else noZone.push(d);
  });
  let html = "";
  for (const zid in zones){ const devs = byZone[zid] || []; if (devs.length) html += zoneHtml(zones[zid], devs); }
  if (noZone.length) html += zoneHtml(null, noZone);
  document.getElementById("root").innerHTML = html || "Sin equipos todavía — enciende una placa.";
}
function zoneHtml(z, devs){
  const head = z
    ? `<span class="chip">🌿 <b>${z.crop||"cultivo"}</b></span>
       <span class="chip">riega si &lt;<b>${z.soil_min_pct}%</b> · para si &gt;<b>${z.soil_max_pct}%</b></span>
       <button class="primary" onclick="irrigate(${z.zone_id},10)">💧 Riego</button>
       <button class="stop" onclick="irrigate(${z.zone_id},0)">⏹ Stop</button>`
    : `<span class="chip">sin zona asignada</span>`;
  return `<div class="zone"><div class="zonehead"><h3>${z ? z.name : "Equipos sin zona"}</h3>${head}</div>
    ${devs.map(devHtml).join("")}</div>`;
}
function devHtml(d){
  return `<div style="margin-top:.8rem">
    <div class="zonehead" style="margin-bottom:.3rem">
      <b>${d.name || d.device_id}</b>
      <span class="badge ${d.status}">● ${d.status}</span>
      <small>${d.device_id} · ${ago(d.last_seen)}</small>
      <button onclick="rmDev('${d.device_id}')" style="margin-left:auto">Quitar</button>
    </div>
    <div class="grid">${d.sensors.map(s => sensorHtml(d, s)).join("")}</div>
  </div>`;
}
function sensorHtml(d, s){
  const zone = zones[d.zone_id];
  const st = sensorState(s, zone);
  const color = st.cls ? COL[st.cls] : COL.def;
  return `<div class="card ${st.cls||""}" style="--accent:${color}"
           onclick="openChart('${d.device_id}','${s.sensor_id}','${s.type||s.sensor_id}','${s.unit||""}')"
           data-dev="${d.device_id}" data-sid="${s.sensor_id}" data-color="${color}">
    <div class="cardtop"><span class="icon">${ICON[s.type]||"·"}</span>
      <span class="lbl">${NAME[s.type]||s.type||s.sensor_id}${st.tag ? `<b>${st.tag}</b>` : ""}</span></div>
    <div class="val">${s.last_value ?? "--"}<small>${s.unit||""}</small></div>
    <div class="meta">${trend(s)}<span class="tchip">${ago(s.last_seen)}</span></div>
    ${gaugeHtml(s, zone)}
    <div class="spark"><canvas id="sp-${d.device_id}-${s.sensor_id}"></canvas></div>
    <div class="cardfoot"><span></span>
      <button onclick="event.stopPropagation();rmSens('${d.device_id}','${s.sensor_id}')">Quitar</button></div>
  </div>`;
}
// ---- sparklines 24h (agg horaria, cada 60s) ----
async function sparklines(){
  for (const c of document.querySelectorAll(".card[data-dev]")){
    const dev = c.dataset.dev, sid = c.dataset.sid, color = c.dataset.color || "#38bdf8";
    try{
      const agg = await api(`/api/v2/history-agg?device_id=${dev}&sensor_id=${sid}&bucket=1h&hours=24`);
      const cv = document.getElementById(`sp-${dev}-${sid}`);
      if (!cv || !agg.length) continue;
      const old = Chart.getChart(cv); if(old) old.destroy();
      new Chart(cv, {type:"line", data:{labels:agg.map(()=>""), datasets:[{
        data: agg.map(x=>x.avg), borderColor:color, borderWidth:1.5, pointRadius:0, tension:.4, fill:true,
        backgroundColor: color+"22"}]},
        options:{responsive:true, animation:false, plugins:{legend:{display:false}},
                 scales:{x:{display:false}, y:{display:false}}}});
    }catch(e){}
  }
}
// ---- alertas ----
function ensureAdm(){ return sessionStorage.ADM || (sessionStorage.ADM = prompt("X-Admin-Key (gobernar riegos/alertas):") || ""); }
async function loadAlerts(){
  try{
    const list = await api("/api/v2/alerts");
    const box = document.getElementById("alerts");
    if (!list.length) { box.innerHTML = "<small>Todo en orden — sin alertas abiertas</small>"; return; }
    box.innerHTML = list.map(a =>
      `<div class="alert ${a.severity}"><b>${a.kind}</b> · ${a.message}
       ${sessionStorage.ADM ? `<button onclick="ack(${a.alert_id})">Ack</button>` : ""}</div>`).join("");
  }catch(e){ document.getElementById("alerts").innerHTML = "alertas: " + e.message; }
}
async function ack(id){
  const r = await fetch(`/api/v2/alerts/${id}/ack`, {method:"POST", headers:{"X-Admin-Key":ensureAdm()}});
  r.ok ? toast("Alerta reconocida") : toast("No se pudo ackear", false);
  loadAlerts();
}
// ---- acciones ----
async function irrigate(zid, min){
  const k = ensureAdm(); if(!k) return;
  let body = min > 0 ? {duration_min:min} : {stop:true};
  if (min > 0){ const m = prompt("Minutos de riego (max 120):", "10"); if(!m) return; body = {duration_min: Math.min(120, parseInt(m)||10)}; }
  const r = await fetch(`/api/v2/zones/${zid}/irrigate`, {method:"POST",
    headers:{"Content-Type":"application/json","X-Admin-Key":k}, body: JSON.stringify(body)});
  if(!r.ok){ toast("Fallo: " + (await r.text()).slice(0,80), false); }
  else { toast(min > 0 ? `💧 Riego ${body.duration_min} min encolado` : "⏹ Stop encolado"); refresh(); feed(); }
}
async function rmSens(d, s){ if(confirm(`Quitar ${s} de ${d}?`)){ await api(`/api/v2/devices/${d}/sensors/${s}`, {method:"DELETE"}); toast("Sensor dado de baja (baja lógica)"); refresh(); } }
async function rmDev(d){ if(confirm(`Quitar equipo ${d}?`)){ await api(`/api/v2/devices/${d}`, {method:"DELETE"}); toast("Equipo dado de baja"); refresh(); } }
// ---- feed de actividad ----
const TRIG = {rule:"🤖", manual:"✋", offline:"📴", schedule:"⏰"};
async function feed(){
  try{
    const evs = await api("/api/v2/irrigation-events?limit=6");
    document.getElementById("feed").innerHTML = evs.map(e => {
      const done = e.duration_min != null;
      return `<div class="ev"><span>${TRIG[e.trigger]||"•"}</span>
        <b>${e.zone}</b> <span class="pill ${e.trigger}">${e.trigger}</span>
        <span>${done ? `riegó ${e.duration_min.toFixed(0)} min${e.liters ? " · " + e.liters + "L" : ""}` : "riego EN CURSO ⏳"}</span>
        <span class="t">${ago(e.started_at)}</span></div>`;
    }).join("") || "<small>Sin actividad aún</small>";
  }catch(e){ document.getElementById("feed").innerHTML = "feed: " + e.message; }
}
// ---- charts grandes (modal) ----
function mkChart(id, labels, data, label, color){
  const old = Chart.getChart(id); if(old) old.destroy();
  return new Chart(document.getElementById(id), {type:"line",
    data:{labels, datasets:[{label, data, borderColor:color, backgroundColor:color+"33",
      pointRadius:0, tension:.3, fill:true}]},
    options:{plugins:{legend:{display:true, labels:{color:"#e2e8f0", font:{size:10}}}},
      scales:{x:{ticks:{color:"#94a3b8", maxTicksLimit:8}}, y:{ticks:{color:"#94a3b8"}}}}});
}
async function openChart(dev, sid, type, unit, rango){
  const ov = document.getElementById("overlay");
  ov.style.display = "flex";
  document.getElementById("charttitle").textContent = `${NAME[type]||type} · ${dev}/${sid} (${unit||""})`;
  document.getElementById("rangos").innerHTML = ["24h","7d","1a"].map(r =>
    `<button onclick="openChart('${dev}','${sid}','${type}','${unit}','${r}')">${r}</button>`).join(" ");
  const horas = {"24h":24, "7d":168, "1a":8760}[rango || "7d"] || 168;
  try {
    const agg = await api(`/api/v2/history-agg?device_id=${dev}&sensor_id=${sid}&bucket=1h&hours=${horas}`);
    const lb = agg.map(x => new Date(x.ts).toLocaleString("es", {day:"2-digit", month:"2-digit", hour:"2-digit"}));
    mkChart("ch1", lb, agg.map(x => x.avg), "promedio horario", "#38bdf8");
    if (agg.length && agg[0].min !== undefined) mkChart("ch2", lb, agg.map(x => x.min), "mínimo", "#f87171");
  } catch(e) { toast("sin datos agregados", false); }
  try {
    const raw = await api(`/api/v2/history?device_id=${dev}&sensor_id=${sid}&limit=200`);
    const lb2 = raw.map(x => new Date(x.ts).toLocaleTimeString("es", {hour:"2-digit", minute:"2-digit"})).reverse();
    mkChart("ch3", lb2, raw.map(x => x.value).reverse(), "últimos puntos crudos", "#4ade80");
  } catch(e) {}
}
// ---- arranque ----
refresh(); sparklines(); feed();
setInterval(refresh, 5000);
setInterval(sparklines, 60000);
setInterval(feed, 15000);
</script>
<div id="overlay" onclick="if(event.target===this)this.style.display='none'">
  <div id="chartbox"><h3 id="charttitle"></h3> <small><span id="rangos"></span>
    <button onclick="document.getElementById('overlay').style.display='none'">Cerrar</button></small>
    <canvas id="ch1" class="chartbig"></canvas><canvas id="ch2" class="chartbig"></canvas><canvas id="ch3" class="chartbig"></canvas>
  </div>
</div>
</body></html>"""


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
