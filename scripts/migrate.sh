#!/bin/bash
# Aplica el esquema completo: primero db/init/ y despues db/migrations/.
# TODO lleva IF NOT EXISTS, asi que se puede correr cuantas veces se quiera.
#
# db/init/ solo lo ejecuta PostgreSQL cuando el volumen esta vacio; este script
# es el que mantiene el esquema al dia en el resto de despliegues (y tras un
# restore, que borra el esquema public).
set -euo pipefail
cd "$(dirname "$0")/.."

USER="${POSTGRES_USER:-iot}"
DB="${POSTGRES_DB:-iot}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }

for archivo in db/init/*.sql db/migrations/*.sql; do
  [[ -e "$archivo" ]] || continue
  log "Aplicando $archivo"
  docker exec -i db-postgres psql -U "$USER" -d "$DB" -q -v ON_ERROR_STOP=1 < "$archivo"
  echo -e "\033[1;32m  OK $archivo\033[0m"
done

log "Estado del esquema"
docker exec db-postgres psql -U "$USER" -d "$DB" -c "\dt+"