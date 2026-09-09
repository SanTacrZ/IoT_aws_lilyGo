# Entorno de desarrollo con Docker

## Levantar todo
```bash
cd iot-poc/deploy
docker compose -f docker-compose.dev.yml up -d          # db + api-v1 + api-v2 + rule-engine + seed
docker compose -f docker-compose.dev.yml --profile demo up -d sim   # (opcional) placas simuladas
```

## Servicios y puertos
| Servicio | URL/puerto | Qué es |
|---|---|---|
| api-v2 | http://localhost:8001 | `/api/v2/*` + `/dashboard-v2` (sistema autónomo) |
| api-v1 | http://localhost:8000 | POC de clase (compatibilidad) |
| db | localhost:5433 | PostgreSQL 16 + TimescaleDB (datos persistentes en volumen) |
| rule-engine | — | motor de riego: evalúa reglas cada 60 s |
| mosquitto | :1884 | broker MQTT dev (anon; en AWS = IoT Core X.509) |
| mqtt-ingest | — | consume `agrosense/+/data` → mismo upsert autónomo que HTTP |
| seed | — | one-shot: extensión timescale + migración 002 + datos demo |
| sim | — | placas virtuales que postean firmado HMAC cada 30 s |

## Credenciales dev (`iot-poc/deploy/.env.dev`)
```
DEVICE_API_KEY=demo-key-cambiar
HMAC_SECRET=demo-secret-cambiar
ADMIN_KEY=demo-admin-cambiar
```
**Nunca subir el `.env` real** (solo el example). En AWS van a Secrets Manager.

## Verificar
```bash
curl -s localhost:8001/health
curl -s -H "X-Api-Key: demo-key-cambiar" localhost:8001/api/v2/state
curl -s -H "X-Admin-Key: demo-admin-cambiar" localhost:8001/api/v2/farms
docker compose -f docker-compose.dev.yml logs -f rule-engine
```

## Tests (7 de integracion: CRUD + ingesta HMAC + ciclo de riego)
```bash
docker compose -f docker-compose.dev.yml exec api-v2 pip install -q pytest
docker compose -f docker-compose.dev.yml exec api-v2 pytest -q
```
Tambien corre en CI (GitHub Actions) contra un servicio timescaledb efimero.

## Prueba de estres
```bash
python3 tools/stress_test.py http://localhost:8001 reads 500 30   # desde el host
```

## Probar MQTT (fase 5)
```bash
docker compose -f docker-compose.dev.yml exec mosquitto mosquitto_pub -h localhost \
  -t "agrosense/mqtt-dev-01/data" -q 1 \
  -m '{"device_id":"mqtt-dev-01","name":"Placa MQTT","fw":"v5","sensors":[{"sensor_id":"soil1","type":"soil","unit":"%","value":38.5}]}'
# la placa se registra sola: verla en /api/v2/state y en el dashboard
# firmware: iot-poc/firmware/lilygo_mqtt.ino (PubSubClient, QoS1)
```

## Reset total
```bash
docker compose -f docker-compose.dev.yml down -v   # borra datos tambien
```
