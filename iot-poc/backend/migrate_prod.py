"""Migraciones de produccion (se ejecuta como tarea Fargate one-shot via ECS RunTask).
Sin datos demo: solo tablas, MV horaria y pg_cron.
Comando: python -u migrate_prod.py
"""
import os
import sys
import time

sys.path.insert(0, "/app")
os.environ.setdefault("DB_SECRET_ARN", os.getenv("DB_SECRET_ARN", ""))

import app_v2 as backend  # noqa: E402

def main():
    for intento in range(15):  # esperar a que RDS este disponible
        try:
            backend.db().cursor().execute("SELECT 1")
            break
        except Exception as e:
            print(f"esperando DB ({intento+1}): {e}")
            time.sleep(10)
    else:
        raise SystemExit("RDS nunca respondio")

    backend.init_db()
    with backend.db().cursor() as cur:
        mig = open("/app/db/migrations/002_precision.sql").read()
        cur.execute(mig)
        print("migracion 002 ok")
    # RDS gestionado no soporta TimescaleDB: fallback profesional con MV + pg_cron
    try:
        with backend.db().cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            cur.execute("SELECT create_hypertable('measurements_v2', 'ts', if_not_exists => TRUE)")
        print("timescaledb disponible")
        return
    except Exception:
        print("timescaledb no disponible -> MV horaria + pg_cron")
    conn = backend.db()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP MATERIALIZED VIEW IF EXISTS readings_hourly CASCADE")
        cur.execute("""CREATE MATERIALIZED VIEW readings_hourly AS
                       SELECT device_id, sensor_id, date_bin('1 hour', ts, '2000-01-01') AS bucket,
                              avg(value) AS avg_v, min(value) AS min_v,
                              max(value) AS max_v, count(*) AS n
                       FROM measurements_v2 GROUP BY device_id, sensor_id, bucket""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hourly_key ON readings_hourly (device_id, sensor_id, bucket)")
        print("MV readings_hourly creada")
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_cron")
            cur.execute("SELECT cron.schedule('refresh-readings-hourly', '0 * * * *', 'REFRESH MATERIALIZED VIEW CONCURRENTLY readings_hourly')")
            print("pg_cron: refresh horario programado")
        except Exception as e:
            print("pg_cron no disponible (refrescar manual):", e)

if __name__ == "__main__":
    main()
