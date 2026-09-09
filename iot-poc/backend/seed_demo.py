"""Seed de desarrollo: extension Timescale, migracion 002 y datos demo.
Idempotente. Uso: docker compose -f docker-compose.dev.yml run --rm seed
"""
import json
import os
import time
import random

import psycopg2

def db():
    return psycopg2.connect(host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
                            dbname=os.getenv("DB_NAME", "iot"), user=os.getenv("DB_USER", "iot"),
                            password=os.getenv("DB_PASSWORD", "iot"), connect_timeout=5)

def wait_db():
    for _ in range(30):
        try:
            with db() as c, c.cursor() as cur:
                cur.execute("SELECT 1")
            return
        except Exception:
            time.sleep(2)
    raise SystemExit("DB no disponible")

def main():
    wait_db()
    with db() as conn, conn.cursor() as cur:
        # 1) TimescaleDB (si la imagen lo trae; en pg vanilla esto falla y seguimos)
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        except Exception as e:
            print("timescaledb no disponible:", e)

        # 2) Tablas v2 (mismo DDL que init_db de app_v2)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS devices_v2 (
          device_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', fw TEXT NOT NULL DEFAULT '',
          enabled BOOLEAN NOT NULL DEFAULT TRUE, last_seen TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS sensors_v2 (
          device_id TEXT NOT NULL REFERENCES devices_v2(device_id) ON DELETE CASCADE,
          sensor_id TEXT NOT NULL, type TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '',
          enabled BOOLEAN NOT NULL DEFAULT TRUE, last_value DOUBLE PRECISION, last_seen TIMESTAMPTZ,
          PRIMARY KEY (device_id, sensor_id));
        CREATE TABLE IF NOT EXISTS measurements_v2 (
          id BIGSERIAL, device_id TEXT NOT NULL, sensor_id TEXT NOT NULL,
          value DOUBLE PRECISION NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (id, ts));
        CREATE INDEX IF NOT EXISTS idx_meas_dev_sens_ts ON measurements_v2 (device_id, sensor_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_sens_dev ON sensors_v2 (device_id);
        """)

        # 3) Migracion de precision (idempotente)
        try:
            cur.execute(open("/app/db/migrations/002_precision.sql").read())
            print("migracion 002 ok")
        except FileNotFoundError:
            print("migracion 002 no encontrada (omitida)")

        # 4) Hipertable + compresion + retencion (best-effort)
        try:
            cur.execute("SELECT create_hypertable('measurements_v2', 'ts', if_not_exists => TRUE, migrate_data => TRUE)")
            cur.execute("ALTER TABLE measurements_v2 SET (timescaledb.compress)")
            cur.execute("SELECT add_compression_policy('measurements_v2', INTERVAL '7 days', if_not_exists => TRUE)")
            cur.execute("SELECT add_retention_policy('measurements_v2', INTERVAL '180 days', if_not_exists => TRUE)")
            print("timescale: hipertable + politicas ok")
        except Exception as e:
            print("timescale politicas omitidas:", e)

        # 5) Datos demo (solo si vacio)
        cur.execute("SELECT count(*) FROM farms")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO farms (name, location, lat, lon) VALUES ('Parcela Demo','Comunidad',0,0) RETURNING farm_id")
            farm = cur.fetchone()[0]
            cur.execute("""INSERT INTO zones (farm_id, name, crop, area_m2, soil_min_pct, soil_max_pct)
                           VALUES (%s,'Zona A - Tomate','tomate',200, 35, 60),
                                  (%s,'Zona B - Maiz','maiz',400, 30, 55) RETURNING zone_id""", (farm, farm))
            zones = [r[0] for r in cur.fetchall()]
            # asegurar que exista la placa demo antes del FK de actuators
            cur.execute("""INSERT INTO devices_v2 (device_id, name, fw)
                           VALUES ('lilygo-01', 'Equipo lilygo-01', 'v2-dev')
                           ON CONFLICT (device_id) DO NOTHING""")
            cur.execute("UPDATE devices_v2 SET zone_id=%s WHERE device_id='lilygo-01'", (zones[0],))
            cur.execute("""INSERT INTO actuators (device_id, zone_id, kind, label, pin)
                           VALUES ('lilygo-01', %s, 'pump', 'bomba1', 26)""", (zones[0],))
            cur.execute("""INSERT INTO rules (zone_id, name, sensor_type, condition, threshold, action, cooldown_min)
                           VALUES (%s,'riego nocturno','soil','below',35,'irrigate+alert',60),
                                  (%s,'alerta seca','soil','below',25,'alert',30)""", (zones[0], zones[0]))
            print("seed demo: farm/zonas/reglas/actuador creados")
        else:
            print("seed: datos ya existen")

    print("seed completo")

if __name__ == "__main__":
    main()
