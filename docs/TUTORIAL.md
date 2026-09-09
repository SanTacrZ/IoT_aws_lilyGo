# Tutorial paso a paso — del CRUD local al POC en AWS

Rama `ejercicio-local`. Al final entiendes la misma estructura que corre en AWS
(rama `main`), solo cambian el Postgres (contenedor → RDS) y los secretos (env → Secrets Manager).

## Paso 0 — Estructura (léela antes de correr nada)

```
iot-poc/
├── backend/app.py        # API Flask: el CRUD + /dashboard + /health
├── backend/Dockerfile    # imagen del API (gunicorn)
├── backend/tests/        # pytest del contrato
├── deploy/
│   ├── docker-compose.local.yml  # 👈 ESTE ejercicio: api + postgres local
│   ├── docker-compose.yml        # AWS: solo api (la DB es RDS)
│   ├── .env.local.example        # claves demo locales (sí se commitean, no son reales)
│   └── .env.example              # plantilla AWS (placeholders)
└── firmware/
    ├── sim_device.py             # simula la LilyGo firmando HMAC
    └── lilygo_secure_post.ino    # firmware real ESP32
```

## Paso 1 — Levantar todo en local

```bash
cd iot-poc/deploy
cp .env.local.example .env.local
docker compose -f docker-compose.local.yml up -d --build
curl http://localhost:8000/health   # {"db":"up","status":"ok"}
```

¿Qué pasó? Docker creó 2 contenedores: `postgres:16-alpine` (la DB) y `api`
(Flask+gunicorn). El `api` al arrancar ejecuta `init_db()` y crea la tabla `readings`.

## Paso 2 — CREATE (POST firmado, como la placa)

```bash
cd ../firmware
DEVICE_API_KEY=clave-local-demo HMAC_SECRET=hmac-local-demo \
  python3 sim_device.py http://localhost:8000/api/v1/readings
# 201 {"status":"stored",...}
```

El simulador firma `HMAC(timestamp.body)` igual que el `.ino`. Prueba romperlo:
cambia un dígito de la clave → `401`; manda `temperature_c: 125` → `422`.

## Paso 3 — READ (GET + dashboard)

```bash
curl -H "X-Api-Key: clave-local-demo" "http://localhost:8000/api/v1/readings?limit=5"
# abre en el navegador:
http://localhost:8000/dashboard
```

## Paso 4 — UPDATE y DELETE (completan el CRUD)

```bash
# Supón id=1 (míralo en el dashboard o el GET). El PUT exige firma HMAC igual que el POST:
# genera ts/sig como en sim_device.py y envía {"temperature_c": 26.5}
curl -X PUT http://localhost:8000/api/v1/readings/1 -H "Content-Type: application/json" \
  -H "X-Api-Key: clave-local-demo" -H "X-Timestamp: <ts>" -H "X-Signature: <sig>" \
  -d '{"temperature_c": 26.5}'
# DELETE solo pide API-Key:
curl -X DELETE http://localhost:8000/api/v1/readings/1 -H "X-Api-Key: clave-local-demo"
```

## Paso 5 — Firmware real

Flashea `lilygo_secure_post.ino` cambiando `WIFI_SSID/PASS`, `API_URL`
(apunta a tu PC: `http://TU-IP-PC:8000/api/v1/readings`) y las 2 claves demo.
El monitor serie debe mostrar `POST 201`.

## Paso 6 — El salto a AWS (rama main, ya montado)

| Local (este ejercicio) | AWS (main) |
|---|---|
| Postgres en contenedor `db` | RDS `iot-poc` (Postgres gestionado) |
| Clave DB en `.env.local` | Clave en Secrets Manager vía boto3 |
| Sin respaldo frío | S3 `raw/...jsonl` con SSE vía boto3 |
| Claves demo commiteadas | Claves reales solo en `.env` del EC2 |
| `docker-compose.local.yml` (api+db) | `docker-compose.yml` (solo api) |

Ver `docs/SECURITY.md` para el modelo de amenazas y lo pendiente pre-prod (TLS, RDS cifrado).

## Apagar el ejercicio

```bash
cd iot-poc/deploy
docker compose -f docker-compose.local.yml down -v   # -v borra los datos demo
```
