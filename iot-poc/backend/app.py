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


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8000)
else:  # gunicorn: inicializar esquema al arrancar el worker
    try:
        init_db()
    except Exception as e:
        log.warning("init_db diferido: %s", e)
