-- 004_alertas.sql — alertas con estado (alert-agent).
--
-- Una alerta es un estado, no un mensaje: se abre cuando una condicion se
-- cumple, se recuerda mientras siga abierta y sin reconocer, y se cierra
-- (resuelta_en) cuando la condicion desaparece. El indice unico parcial impide
-- abrir dos veces la misma condicion (clave) mientras siga activa.
-- Idempotente: se puede volver a aplicar sin romper nada.

CREATE TABLE IF NOT EXISTS alertas (
    id              BIGSERIAL PRIMARY KEY,
    clave           TEXT             NOT NULL,
    tipo            TEXT             NOT NULL,
    entidad         TEXT             NOT NULL,
    severidad       TEXT             NOT NULL CHECK (severidad IN ('atencion', 'critico')),
    titulo          TEXT             NOT NULL,
    detalle         TEXT             NOT NULL DEFAULT '',
    accion          TEXT             NOT NULL DEFAULT '',
    valor           DOUBLE PRECISION,
    abierta_en      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    ultima_notif    TIMESTAMPTZ,
    avisos          INTEGER          NOT NULL DEFAULT 0,
    escalada_en     TIMESTAMPTZ,
    reconocida_en   TIMESTAMPTZ,
    reconocida_por  TEXT,
    resuelta_en     TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS alertas_abierta_unica ON alertas (clave) WHERE resuelta_en IS NULL;
CREATE INDEX IF NOT EXISTS alertas_abierta_en_idx ON alertas (abierta_en DESC);

-- Bitacora de todo lo que se envio (o se intento enviar) y por que canal.
CREATE TABLE IF NOT EXISTS notificaciones (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alerta_id   BIGINT      REFERENCES alertas (id) ON DELETE CASCADE,
    canal       TEXT        NOT NULL,
    destino     TEXT        NOT NULL DEFAULT '',
    texto       TEXT        NOT NULL,
    ok          BOOLEAN     NOT NULL,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS notificaciones_created_idx ON notificaciones (created_at DESC);
