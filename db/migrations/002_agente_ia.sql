-- Agente IA (Gemini): historial de analisis sobre el estado del cultivo.
-- Idempotente: se puede volver a aplicar sin romper nada.

CREATE TABLE IF NOT EXISTS analisis_ia (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    entidad           TEXT             NOT NULL,
    estado            TEXT             NOT NULL CHECK (estado IN ('ok', 'atencion', 'critico', 'error')),
    diagnostico       TEXT             NOT NULL DEFAULT '',
    accion_recomendada TEXT            NOT NULL DEFAULT '',
    confianza         DOUBLE PRECISION,
    modelo            TEXT,
    contexto          JSONB            NOT NULL DEFAULT '{}'::jsonb,
    latencia_ms       INTEGER,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS analisis_ia_created_idx ON analisis_ia (created_at DESC);
CREATE INDEX IF NOT EXISTS analisis_ia_entidad_idx ON analisis_ia (entidad, created_at DESC);