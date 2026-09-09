"""Datos quemados de demo para PROD (se ejecuta via ECS RunTask one-shot).
Farm/zonas/reglas/actuador + historial 7 dias con curvas realistas + alertas + riegos.
Idempotente: solo actua si farms esta vacio. Comando: python -u burn_prod.py
"""
import sys

sys.path.insert(0, "/app")
import app_v2 as backend  # noqa: E402

def main():
    backend.init_db()
    with backend.db().cursor() as cur:
        cur.execute("SELECT count(*) FROM farms")
        if cur.fetchone()[0] > 0:
            print("datos ya existen, nada que quemar")
            return
        cur.execute("INSERT INTO farms (name, location) VALUES ('Parcela Demo AWS','Finca comunidad') RETURNING farm_id")
        f = cur.fetchone()[0]
        cur.execute("""INSERT INTO zones (farm_id, name, crop, area_m2, soil_min_pct, soil_max_pct)
                       VALUES (%s,'Zona A - Tomate','tomate',200,35,60), (%s,'Zona B - Maiz','maiz',400,30,55)
                       RETURNING zone_id""", (f, f))
        z1, z2 = [r[0] for r in cur.fetchall()]
        for dev in ("lilygo-01", "lilygo-02"):
            cur.execute("""INSERT INTO devices_v2 (device_id, name, fw) VALUES (%s, %s, 'v4-prod')
                           ON CONFLICT (device_id) DO NOTHING""", (dev, f"Bomba {dev[-2:]}"))
        cur.execute("UPDATE devices_v2 SET zone_id=%s WHERE device_id='lilygo-01'", (z1,))
        cur.execute("UPDATE devices_v2 SET zone_id=%s WHERE device_id='lilygo-02'", (z2,))
        cur.execute("INSERT INTO actuators (device_id, zone_id, kind, label, pin) VALUES ('lilygo-01', %s, 'pump', 'bomba1', 26)", (z1,))
        cur.execute("""INSERT INTO rules (zone_id, name, sensor_type, condition, threshold, action, cooldown_min)
                       VALUES (%s,'riego tomate','soil','below',35,'irrigate+alert',60)""", (z1,))
        cur.execute("""INSERT INTO measurements_v2 (device_id, sensor_id, value, ts)
            SELECT d.dev, s.sid, CASE s.sid
                WHEN 'temp1'  THEN round((22 + 7*sin(2*pi()*((h%24)-4)/24.0))::numeric,1)
                WHEN 'hum1'   THEN round((65 - 12*sin(2*pi()*((h%24)-4)/24.0))::numeric,1)
                WHEN 'solar1' THEN round(greatest(0, 750*sin(pi()*((h%24)-6)/12.0))::numeric,0)
                WHEN 'batt1'  THEN round((4.0 + 0.12*((h%24)/24.0) - 0.001*h)::numeric,2)
                WHEN 'soil1'  THEN round((40 + 8*sin(2*pi()*h/168.0))::numeric,1)
                WHEN 'soil2'  THEN round((37 + 7*sin(2*pi()*h/168.0))::numeric,1)
            END, now() - make_interval(hours => (168 - h))
            FROM generate_series(0,168) h
            CROSS JOIN (VALUES ('lilygo-01'),('lilygo-02')) d(dev)
            CROSS JOIN (VALUES ('temp1'),('hum1'),('solar1'),('batt1'),('soil1'),('soil2')) s(sid)""")
        cur.execute("""INSERT INTO irrigation_events (zone_id, actuator_id, started_at, ended_at, duration_min, liters, trigger, notes)
                       VALUES (%s,1, now()-interval '2 days', now()-interval '2 days' + interval '12 minutes', 12, 9.6, 'manual', 'mantenimiento'),
                              (%s,1, now()-interval '1 day', now()-interval '1 day' + interval '14 minutes', 14, 11.2, 'rule', 'soil=31.4% < 35.0%')""", (z1, z1))
        cur.execute("""INSERT INTO alerts (zone_id, device_id, severity, kind, message, created_at) VALUES
                       (%s,'lilygo-02','warn','battery_low','Zona B: bateria 3.42V (<3.5V).', now()-interval '4 hours')""", (z2,))
    print("datos quemados de produccion listos")

if __name__ == "__main__":
    main()
