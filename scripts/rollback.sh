#!/bin/bash
# Vuelve a la version anterior de las imagenes de la app.
#
#   bash scripts/rollback.sh              -> usa deploy/previous.lock
#   bash scripts/rollback.sh images.lock  -> usa ese lock
#
# NO borra volumenes: postgres-data, mongo-db y grafana siguen intactos, asi que
# las lecturas y los analisis ya guardados se conservan. Si ademas hubiera que
# volver atras el ESQUEMA, esta scripts/restore.sh backups/<fecha>.
set -euo pipefail
cd "$(dirname "$0")/.."

LOCK="${1:-deploy/previous.lock}"
APP_VERSION="${APP_VERSION:-dev}"

log() { echo -e "\n\033[1;34m==> $*\033[0m"; }
ok()  { echo -e "\033[1;32m  OK $*\033[0m"; }
err() { echo -e "\033[1;31m  ERROR $*\033[0m"; }

[[ -s "$LOCK" ]] || { err "No hay lock file en $LOCK; nada que revertir."; exit 1; }

log "Version a la que se vuelve (digest de $LOCK):"
cat "$LOCK"; echo

# El compose espera el tag $APP_VERSION (por defecto "dev"): se reetiqueta la
# imagen anterior con ese nombre y se recrea solo el contenedor del servicio.
# Los volumenes no se tocan.
resultado=$(python3 - "$LOCK" <<'PY'
import json, subprocess, sys

lock = json.load(open(sys.argv[1]))
tag_destino = __import__("os").environ.get("APP_VERSION", "dev")
fallos = []
for svc, info in sorted(lock.items()):
    imagen_id, repo = info["id"], info["repo"]
    if subprocess.run(["docker", "image", "inspect", imagen_id],
                      capture_output=True).returncode != 0:
        fallos.append(f"{svc}: la imagen {imagen_id[:19]} ya no esta en disco")
        continue
    destino = f"{repo}:{tag_destino}"
    subprocess.run(["docker", "tag", imagen_id, destino], check=True)
    print(f"  {svc}: {imagen_id[:19]} -> {destino}")
if fallos:
    print("FALLOS:", *fallos, sep="\n  ")
    sys.exit(1)
PY
) || { err "No se pudieron preparar las imagenes"; exit 1; }
echo "$resultado"

log " Recreando contenedores (sin tocar volumenes)"
SERVICIOS=$(python3 -c "import json;print(' '.join(json.load(open('$LOCK')).keys()))")
docker compose up -d --no-deps --force-recreate $SERVICIOS

log "Estado tras el rollback"
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
ok "Rollback aplicado. Los datos en los volumenes no se tocaron."