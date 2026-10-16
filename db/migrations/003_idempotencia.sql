-- Idempotencia de la carga: un id por lectura enviado por el dispositivo.
-- Si el nodo reintenta el POST (timeout de Wi-Fi, etc.) no se duplica la fila.
--
-- TimescaleDB exige que todo indice unico de una hypertable incluya la columna
-- de particion, asi que la unicidad es (time_index, entity_id, lectura_id).
-- Por eso el servidor usa como time_index la hora propia del dispositivo
-- cuando es coherente: en un reintento llegan ambos valores iguales y la
-- segunda fila se descarta.
ALTER TABLE lecturas ADD COLUMN IF NOT EXISTS lectura_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS lecturas_idem_idx
    ON lecturas (time_index, entity_id, lectura_id)
    WHERE lectura_id IS NOT NULL;
