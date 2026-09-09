"""Ingestor MQTT (fase 5, primer paso hacia AWS IoT Core).
Consume topic 'agrosense/<device_id>/data' con el MISMO esquema JSON de
POST /api/v2/readings ({device_id, name?, fw?, sensors:[{sensor_id,type,unit,value}]})
y hace el mismo upsert autonomo en Postgres (devices/sensors/measurements).
En AWS: mismo script apuntando a IoT Core (8883 TLS + X.509) con los mismos topics.
"""
import json
import logging
import os
from datetime import datetime, timezone

import psycopg2

try:  # paho-mqtt 2.x
    from paho.mqtt.client import CallbackAPIVersion, Client
except ImportError:  # 1.x
    CallbackAPIVersion = None
    from paho.mqtt.client import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mqtt-ingest")

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC = "agrosense/+/data"
MAX_SENSORS = 32

DDL_DEVICES = """INSERT INTO devices_v2 (device_id, name, fw, enabled, last_seen)
                 VALUES (%s,%s,%s,TRUE,%s) ON CONFLICT (device_id) DO UPDATE SET
                 name=EXCLUDED.name, fw=EXCLUDED.fw, enabled=TRUE, last_seen=EXCLUDED.last_seen"""
DDL_SENSORS = """INSERT INTO sensors_v2 (device_id, sensor_id, type, unit, enabled, last_value, last_seen)
                 VALUES (%s,%s,%s,%s,TRUE,%s,%s) ON CONFLICT (device_id, sensor_id) DO UPDATE SET
                 type=EXCLUDED.type, unit=EXCLUDED.unit, enabled=TRUE,
                 last_value=EXCLUDED.last_value, last_seen=EXCLUDED.last_seen"""
DDL_MEAS = "INSERT INTO measurements_v2 (device_id, sensor_id, value) VALUES (%s,%s,%s)"

_conn = None

def db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(host=os.getenv("DB_HOST", "db"),
                                 port=int(os.getenv("DB_PORT", "5432")),
                                 dbname=os.getenv("DB_NAME", "iot"),
                                 user=os.getenv("DB_USER", "iot"),
                                 password=os.getenv("DB_PASSWORD", "iot"), connect_timeout=5)
        _conn.autocommit = True
    return _conn

def handle(device_id: str, data: dict) -> bool:
    """Upsert autonomo; devuelve True si todo se inserto. Valida como el HTTP."""
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
        return False
    sensors = data.get("sensors", [])
    if not isinstance(sensors, list) or not 1 <= len(sensors) <= MAX_SENSORS:
        return False
    now = datetime.now(timezone.utc)
    with db().cursor() as cur:
        cur.execute(DDL_DEVICES, (device_id, str(data.get("name", device_id))[:64],
                                  str(data.get("fw", ""))[:32], now))
        for s in sensors:
            try:
                sid, val = str(s["sensor_id"])[:64], float(s["value"])
            except (KeyError, TypeError, ValueError):
                return False
            cur.execute(DDL_SENSORS, (device_id, sid, str(s.get("type", ""))[:32],
                                      str(s.get("unit", ""))[:16], val, now))
            cur.execute(DDL_MEAS, (device_id, sid, val))
    return True

def on_connect(client, *args):
    client.subscribe(TOPIC, qos=1)
    log.info("suscrito a %s (qos1)", TOPIC)

def on_message(client, userdata, msg):
    try:
        device_id = msg.topic.split("/")[1]
        data = json.loads(msg.payload.decode())
        if handle(device_id, data):
            log.info("%s: %d sensores ok", device_id, len(data.get("sensors", [])))
        else:
            log.warning("%s: payload invalido, descartado", device_id)
    except Exception:
        log.exception("error procesando %s", msg.topic)

def main():
    if CallbackAPIVersion:
        mqttc = Client(CallbackAPIVersion.VERSION2)
    else:
        mqttc = Client()
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    while True:
        try:
            mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            mqttc.loop_forever(retry_first_connection=True)
        except Exception as e:
            log.warning("mqtt caido (%s), reintento en 5s", e)
            import time; time.sleep(5)

if __name__ == "__main__":
    main()
