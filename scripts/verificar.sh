#!/bin/bash
# Verificacion completa antes de commitear o desplegar.
#
#   bash scripts/verificar.sh            lint + pruebas + compose + firmware
#   bash scripts/verificar.sh --rapido   sin compilar el firmware (lo usa el hook de git)
#   bash scripts/verificar.sh --e2e      ademas, la prueba de punta a punta contra el stack
#
# Primera vez: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

RAPIDO=0; E2E=0
for arg in "$@"; do
  case "$arg" in
    --rapido) RAPIDO=1 ;;
    --e2e) E2E=1 ;;
    *) echo "Opcion desconocida: $arg" >&2; exit 2 ;;
  esac
done

PY=".venv/bin/python"
[[ -x "$PY" ]] || { echo "Falta .venv: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2; exit 2; }

fallos=()
paso() {
  local nombre="$1"; shift
  echo -e "\n\033[1;34m==> $nombre\033[0m"
  if "$@"; then
    echo -e "\033[1;32m  OK $nombre\033[0m"
  else
    echo -e "\033[1;31m  FALLO $nombre\033[0m"
    fallos+=("$nombre")
  fi
}

paso "Lint de Python (ruff)" .venv/bin/ruff check services tests scripts
paso "Lint de scripts (shellcheck)" shellcheck -S warning scripts/*.sh .githooks/pre-commit
paso "Pruebas unitarias y de contrato" "$PY" -m pytest -q
paso "docker-compose.yml valido" docker compose config -q

if (( ! RAPIDO )); then
  PIO="$(command -v pio || echo "$HOME/.platformio/penv/bin/pio")"
  if [[ -x "$PIO" ]]; then
    paso "Firmware compila" bash -c "cd firmware/lilygo_planta && '$PIO' run -s"
  else
    echo "  (aviso) PlatformIO no instalado: no se compila el firmware"
  fi
fi

if (( E2E )); then
  paso "Prueba de punta a punta" "$PY" -m pytest -m e2e -q
fi

if (( ${#fallos[@]} )); then
  echo -e "\n\033[1;31mFallaron: ${fallos[*]}\033[0m"
  exit 1
fi
echo -e "\n\033[1;32mTodo verificado.\033[0m"
