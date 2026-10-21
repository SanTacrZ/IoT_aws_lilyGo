#!/usr/bin/env python3
"""Alta y baja de nodos con credenciales propias (DEVICE_CREDENTIALS).

    python3 scripts/nuevo_nodo.py zona-norte          # alta: clave + secreto nuevos
    python3 scripts/nuevo_nodo.py --revocar zona-norte

El alta agrega "id:api_key:hmac_secret" a DEVICE_CREDENTIALS en .env y escribe
firmware/lilygo_planta/nodos/<id>/secrets.h (Wi-Fi copiado del secrets.h actual)
listo para copiar a src/ y flashear. Nunca imprime los secretos.
Despues: docker compose up -d iot-agent  (relee .env).
"""
import argparse
import os
import pathlib
import re
import secrets
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
ENV = RAIZ / ".env"
FIRMWARE = RAIZ / "firmware/lilygo_planta"


def leer_creds(texto):
    m = re.search(r"^DEVICE_CREDENTIALS=(.*)$", texto, re.M)
    return [c for c in (m.group(1).split(",") if m else []) if c.strip()]


def escribir_creds(texto, creds):
    linea = "DEVICE_CREDENTIALS=" + ",".join(creds)
    if re.search(r"^DEVICE_CREDENTIALS=", texto, re.M):
        return re.sub(r"^DEVICE_CREDENTIALS=.*$", lambda _: linea, texto, flags=re.M)
    return texto.rstrip("\n") + "\n" + linea + "\n"


def guardar_privado(ruta, contenido):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(contenido)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dispositivo")
    ap.add_argument("--revocar", action="store_true", help="quita el nodo de DEVICE_CREDENTIALS")
    args = ap.parse_args()

    disp = args.dispositivo
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", disp):
        sys.exit("El id solo admite minusculas, numeros y guiones (ej. zona-norte-01).")
    texto = ENV.read_text()
    creds = leer_creds(texto)
    existentes = {c.split(":")[0] for c in creds}

    if args.revocar:
        if disp not in existentes:
            sys.exit(f"{disp} no esta en DEVICE_CREDENTIALS.")
        guardar_privado(ENV, escribir_creds(texto, [c for c in creds if c.split(":")[0] != disp]))
        print(f"Revocado {disp}. Aplica con: docker compose up -d iot-agent")
        return

    if disp in existentes:
        sys.exit(f"{disp} ya existe. Para rotar su clave: --revocar y alta de nuevo.")
    api_key, secreto = secrets.token_hex(16), secrets.token_hex(32)
    guardar_privado(ENV, escribir_creds(texto, creds + [f"{disp}:{api_key}:{secreto}"]))

    wifi = ""
    actual = FIRMWARE / "src/secrets.h"
    if actual.exists():
        wifi = "\n".join(l for l in actual.read_text().splitlines()
                         if l.startswith(("#define WIFI_SSID", "#define WIFI_PASSWORD")))
    destino = FIRMWARE / "nodos" / disp / "secrets.h"
    guardar_privado(destino, f"""/* Generado por scripts/nuevo_nodo.py para {disp}. NO subir a git. */
#pragma once

{wifi or '#define WIFI_SSID      "NOMBRE_DE_TU_WIFI_2_4_GHZ"\n#define WIFI_PASSWORD  "CONTRASENA_DE_TU_WIFI"'}
#define DEVICE_ID_NODO "{disp}"
#define DEVICE_API_KEY "{api_key}"
#define HMAC_SECRET    "{secreto}"
""")
    print(f"Nodo {disp} dado de alta en .env (DEVICE_CREDENTIALS).")
    print(f"secrets.h del nodo: {destino.relative_to(RAIZ)}")
    print("Siguiente:")
    print("  docker compose up -d iot-agent")
    print(f"  cp {destino.relative_to(RAIZ)} firmware/lilygo_planta/src/secrets.h && "
          "(cd firmware/lilygo_planta && pio run -t upload)")


if __name__ == "__main__":
    main()
