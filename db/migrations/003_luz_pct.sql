-- 003_luz_pct.sql — indice de luz 0-100%.
--
-- El AP3216C se topa en 20.661 lux (Range 1, datasheet rev 0.86) y el sol de
-- mediodia ecuatorial ronda los 100.000 lux, asi que un porcentaje sobre lux
-- reales no seria fiel. El firmware envia ademas "luz_pct": un indice relativo
-- al rango que alcanza el sensor en la instalacion, con 0% en el extremo oscuro
-- y 100% en el brillante. Se recalibra en caliente desde el propio nodo con:
--     guardar_luz <lux_100pct> <lux_0pct>
-- Los endpoints viven en la NVS del nodo, no aqui: esta columna solo lo refleja.
--
-- CREATE OR REPLACE VIEW no admite insertar columnas en medio, y la vista no
-- tiene dependencias (verificado en pg_depend), asi que se recrea dentro de una
-- transaccion: para el resto de sesiones la troca es atomica.

BEGIN;

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
    (payload->>'satelites')::integer                     AS satelites
FROM lecturas;

COMMIT;