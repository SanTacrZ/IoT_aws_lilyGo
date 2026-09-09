"""IoT POC backend: Flask + PostgreSQL (RDS) + boto3 (Secrets Manager + S3).
Seguridad: TLS (terminado en nginx/reverse o cert propio) + API-Key + HMAC-SHA256 + anti-replay.
12-factor: toda config por variables de entorno.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
import psycopg2
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iot-poc")

app = Flask(__name__)

# --- Config por entorno (estandar 12-factor) ---
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
DB_SECRET_ARN = os.getenv("DB_SECRET_ARN", "")          # Secrets Manager con {username,password,host,port,dbname}
S3_BACKUP_BUCKET = os.getenv("S3_BACKUP_BUCKET", "")    # backup frio JSONL
DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "")        # API-Key compartida (rotar en prod)
HMAC_SECRET = os.getenv("HMAC_SECRET", "")              # secreto HMAC (rotar en prod)
MAX_SKEW_S = int(os.getenv("MAX_SKEW_S", "300"))        # ventana anti-replay 5 min

_db_conn = None
_secrets_cache: dict = {}


def get_secret_json(arn: str) -> dict:
    """Lee secreto desde Secrets Manager con cache en memoria (boto3)."""
    if arn in _secrets_cache:
        return _secrets_cache[arn]
    sm = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = sm.get_secret_value(SecretId=arn)
    data = json.loads(resp["SecretString"])
    _secrets_cache[arn] = data
    return data


def db_config() -> dict:
    """Prioridad: secreto RDS > variables DB_* (fallback local/docker)."""
    if DB_SECRET_ARN:
        s = get_secret_json(DB_SECRET_ARN)
        return {"host": s["host"], "port": int(s.get("port", 5432)),
                "dbname": s["dbname"], "user": s["username"], "password": s["password"]}
    return {"host": os.getenv("DB_HOST", "db"), "port": int(os.getenv("DB_PORT", "5432")),
            "dbname": os.getenv("DB_NAME", "iot"), "user": os.getenv("DB_USER", "iot"),
            "password": os.getenv("DB_PASSWORD", "iot")}


def db() -> psycopg2.extensions.connection:
    global _db_conn
    if _db_conn is None or _db_conn.closed:
        cfg = db_config()
        _db_conn = psycopg2.connect(connect_timeout=5, **cfg)
        _db_conn.autocommit = True
    return _db_conn


DDL = """
CREATE TABLE IF NOT EXISTS readings (
  id BIGSERIAL PRIMARY KEY,
  device_id TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  temperature_c DOUBLE PRECISION,
  humidity_pct DOUBLE PRECISION,
  soil_moisture_pct DOUBLE PRECISION,
  solar_w_m2 DOUBLE PRECISION,
  battery_v DOUBLE PRECISION,
  raw JSONB
);
CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings (device_id, ts DESC);
"""


def init_db():
    with db().cursor() as cur:
        cur.execute(DDL)
    log.info("DB init ok")


def verify_auth(payload: bytes) -> tuple[bool, str]:
    """API-Key en X-Api-Key + HMAC-SHA256 en X-Signature sobre (timestamp.body)."""
    if not DEVICE_API_KEY or not HMAC_SECRET:
        return False, "server auth no configurado"
    if request.headers.get("X-Api-Key") != DEVICE_API_KEY:
        return False, "api-key invalida"
    ts = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")
    try:
        sk = abs(time.time() - int(ts))
    except ValueError:
        return False, "timestamp invalido"
    if sk > MAX_SKEW_S:
        return False, "timestamp expirado (replay?)"
    expect = hmac.new(HMAC_SECRET.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return False, "firma HMAC invalida"
    return True, "ok"


def s3_backup(entry: dict):
    """Backup frio best-effort a S3 (un objeto/dia, append via put). boto3."""
    if not S3_BACKUP_BUCKET:
        return
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"raw/{entry['device_id']}/{day}.jsonl"
        try:
            obj = s3.get_object(Bucket=S3_BACKUP_BUCKET, Key=key)
            body = obj["Body"].read().decode() + json.dumps(entry) + "\n"
        except ClientError:
            body = json.dumps(entry) + "\n"
        s3.put_object(Bucket=S3_BACKUP_BUCKET, Key=key, Body=body.encode(),
                      ServerSideEncryption="AES256", ContentType="application/x-ndjson")
    except Exception as e:  # backup nunca debe tumbar el ingest
        log.warning("s3 backup fallo: %s", e)


@app.get("/health")
def health():
    try:
        with db().cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ok", db="up")
    except Exception as e:
        return jsonify(status="degraded", db=str(e)), 503


@app.post("/api/v1/readings")
def ingest():
    raw = request.get_data()
    ok, msg = verify_auth(raw)
    if not ok:
        return jsonify(error=msg), 401
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return jsonify(error="JSON invalido"), 400
    # Validacion minima de contrato LilyGo
    for f in ("device_id", "temperature_c", "humidity_pct", "soil_moisture_pct", "solar_w_m2"):
        if f not in data:
            return jsonify(error=f"falta campo {f}"), 422
    entry = {"device_id": str(data["device_id"]), "ts": datetime.now(timezone.utc).isoformat(),
             "temperature_c": data["temperature_c"], "humidity_pct": data["humidity_pct"],
             "soil_moisture_pct": data["soil_moisture_pct"], "solar_w_m2": data["solar_w_m2"],
             "battery_v": data.get("battery_v")}
    with db().cursor() as cur:
        cur.execute(
            "INSERT INTO readings (device_id, temperature_c, humidity_pct,"
            " soil_moisture_pct, solar_w_m2, battery_v, raw) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (entry["device_id"], entry["temperature_c"], entry["humidity_pct"],
             entry["soil_moisture_pct"], entry["solar_w_m2"], entry["battery_v"],
             json.dumps(data)))
    s3_backup({**entry, "raw": data})
    return jsonify(status="stored", ts=entry["ts"]), 201


@app.get("/api/v1/readings")
def latest():
    """Lectura para dashboard/clase. Protegida con la misma API-Key (sin HMAC)."""
    if request.headers.get("X-Api-Key") != DEVICE_API_KEY:
        return jsonify(error="api-key invalida"), 401
    device = request.args.get("device_id", "")
    limit = min(int(request.args.get("limit", 20)), 100)
    with db().cursor() as cur:
        if device:
            cur.execute("SELECT device_id, ts, temperature_c, humidity_pct,"
                        " soil_moisture_pct, solar_w_m2, battery_v FROM readings"
                        " WHERE device_id=%s ORDER BY ts DESC LIMIT %s", (device, limit))
        else:
            cur.execute("SELECT device_id, ts, temperature_c, humidity_pct,"
                        " soil_moisture_pct, solar_w_m2, battery_v FROM readings"
                        " ORDER BY ts DESC LIMIT %s", (limit,))
        cols = [d[0] for d in cur.description]
        return jsonify([dict(zip(cols, r)) for r in cur.fetchall()])


@app.delete("/api/v1/readings/<int:rid>")
def delete_reading(rid: int):
    """CRUD: elimina una lectura por id. Protegida con API-Key."""
    if request.headers.get("X-Api-Key") != DEVICE_API_KEY:
        return jsonify(error="api-key invalida"), 401
    with db().cursor() as cur:
        cur.execute("DELETE FROM readings WHERE id=%s", (rid,))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(status="deleted", id=rid)


@app.put("/api/v1/readings/<int:rid>")
def update_reading(rid: int):
    """CRUD: actualiza campos de una lectura. Requiere auth firmada (igual que ingest)."""
    raw = request.get_data()
    ok, msg = verify_auth(raw)
    if not ok:
        return jsonify(error=msg), 401
    try:
        data = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return jsonify(error="JSON invalido"), 400
    allowed = ("temperature_c", "humidity_pct", "soil_moisture_pct", "solar_w_m2", "battery_v")
    sets = [(f, data[f]) for f in allowed if f in data]
    if not sets:
        return jsonify(error="nada para actualizar"), 422
    clause = ", ".join(f"{f}=%s" for f, _ in sets)
    with db().cursor() as cur:
        cur.execute(f"UPDATE readings SET {clause} WHERE id=%s", (*[v for _, v in sets], rid))
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
    return jsonify(status="updated", id=rid)


@app.get("/dashboard")
def dashboard():
    """Pagina para la clase: ultima muestra + ultimas 20, sin clave (solo lectura)."""
    import html as _h
    with db().cursor() as cur:
        cur.execute("SELECT device_id, ts, temperature_c, humidity_pct,"
                    " soil_moisture_pct, solar_w_m2, battery_v FROM readings"
                    " ORDER BY ts DESC LIMIT 20")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    last = rows[0] if rows else {}
    # Detector de caida: si la ultima muestra es mas vieja que el umbral, placa CAIDA.
    stale_after = int(os.getenv("STALE_AFTER_S", "180"))  # placa envia 1/min -> 3 min sin datos = caida
    down = True
    age_s = None
    try:
        if last.get("ts"):
            _ts = last["ts"]
            if _ts.tzinfo is None:  # psycopg2 puede devolver naive -> asumir UTC
                _ts = _ts.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - _ts).total_seconds()
            down = age_s > stale_after
    except Exception:
        down = True
    if down:
        banner = ('<div class="down">🔴 LilyGo <b>CAÍDO</b> — sin datos hace '
                  f'{_h.escape(str(int(age_s)) + "s" if age_s is not None else "nunca")} '
                  f'(umbral {stale_after}s)</div>')
        cards = "".join(
            f'<div class="card off"><span>{label}</span><b>CAÍDO</b></div>'
            for label in ("🌡 Temperatura", "💧 Humedad", "🌱 Suelo", "☀️ Solar", "🔋 Batería"))
    else:
        banner = (f'<div class="up">🟢 LilyGo <b>EN LÍNEA</b> — último dato hace '
                  f'{int(age_s)}s</div>')
        cards = "".join(
            f'<div class="card"><span>{label}</span><b>{_h.escape(str(last.get(k, "--")))}{unit}</b></div>'
            for label, k, unit in [("🌡 Temperatura", "temperature_c", " °C"),
                                   ("💧 Humedad", "humidity_pct", " %"),
                                   ("🌱 Suelo", "soil_moisture_pct", " %"),
                                   ("☀️ Solar", "solar_w_m2", " W/m²"),
                                   ("🔋 Batería", "battery_v", " V")])
    trs = "".join(
        "<tr><td>{ts}</td><td>{dev}</td><td>{t}</td><td>{h}</td><td>{s}</td><td>{sol}</td><td>{b}</td></tr>".format(
            **{k: _h.escape(str(r.get(k, ""))) for k in
               (("ts", "device_id", "temperature_c", "humidity_pct",
                 "soil_moisture_pct", "solar_w_m2", "battery_v"))} |
            {"ts": _h.escape(str(r.get("ts", ""))), "dev": _h.escape(str(r.get("device_id", ""))),
             "t": r.get("temperature_c", ""), "h": r.get("humidity_pct", ""),
             "s": r.get("soil_moisture_pct", ""), "sol": r.get("solar_w_m2", ""),
             "b": r.get("battery_v", "")})
        for r in rows)
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IoT LilyGo - Monitor</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#0f172a;color:#e2e8f0}}
.cards{{display:flex;gap:.8rem;flex-wrap:wrap;margin:1rem 0}}.card{{flex:1;min-width:150px;background:#1e293b;border-radius:.75rem;padding:1rem;text-align:center}}
.card span{{font-size:.8rem;color:#94a3b8}}.card b{{font-size:1.6rem;display:block;margin-top:.3rem}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}th,td{{text-align:left;padding:.45rem;border-bottom:1px solid #334155}}
.up{{background:#052e16;border:1px solid #16a34a;border-radius:.75rem;padding:.8rem 1rem;margin:.5rem 0}}
.down{{background:#450a0a;border:1px solid #dc2626;border-radius:.75rem;padding:.8rem 1rem;margin:.5rem 0;font-size:1.1rem}}
.card.off b{{color:#f87171}}
small{{color:#94a3b8}}</style></head><body>
<h1>🌱 IoT LilyGo - Monitor <small>(auto-refresh 10s)</small></h1>
{banner}
<p><small>Dispositivo: <b>{_h.escape(str(last.get("device_id", "--")))}</b> · Última muestra: {_h.escape(str(last.get("ts", "--")))}</small></p>
<div class="cards">{cards}</div>
<table><tr><th>Fecha/hora</th><th>Equipo</th><th>Temp °C</th><th>Hum %</th><th>Suelo %</th><th>Solar W/m²</th><th>Bat V</th></tr>
{trs or '<tr><td colspan="7">Sin datos todavía</td></tr>'}</table></body></html>"""


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8000)
else:  # gunicorn: inicializar esquema al arrancar el worker
    try:
        init_db()
    except Exception as e:
        log.warning("init_db diferido: %s", e)
