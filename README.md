# IoT AWS LilyGo 🌱☀️

POC profesional de IoT: **LilyGo (ESP32) + Soil Moisture + Humedad/Temperatura (HyT) + Radiación Solar** → backend **Flask contenerizado** en EC2 → **PostgreSQL en RDS** → **boto3** (Secrets Manager + backup S3).

## Arquitectura

```
LilyGo ESP32 ──POST JSON firmado (HMAC)──▶ EC2 s2 :8000 (Docker: api)
         │                                          │  ├─ boto3 (CONTROL): Secrets Manager → credenciales RDS
         │                                          │  │                    S3 → backup frío raw/*.jsonl
                                                 psycopg2 (DATOS): INSERT → RDS Postgres (tabla readings)
```

**Aclaración importante: boto3 nunca habla con el RDS.** Hay dos planos separados:

* **Plano de control (boto3):** al arrancar, el backend pide `{username, password, host,
  port, dbname}` a **Secrets Manager** (`DB_SECRET_ARN`, con caché en memoria) y usa
  `PutObject/GetObject` contra **S3** para el respaldo frío con `SSE AES256`.
  Así la clave del RDS **nunca vive en disco ni en git** (ver `docs/SECURITY.md`).
* **Plano de datos (psycopg2):** con esas credenciales abre TCP a `:5432` y ejecuta el
  `INSERT INTO readings`. Si mañana el Postgres cambia de proveedor, boto3 ni se entera.

El backup S3 es best-effort: si falla, el `INSERT` ya quedó y el request igual devuelve `201`.

**Seguridad (no se envía plano):** `X-Api-Key` + `X-Timestamp` + `X-Signature = HMAC-SHA256(timestamp.body)` con ventana anti-replay (5 min). En producción siempre HTTPS/TLS delante.

## Estructura

```
iot-poc/
├── backend/          # API Flask + psycopg2 + boto3 (Dockerfile, app.py, requirements.txt)
├── deploy/           # docker-compose.yml + .env.example (despliegue EC2)
├── firmware/         # lilygo_secure_post.ino (ESP32) + sim_device.py (simulador)
app.py                # servidor Flask legacy clase (puerto 80, /dashboard)
stress.py             # prueba de estrés GET/POST stdlib
user-data.sh          # bootstrap EC2 original
```

## Contrato de datos

`POST /api/v1/readings` (firmado):
```json
{"device_id":"lilygo-01","temperature_c":25.4,"humidity_pct":58.1,
 "soil_moisture_pct":42.0,"solar_w_m2":480.5,"battery_v":4.02}
```

## Despliegue rápido (EC2 s2)

```bash
cd iot-poc/deploy
cp .env.example .env   # rellenar DEVICE_API_KEY, HMAC_SECRET, DB_SECRET_ARN, S3_BACKUP_BUCKET
docker-compose up -d --build
curl http://localhost:8000/health
DEVICE_API_KEY=... HMAC_SECRET=... python3 ../firmware/sim_device.py http://TU-IP:8000/api/v1/readings
```

Infra AWS usada: EC2 `s2` (t2.micro, us-west-2), RDS `iot-poc` (postgres free-tier), S3 `iot-poc-265096288210-usw2`, Secrets Manager.

## Estándares

12-factor (config por entorno), contenedor con healthcheck + gunicorn, DDL versionado en `init_db()`, backup best-effort que nunca tumba el ingest, logs estructurados.

## Seguridad

Política completa en [`docs/SECURITY.md`](docs/SECURITY.md): HMAC-SHA256 + anti-replay,
Secretos fuera del repo (Secrets Manager + `.env` solo en el servidor), doble escritura
RDS + S3 con SSE, detector EN LÍNEA/CAÍDO y plan de endurecimiento pre-producción (TLS, RDS cifrado, IAM Role).

Roadmap al despliegue en finca en [`docs/ROADMAP.md`](docs/ROADMAP.md): sensores reales,
cero pérdidas (cola + idempotencia), tríada CIA y escala con LoRaWAN/IoT Core.
