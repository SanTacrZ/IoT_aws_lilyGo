#!/bin/bash
# Despliegue con rollback automatico.
#
#   1. backup de PostgreSQL + Mongo antes de tocar nada
#   2. snapshot de las imagenes actuales -> deploy/previous.lock (por digest)
#   3. build + up de la version nueva (tag APP_VERSION = timestamp)
#   4. healthcheck de todos los servicios; si algo falla, restaura las imagenes
#      anteriores y vuelve a levantarlas.
#
# Los datos NO se pierden en ningun paso: viven en volumenes con nombre
# (postgres-data, mongo-db, grafana) que up/rollback nunca borran.
# Para volver de una version a otra se usa este script o scripts/rollback.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

LOCK_DIR="deploy"
SERVICIOS_APP="iot-agent ai-agent ai-scheduler alert-agent frontend"
SERVICIOS_SALUD="db-postgres orion iot-agent ai-agent ai-scheduler alert-agent frontend"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
ok()  { echo -e "\033[1;32m  OK $*\033[0m"; }
err() { echo -e "\033[1;31m  ERROR $*\033[0m"; }

mkdir -p "$LOCK_DIR"

# Cada build genera un tag propio; asi la version anterior sigue en disco.
export APP_VERSION="${APP_VERSION:-$(date +%Y%m%d-%H%M%S)}"

snapshot_imagenes() {
  local destino="$1"
  docker compose images --format json | python3 -c "
import json, sys
datos = json.load(sys.stdin)
salida = {}
for svc in '''$SERVICIOS_APP'''.split():
    for c in datos:
        if c.get('ContainerName') == svc and c.get('ID', '').startswith('sha256:'):
            salida[svc] = {'id': c['ID'], 'repo': c['Repository'], 'tag': c['Tag']}
json.dump(salida, open('$destino', 'w'), indent=2, sort_keys=True)
"
  [[ -s "$destino" ]] || echo "{}" > "$destino"
}

esperar_salud() {
  local limite=$SECONDS malos=() estado
  while (( SECONDS - limite < HEALTH_TIMEOUT )); do
    malos=()
    for s in $SERVICIOS_SALUD; do
      estado=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$s" 2>/dev/null || echo "ausente")
      case "$estado" in
        healthy|running) ;;
        *) malos+=("$s:$estado") ;;
      esac
    done
    if [[ ${#malos[@]} -eq 0 ]]; then return 0; fi
    sleep 5
  done
  err "Servicios no saludables: ${malos[*]}"
  return 1
}

revertir() {
  err "Healthcheck fallido"
  if [[ ! -s "$LOCK_DIR/previous.lock" ]]; then
    err "Es el primer despliegue con este script: no hay version anterior a la que volver."
    err "Quedan los contenedores de la version nueva; revisa 'docker compose logs' y 'bash scripts/deploy.sh' otra vez tras corregir."
    exit 1
  fi
  log "Revirtiendo automaticamente a la version anterior"
  if bash scripts/rollback.sh; then
    ok "Rollback aplicado, el sistema quedo en la version anterior"
  else
    err "El rollback automatico fallo: revisa 'docker compose ps' a mano"
  fi
  exit 1
}

# --- 1. Backup -------------------------------------------------------------
log "1/5 Backup de datos antes del despliegue"
bash scripts/backup.sh || err "El backup fallo (sigo: los volumenes siguen intactos)"

# --- 2. Snapshot de la version saliente ------------------------------------
log "2/5 Guardando las imagenes actuales para poder revertir"
[[ -s "$LOCK_DIR/images.lock" ]] && cp "$LOCK_DIR/images.lock" "$LOCK_DIR/previous.lock"
snapshot_imagenes "$LOCK_DIR/images.lock"

# --- 3. Desplegar ----------------------------------------------------------
log "3/5 Construyendo y levantando la version $APP_VERSION"
if ! docker compose up -d --build; then
  err "Fallo el build/up"
  revertir
fi

# --- 4. Healthcheck --------------------------------------------------------
log "4/5 Verificando salud de: $SERVICIOS_SALUD"
esperar_salud || revertir

log "4b/5 Comprobando que las APIs responden"

# Un contenedor puede reportar "running" antes de que el proceso atienda HTTP:
# por eso esto reintenta en lugar de hacer un unico curl.
esperar_http() {
  local url="$1" intentos="${2:-20}" espera="$3" i
  for ((i = 1; i <= intentos; i++)); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$espera"
  done
  return 1
}

if ! esperar_http "http://localhost:5000/api/v1/health" 20 3; then
  err "el iot-agent no responde"
  docker compose logs --tail=30 iot-agent || true
  revertir
fi
if ! esperar_http "http://localhost:5050/api/agente/health" 12 3; then
  err "  (aviso) el agente IA no responde; revisa GEMINI_API_KEY y db/migrations/002"
fi

# --- 5. Listo --------------------------------------------------------------
snapshot_imagenes "$LOCK_DIR/images.lock"
ok "Despliegue correcto (version $APP_VERSION)"
ok "Imagenes de esta version en $LOCK_DIR/images.lock"
docker compose ps --format 'table {{.Name}}\t{{.Status}}'