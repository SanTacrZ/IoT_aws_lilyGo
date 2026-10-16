#!/bin/bash
# Backup de los datos con estado: PostgreSQL/TimescaleDB + Mongo (metadatos Orion).
# Los volumenes con nombre NO se borran al redesplegar; esto es la red de
# seguridad para cuando si se quiera reconstruir el stack.
#
#   bash scripts/backup.sh            -> guarda y rota (conserva los ultimos 10)
#   bash scripts/restore.sh <dir>     -> restaura un backup
#
# OJO: los datos de la hypertable `lecturas` se sacan con COPY ... TO STDOUT en
# CSV, no con pg_dump -t: pg_dump (PG16 + TimescaleDB) ve la tabla padre vacia
# porque las filas viven en los chunks y devuelve un COPY sin datos.
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_ROOT="${BACKUP_ROOT:-backups}"
KEEP="${BACKUP_KEEP:-10}"
USER="${POSTGRES_USER:-iot}"
DB="${POSTGRES_DB:-iot}"
TS="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$TS"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
ok()  { echo -e "\033[1;32m  OK $*\033[0m"; }

mkdir -p "$DEST"

volcar() {
  # $1 = tabla ; $2 = columna de orden
  local tabla="$1" orden="$2" destino="$DEST/$1.csv.gz"
  if ! docker exec db-postgres psql -U "$USER" -d "$DB" -t -c \
        "SELECT 1 FROM information_schema.tables WHERE table_name='$tabla'" | grep -q 1; then
    echo "  (aviso) la tabla $tabla no existe todavia"
    : > /tmp/opencode/.vacio 2>/dev/null || true
    printf '' | gzip > "$destino"
    return 0
  fi
  docker exec db-postgres psql -U "$USER" -d "$DB" \
    -c "\copy (SELECT * FROM $tabla ORDER BY $orden) TO STDOUT WITH (FORMAT csv)" \
    | gzip > "$destino"
  ok "$tabla -> $destino ($(zcat "$destino" | wc -l) filas, $(du -h "$destino" | cut -f1))"
}

log "Volcando datos de PostgreSQL/TimescaleDB"
volcar lecturas time_index
volcar analisis_ia id

log "Esquema (referencia) -> $DEST/esquema.sql.gz"
docker exec db-postgres pg_dump -U "$USER" -d "$DB" --schema-only --no-owner \
  | gzip > "$DEST/esquema.sql.gz"
ok "esquema.sql.gz ($(du -h "$DEST/esquema.sql.gz" | cut -f1))"

log "Backup Mongo (metadatos de Orion) -> $DEST/mongo.bson.gz"
if docker exec db-mongo mongodump --quiet --archive --gzip > "$DEST/mongo.bson.gz" 2>/dev/null; then
  ok "mongo.bson.gz ($(du -h "$DEST/mongo.bson.gz" | cut -f1))"
else
  echo "  (aviso) mongodump fallo; el broker reconstruye sus metadatos, no es critico"
fi

cat > "$DEST/INFO.txt" <<EOF
backup      : $TS
host        : $(hostname)
compose     : $(docker compose ps --format '{{.Name}} {{.Image}} {{.Status}}' | tr '\n' '; ')
postgres    : ${POSTGRES_DB:-$DB}
contenido   : lecturas.csv.gz, analisis_ia.csv.gz, esquema.sql.gz, mongo.bson.gz
restaurar   : bash scripts/restore.sh $DEST
EOF

log "Rotando backups (conservando los ultimos $KEEP)"
ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r viejo; do
  echo "  borrando $viejo"
  rm -rf "$viejo"
done

ok "Backup completo en $DEST"