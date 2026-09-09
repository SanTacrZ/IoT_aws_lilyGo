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
    g = admin_required()
    if g: return g
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
    g = admin_required()
    if g: return g
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
    g = admin_required()
    if g: return g
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
