"""AgroSense - Motor de reglas de riego de precision.

Evaluado cada minuto (EventBridge/Lambda en AWS, o hilo APScheduler en local).
Lee docs/AUTOMATION.md para el diseno completo.

Requiere tablas de db/migrations/002_precision.sql.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

try:
    import psycopg2
except ImportError:  # Lambda: psycopg2-binary va empaquetado aparte
    psycopg2 = None

log = logging.getLogger("rule-engine")
logging.basicConfig(level=logging.INFO)

STALE_AFTER_S = int(os.getenv("STALE_AFTER_S_V2", "900"))
MAX_STUCK_MIN = int(os.getenv("MAX_STUCK_MIN", "30"))
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "")  # opcional en local

SEVERITY_KINDS = {"soil_dry": "warn", "sensor_down": "critical",
                  "battery_low": "warn", "irrigation_stuck": "critical",
                  "soil_flood": "warn"}

SNS = None
def sns():
    global SNS
    if SNS is None and SNS_TOPIC_ARN:
        SNS = boto3.client("sns")
    return SNS

def db():
    cfg = {"host": os.getenv("DB_HOST", "db"), "port": int(os.getenv("DB_PORT", "5432")),
           "dbname": os.getenv("DB_NAME", "iot"), "user": os.getenv("DB_USER", "iot"),
           "password": os.getenv("DB_PASSWORD", "iot")}
    return psycopg2.connect(connect_timeout=5, **cfg)

def push_alert(cur, zone_id, device_id, kind, message):
    cur.execute("INSERT INTO alerts (zone_id, device_id, severity, kind, message) "
                "VALUES (%s,%s,%s,%s,%s)",
                (zone_id, device_id, SEVERITY_KINDS.get(kind, "info"), kind, message))
    if sns():
        try:
            sns().publish(TopicArn=SNS_TOPIC_ARN,
                          Subject=f"[AgroSense] {kind}",
                          Message=message)
        except Exception as e:
            log.warning("sns fallo: %s", e)

def last_soil(cur, zone_id):
    """Ultimo valor de suelo de la zona + edad en segundos."""
    cur.execute("""
        SELECT m.value, EXTRACT(EPOCH FROM (now() - m.ts)) AS age_s
        FROM measurements_v2 m
        JOIN sensors_v2 s USING (device_id, sensor_id)
        JOIN devices_v2 d USING (device_id)
        JOIN zones z USING (zone_id)
        WHERE z.zone_id = %s AND s.type = 'soil'
        ORDER BY m.ts DESC LIMIT 1""", (zone_id,))
    r = cur.fetchone()
    return (float(r[0]), float(r[1])) if r else (None, None)

def eval_zone(cur, zone, now):
    (zone_id, name, crop, soil_min, soil_max, hyst, max_min, rules_rows) = zone
    soil, age_s = last_soil(cur, zone_id)

    # 1) Sin dato fresco = no regurar a ciegas
    if soil is None or age_s > STALE_AFTER_S:
        push_alert(cur, zone_id, None, "sensor_down",
                   f"Zona '{name}': sin lectura de suelo fresca ({int(age_s) if age_s else 0}s). Riego pausado.")
        return
    # 2) Bateria baja de la placa de la zona
    cur.execute("""SELECT d.device_id, min(m.value) FROM measurements_v2 m
                   JOIN devices_v2 d USING (device_id)
                   WHERE d.zone_id = %s AND m.ts > now() - interval '1 day'
                     AND EXISTS (SELECT 1 FROM sensors_v2 s
                                 WHERE s.device_id = d.device_id AND s.type = 'battery')
                   GROUP BY d.device_id HAVING min(m.value) < 3.5""", (zone_id,))
    for dev, v in cur.fetchall():
        push_alert(cur, zone_id, dev, "battery_low", f"Zona '{name}': bateria {dev} = {v:.2f}V (<3.5V).")

    # 3) Riego atascado: evento abierto demasiado tiempo
    cur.execute("""SELECT event_id, EXTRACT(EPOCH FROM (now() - started_at))/60
                   FROM irrigation_events WHERE zone_id = %s AND ended_at IS NULL""", (zone_id,))
    for eid, open_min in cur.fetchall():
        if open_min > max_min:
            push_alert(cur, zone_id, None, "irrigation_stuck",
                       f"Zona '{name}': riego abierto {int(open_min)} min (tope {max_min}).")
            _queue_stop(cur, zone_id, "stop-stuck")
            cur.execute("UPDATE irrigation_events SET ended_at=now(), notes='auto-stop stuck' WHERE event_id=%s", (eid,))

    # 4) Reglas con histéresis + cooldown
    for (rule_id, rname, cond, threshold, action, cooldown_min, last_fired) in rules_rows:
        if last_fired and (now - last_fired) < timedelta(minutes=cooldown_min):
            continue
        margin = hyst / 2.0
        hit = (cond == "below" and soil < threshold - margin) or \
              (cond == "above" and soil > threshold + margin)
        if not hit:
            continue
        dur = None
        if "irrigate" in action:
            # duracion estimada: rellenar hasta soil_max (calibrable ~2%/min)
            need_pct = max(0.0, soil_max - soil)
            dur = max(2, min(int((need_pct / 2.0) + 1), max_min))
            cur.execute("""INSERT INTO commands (actuator_id, action, payload)
                           SELECT a.actuator_id, 'on', %s FROM actuators a
                           WHERE a.zone_id = %s AND a.kind = 'pump' AND a.enabled
                           ORDER BY a.actuator_id LIMIT 1
                           RETURNING cmd_id""", (json.dumps({"duration_min": dur}), zone_id))
            cmd = cur.fetchone()
            cur.execute("""INSERT INTO irrigation_events (zone_id, actuator_id, trigger, rule_id, notes)
                           VALUES (%s, (SELECT actuator_id FROM actuators WHERE zone_id=%s AND kind='pump' AND enabled LIMIT 1),
                                  'rule', %s, %s)""",
                        (zone_id, zone_id, rule_id, f"soil={soil:.1f}% < {threshold}%"))
        if "alert" in action:
            push_alert(cur, zone_id, None, "soil_dry",
                       f"Zona '{name}' ({crop}): suelo {soil:.1f}% < {threshold}%. "
                       + (f"Riego programado {dur} min." if dur else "Solo alerta."))
        cur.execute("UPDATE rules SET last_fired = now() WHERE rule_id = %s", (rule_id,))
        log.info("zona=%s regla=%s disparada soil=%.1f dur=%s", name, rname, soil, dur)

def _queue_stop(cur, zone_id, why):
    cur.execute("""INSERT INTO commands (actuator_id, action, payload)
                   SELECT a.actuator_id, 'off', %s FROM actuators a
                   WHERE a.zone_id = %s AND a.kind = 'pump' AND a.enabled""",
                (json.dumps({"why": why}), zone_id))

def run_cycle():
    """Un ciclo completo de evaluacion. Idempotente y seguro de reintentar."""
    now = datetime.now(timezone.utc)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT z.zone_id, z.name, z.crop, z.soil_min_pct, z.soil_max_pct,
                                  z.hysteresis_pct, z.max_irrigation_min
                           FROM zones z WHERE z.enabled""")
            for zone_row in cur.fetchall():
                try:
                    cur.execute("""SELECT rule_id, name, condition, threshold, action,
                                          cooldown_min, last_fired FROM rules
                                   WHERE zone_id = %s AND enabled ORDER BY rule_id""",
                                (zone_row[0],))
                    zone = (*zone_row, cur.fetchall())
                    eval_zone(cur, zone, now)
                except Exception as e:
                    log.exception("zona %s fallo: %s", zone_row[0], e)

if __name__ == "__main__":
    run_cycle()
