#!/bin/bash
# Restaura un backup hecho con backup.sh.
#   bash scripts/restore.sh backups/AAAAmmdd-HHMMSS
#
# IMPORTANTE: esto SI borra los datos actuales de las tablas de la app para poner
# los del backup. Los volumenes (mongo, grafana) y la extension timescaledb no se
# tocan; el esquema se reconstruye con db/init + db/migrations.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="${1:?Uso: bash scripts/restore.sh backups/AAAAmmdd-HHMMSS}"
[[ -f "$SRC/lecturas.csv.gz" ]] || { echo "No existe $SRC/lecturas.csv.gz" >&2; exit 1; }

USER="${POSTGRES_USER:-iot}"
DB="${POSTGRES_DB:-iot}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
ok()  { echo -e "\033[1;32m  OK $*\033[0m"; }

log "1/6 Parando lo que escribe en la base de datos"
docker compose stop ai-scheduler ai-agent alert-agent iot-agent frontend

log "2/6 Borrando los datos actuales (solo objetos de la app)"
docker exec -i db-postgres psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
DROP MATERIALIZED VIEW IF EXISTS lecturas_planta_1min CASCADE;
DROP VIEW IF EXISTS lecturas_planta;
DROP TABLE IF EXISTS notificaciones;
DROP TABLE IF EXISTS alertas;
DROP TABLE IF EXISTS analisis_ia;
DROP TABLE IF EXISTS lecturas;
SQL

log "3/6 Reconstruyendo el esquema (db/init + db/migrations)"
bash scripts/migrate.sh >/dev/null
ok "esquema listo"

log "4/6 Cargando lecturas desde el CSV"
gunzip -c "$SRC/lecturas.csv.gz" | docker exec -i db-postgres psql -U "$USER" -d "$DB" -q -v ON_ERROR_STOP=1 \
  -c "\copy lecturas FROM STDIN WITH (FORMAT csv)"

if [[ -f "$SRC/analisis_ia.csv.gz" ]]; then
  log "5/6 Cargando analisis del agente IA"
  gunzip -c "$SRC/analisis_ia.csv.gz" | docker exec -i db-postgres psql -U "$USER" -d "$DB" -q -v ON_ERROR_STOP=1 \
    -c "\copy analisis_ia FROM STDIN WITH (FORMAT csv)"
  # Los ids vienen explicitos en el CSV: hay que re-sincronizar la secuencia.
  docker exec db-postgres psql -U "$USER" -d "$DB" -q -c \
    "SELECT setval(pg_get_serial_sequence('analisis_ia','id'), GREATEST((SELECT COALESCE(MAX(id),1) FROM analisis_ia), 1))"
fi

# Alertas y su bitacora (backups anteriores a 004_alertas no las traen).
for tabla in alertas notificaciones; do
  if [[ -s "$SRC/$tabla.csv.gz" ]]; then
    gunzip -c "$SRC/$tabla.csv.gz" | docker exec -i db-postgres psql -U "$USER" -d "$DB" -q -v ON_ERROR_STOP=1 \
      -c "\copy $tabla FROM STDIN WITH (FORMAT csv)"
    docker exec db-postgres psql -U "$USER" -d "$DB" -q -c \
      "SELECT setval(pg_get_serial_sequence('$tabla','id'), GREATEST((SELECT COALESCE(MAX(id),1) FROM $tabla), 1))"
  fi
done

docker exec db-postgres psql -U "$USER" -d "$DB" -t -c \
  "SELECT '  lecturas=' || count(*) FROM lecturas;" -c "SELECT '  analisis_ia=' || count(*) FROM analisis_ia;"

log "6/6 Levantando los servicios de la aplicacion"
docker compose up -d ai-agent ai-scheduler alert-agent iot-agent frontend

echo -e "\n\033[1;32m  Restaurado desde $SRC\033[0m"
docker compose ps --format 'table {{.Name}}\t{{.Status}}'