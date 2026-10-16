-- Historial de lecturas IoT en PostgreSQL + TimescaleDB.
-- Tabla generica: cualquier dispositivo POSTea su payload tal cual y queda en payload::jsonb.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS lecturas (
    time_index  TIMESTAMPTZ NOT NULL,
    entity_id   TEXT        NOT NULL,
    entity_type TEXT        NOT NULL,
    dispositivo TEXT,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb
);

SELECT create_hypertable('lecturas', 'time_index', if_not_exists => TRUE, migrate_data => TRUE);

CREATE INDEX IF NOT EXISTS lecturas_entity_idx ON lecturas (entity_id, time_index DESC);
CREATE INDEX IF NOT EXISTS lecturas_payload_idx ON lecturas USING GIN (payload);

-- Vista tipada para el dashboard / Grafana (contrato del firmware T-Beam).
-- Se recrea en vez de usar CREATE OR REPLACE porque anadir luz_pct en medio de la
-- lista no es valido en Postgres; DROP + CREATE es atomico dentro de la
-- transaccion que envuelve este script.
DROP VIEW IF EXISTS lecturas_planta;

CREATE VIEW lecturas_planta AS
SELECT
    time_index,
    entity_id,
    dispositivo,
    (payload->>'temperatura_aire')::double precision     AS temperatura_aire,
    (payload->>'humedad_aire')::double precision         AS humedad_aire,
    (payload->>'luz_lux')::double precision              AS luz_lux,
    (payload->>'luz_pct')::double precision              AS luz_pct,
    (payload->>'humedad_suelo_pct')::double precision    AS humedad_suelo_pct,
    (payload->>'humedad_suelo_raw')::integer            AS humedad_suelo_raw,
    (payload->>'alerta_seco')::boolean                   AS alerta_seco,
    (payload->>'latitud')::double precision              AS latitud,
    (payload->>'longitud')::double precision             AS longitud,
    (payload->>'altitud')::double precision              AS altitud,
    (payload->>'satelites')::integer                    AS satelites
FROM lecturas;

-- Agregado continuo de 1 minuto para las series largas del dashboard.
CREATE MATERIALIZED VIEW IF NOT EXISTS lecturas_planta_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(INTERVAL '1 minute', time_index) AS bucket,
    entity_id,
    last((payload->>'temperatura_aire')::double precision, time_index)  AS temperatura_aire,
    last((payload->>'humedad_aire')::double precision, time_index)      AS humedad_aire,
    last((payload->>'luz_lux')::double precision, time_index)           AS luz_lux,
    last((payload->>'humedad_suelo_pct')::double precision, time_index) AS humedad_suelo_pct,
    (array_agg((payload->>'alerta_seco')::boolean ORDER BY time_index DESC))[1] AS alerta_seco,
    count(*) AS muestras
FROM lecturas
WHERE payload ? 'temperatura_aire'
GROUP BY bucket, entity_id
WITH NO DATA;

SELECT add_retention_policy('lecturas', INTERVAL '90 days', if_not_exists => TRUE);

-- Sin politica de refresco el agregado se queda vacio: esta version de
-- TimescaleDB no trae real-time aggregates (materialized_only = t).
SELECT add_continuous_aggregate_policy(
    'lecturas_planta_1min',
    start_offset      => INTERVAL '2 days',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);