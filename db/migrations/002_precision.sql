-- ============================================================
-- AgroSense 002: agricultura de precision (zonas, cultivos,
-- actuadores, comandos, reglas de riego, alertas, usuarios).
-- Requiere: 001 implícito (app_v2 crea devices_v2/sensors_v2/measurements_v2).
-- Opcional TimescaleDB: ejecutar bloque final si la extensión existe.
-- ============================================================

-- Comunidades / parcelas
CREATE TABLE IF NOT EXISTS farms (
  farm_id   BIGSERIAL PRIMARY KEY,
  name      TEXT NOT NULL,
  location  TEXT,
  lat       DOUBLE PRECISION,
  lon       DOUBLE PRECISION,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Zonas de riego dentro de una parcela (cada zona = cultivo con sus umbrales)
CREATE TABLE IF NOT EXISTS zones (
  zone_id    BIGSERIAL PRIMARY KEY,
  farm_id    BIGINT NOT NULL REFERENCES farms(farm_id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  crop       TEXT NOT NULL DEFAULT '',
  area_m2    DOUBLE PRECISION,
  soil_min_pct DOUBLE PRECISION NOT NULL DEFAULT 30,  -- umbral de riego
  soil_max_pct DOUBLE PRECISION NOT NULL DEFAULT 60,  -- objetivo tras riego
  hysteresis_pct DOUBLE PRECISION NOT NULL DEFAULT 3,  -- anti rebote on/off
  max_irrigation_min INT NOT NULL DEFAULT 20,          -- tope seguridad
  enabled    BOOLEAN NOT NULL DEFAULT TRUE,
  UNIQUE (farm_id, name)
);

-- Un dispositivo pertenece a una zona
ALTER TABLE devices_v2 ADD COLUMN IF NOT EXISTS zone_id BIGINT REFERENCES zones(zone_id);

-- Actuadores (bomba, electroválvulas) asociados a dispositivo+zona
CREATE TABLE IF NOT EXISTS actuators (
  actuator_id BIGSERIAL PRIMARY KEY,
  device_id   TEXT NOT NULL REFERENCES devices_v2(device_id) ON DELETE CASCADE,
  zone_id     BIGINT REFERENCES zones(zone_id),
  kind        TEXT NOT NULL CHECK (kind IN ('pump','valve','fan','other')),
  label       TEXT NOT NULL DEFAULT '',
  pin         INT,
  state       TEXT NOT NULL DEFAULT 'off' CHECK (state IN ('on','off','auto')),
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  last_seen   TIMESTAMPTZ,
  UNIQUE (device_id, label)
);

-- Cola de comandos: backend escribe, ESP32 consume (polling) y reporta
CREATE TABLE IF NOT EXISTS commands (
  cmd_id     BIGSERIAL PRIMARY KEY,
  actuator_id BIGINT NOT NULL REFERENCES actuators(actuator_id) ON DELETE CASCADE,
  action     TEXT NOT NULL CHECK (action IN ('on','off','auto')),
  payload    JSONB NOT NULL DEFAULT '{}',   -- ej: {"duration_min": 10}
  status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','done','failed','expired')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at TIMESTAMPTZ,
  done_at    TIMESTAMPTZ,
  result     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_pending ON commands (actuator_id, status) WHERE status = 'pending';

-- Auditoría de riegos (cuánta agua, cuándo, por qué regla)
CREATE TABLE IF NOT EXISTS irrigation_events (
  event_id   BIGSERIAL PRIMARY KEY,
  zone_id    BIGINT NOT NULL REFERENCES zones(zone_id) ON DELETE CASCADE,
  actuator_id BIGINT REFERENCES actuators(actuator_id),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at   TIMESTAMPTZ,
  duration_min DOUBLE PRECISION,
  liters     DOUBLE PRECISION,          -- de caudalímetro si existe
  trigger    TEXT NOT NULL DEFAULT 'rule' CHECK (trigger IN ('rule','manual','schedule')),
  rule_id    BIGINT,
  notes      TEXT
);

-- Reglas de automatización por zona (motor de reglas las evalúa)
CREATE TABLE IF NOT EXISTS rules (
  rule_id    BIGSERIAL PRIMARY KEY,
  zone_id    BIGINT NOT NULL REFERENCES zones(zone_id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  sensor_type TEXT NOT NULL DEFAULT 'soil',  -- tipo de sensor que dispara
  condition  TEXT NOT NULL CHECK (condition IN ('below','above','outside')),
  threshold  DOUBLE PRECISION NOT NULL,
  action     TEXT NOT NULL,                  -- 'irrigate', 'alert', 'irrigate+alert'
  enabled    BOOLEAN NOT NULL DEFAULT TRUE,
  cooldown_min INT NOT NULL DEFAULT 30,      -- no repetir antes de X min
  last_fired TIMESTAMPTZ
);

-- Alertas (SNS + visible en dashboard)
CREATE TABLE IF NOT EXISTS alerts (
  alert_id   BIGSERIAL PRIMARY KEY,
  zone_id    BIGINT REFERENCES zones(zone_id) ON DELETE CASCADE,
  device_id  TEXT,
  severity   TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warn','critical')),
  kind       TEXT NOT NULL,                  -- 'soil_dry','sensor_down','battery_low','irrigation_stuck'
  message    TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  acked_at   TIMESTAMPTZ                     -- ack comunitario
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts (created_at DESC) WHERE acked_at IS NULL;

-- Usuarios y roles comunitarios
CREATE TABLE IF NOT EXISTS users (
  user_id    BIGSERIAL PRIMARY KEY,
  email      TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL DEFAULT '',
  role       TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('viewer','farmer','admin')),
  pass_hash  TEXT NOT NULL DEFAULT '',       -- argon2/bcrypt, fase auth
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS farm_members (
  farm_id BIGINT NOT NULL REFERENCES farms(farm_id) ON DELETE CASCADE,
  user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  PRIMARY KEY (farm_id, user_id)
);

-- ============================================================
-- TimescaleDB (opcional pero recomendado en RDS fase 4):
-- CREATE EXTENSION IF NOT EXISTS timescaledb;
-- SELECT create_hypertable('measurements_v2', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
-- ALTER TABLE measurements_v2 SET (timescaledb.compress);
-- SELECT add_compression_policy('measurements_v2', INTERVAL '7 days');
-- SELECT add_retention_policy('measurements_v2', INTERVAL '180 days');
-- ============================================================
