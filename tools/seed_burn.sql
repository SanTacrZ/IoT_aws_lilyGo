-- ============================================================
-- Datos quemados para demo (historial realista de 7 dias)
-- Ejecutar DESPUES de seed_demo. Idempotente de facto (DB vacia).
-- ============================================================

-- 3 placas, 3 zonas
INSERT INTO zones (farm_id, name, crop, area_m2, soil_min_pct, soil_max_pct)
VALUES (1, 'Zona C - Hortalizas', 'lechuga', 150, 30, 55);
INSERT INTO devices_v2 (device_id, name, fw, zone_id, last_seen) VALUES
  ('lilygo-01', 'Bomba Zona A', 'v3-irrigation', 1, now()),
  ('lilygo-02', 'Bomba Zona B', 'v3-irrigation', 2, now()),
  ('lilygo-03', 'Riego Zona C', 'v3-irrigation', 3, now())
ON CONFLICT (device_id) DO UPDATE SET name=EXCLUDED.name, zone_id=EXCLUDED.zone_id, last_seen=now();

-- Actuadores
INSERT INTO actuators (device_id, zone_id, kind, label, pin) VALUES
  ('lilygo-02', 2, 'pump', 'bomba2', 26),
  ('lilygo-03', 3, 'valve', 'valvulaC', 25)
ON CONFLICT DO NOTHING;

-- Reglas para zonas B y C
INSERT INTO rules (zone_id, name, sensor_type, condition, threshold, action, cooldown_min) VALUES
  (2, 'riego maiz',      'soil', 'below', 30, 'irrigate+alert', 90),
  (3, 'alerta lechugas', 'soil', 'below', 20, 'alert', 45)
ON CONFLICT DO NOTHING;

-- Historial 7 dias con curvas diarias realistas (temp/hum/solar/soil/bateria)
INSERT INTO measurements_v2 (device_id, sensor_id, value, ts)
SELECT dev, sid, CASE sid
    WHEN 'temp1'  THEN round((21 + 7*sin(2*pi()*((h%24)-4)/24.0) + (random()*2-1))::numeric, 1)
    WHEN 'hum1'   THEN round((68 - 14*sin(2*pi()*((h%24)-4)/24.0) + (random()*4-2))::numeric, 1)
    WHEN 'solar1' THEN round((greatest(0, 750*sin(pi()*((h%24)-6)/12.0)) + (random()*20-10))::numeric, 0)
    WHEN 'batt1'  THEN round((4.02 + 0.14*((h%24)/24.0) - 0.002*h + (random()*0.02-0.01))::numeric, 2)
    WHEN 'soil1'  THEN round((42 + 9*sin(2*pi()*h/168.0) + (random()*3-1.5))::numeric, 1)
    WHEN 'soil2'  THEN round((38 + 8*sin(2*pi()*h/168.0) + (random()*3-1.5))::numeric, 1)
  END, now() - make_interval(hours => (168 - h))
FROM generate_series(0, 168) h
CROSS JOIN (VALUES ('lilygo-01'),('lilygo-02'),('lilygo-03')) d(dev)
CROSS JOIN (VALUES ('temp1'),('hum1'),('batt1')) s(sid)
UNION ALL
SELECT dev, sid, CASE sid
    WHEN 'solar1' THEN round((greatest(0, 750*sin(pi()*((h%24)-6)/12.0)))::numeric, 0)
    WHEN 'soil1'  THEN round((42 + 9*sin(2*pi()*h/168.0) + (random()*3-1.5))::numeric, 1)
    WHEN 'soil2'  THEN round((38 + 8*sin(2*pi()*h/168.0) + (random()*3-1.5))::numeric, 1)
  END, now() - make_interval(hours => (168 - h))
FROM generate_series(0, 168) h
CROSS JOIN (VALUES ('lilygo-01'),('lilygo-02'),('lilygo-03')) d(dev)
CROSS JOIN (VALUES ('solar1'),('soil1'),('soil2')) s(sid);

-- Registro de sensores (ultimo valor = instantanea actual)
INSERT INTO sensors_v2 (device_id, sensor_id, type, unit, enabled, last_value, last_seen)
SELECT m.device_id, m.sensor_id,
  CASE m.sensor_id WHEN 'temp1' THEN 'temperature' WHEN 'hum1' THEN 'humidity'
       WHEN 'solar1' THEN 'solar' WHEN 'batt1' THEN 'battery' ELSE 'soil' END,
  CASE m.sensor_id WHEN 'temp1' THEN 'C' WHEN 'hum1' THEN '%' WHEN 'solar1' THEN 'W/m2'
       WHEN 'batt1' THEN 'V' ELSE '%' END,
  TRUE, m.value, m.ts
FROM (SELECT DISTINCT ON (device_id, sensor_id) device_id, sensor_id, value, ts
      FROM measurements_v2 ORDER BY device_id, sensor_id, ts DESC) m
ON CONFLICT (device_id, sensor_id) DO UPDATE SET last_value=EXCLUDED.last_value, last_seen=now();

-- Riegos auditados: manual, regla y offline
INSERT INTO irrigation_events (zone_id, actuator_id, started_at, ended_at, duration_min, liters, trigger, notes) VALUES
  (1, 1, now()-interval '2 days', now()-interval '2 days' + interval '12 minutes', 12, 9.6, 'manual',  'riego manual de mantenimiento'),
  (1, 1, now()-interval '1 day',  now()-interval '1 day'  + interval '15 minutes', 15, 12.0, 'rule',   'soil=31.2% < 35.0%'),
  (2, 2, now()-interval '40 minutes', NULL, NULL, NULL, 'rule', 'riego en curso (regla maiz)'),
  (3, 3, now()-interval '3 hours', now()-interval '3 hours' + interval '8 minutes', 8, 6.2, 'offline', 'reportado al reconectar');

-- Alertas: 2 abiertas + 1 vieja ackeada
INSERT INTO alerts (zone_id, device_id, severity, kind, message, created_at, acked_at) VALUES
  (2, 'lilygo-02', 'warn', 'battery_low', 'Zona ''Zona B - Maiz'': bateria lilygo-02 = 3.31V (<3.5V).', now()-interval '5 hours', NULL),
  (3, NULL, 'warn', 'soil_dry', 'Zona ''Zona C - Hortalizas'' (lechuga): suelo 16.2% < 20%. Solo alerta.', now()-interval '2 hours', NULL),
  (1, NULL, 'critical', 'sensor_down', 'Zona ''Zona A - Tomate'': sin lectura de suelo fresca (1903s). Riego pausado.', now()-interval '2 days', now()-interval '2 days' + interval '3 hours');
