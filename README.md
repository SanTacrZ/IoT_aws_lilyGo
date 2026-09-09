# AgroSense 🌱 — Plataforma IoT de Agricultura de Precisión

> Del POC de clase a sistema de producción: monitoreo y riego autónomos para comunidades agrícolas.
> **Rama de desarrollo completo: `ejercicio-3`.** AWS + PostgreSQL + ESP32/LilyGo.

## Qué es

Sistema autónomo que monitorea cultivos en tiempo real y riega por sí solo:

```
CAPA CAMPO            CAPA CONECTIVIDAD          CAPA NUBE AWS                CAPA APP
LilyGo T-Beam    ──►  HTTPS+HMAC (v2)      ──►  ALB → ECS Fargate      ──►  Dashboard v5
 + suelo cap.,        MQTT QoS1 (v5)            └→ RDS PostgreSQL 16.15      (Tailwind+Lucide,
 DHT22, solar,        LoRaWAN (TTN,             (privado) + MV horaria       zonas, gauges,
 batería              parcelas sin WiFi)        + pg_cron                    feed, toasts)
 Actuadores: relé,     ← comandos (polling)      Secrets Manager · SNS        Motor de reglas
 caudalímetro,                                   CloudWatch                   (histéresis+cooldown)
 watchdog local
```

**Autonomía total:** los equipos se registran solos al primer POST (UPSERT), los sensores
nuevos aparecen sin migraciones, las bajas son lógicas y reversibles, y si la placa pierde
internet riega con reglas locales (NVS) y reporta al reconectar.

## Estado del proyecto

| Fase | Alcance | Estado |
|---|---|---|
| 1 | POC clase: Flask + HMAC + RDS + dashboard v1 | ✅ `main` |
| 2 | Arquitectura autónoma v2 (registro automático, multi-equipo) | ✅ `ejercicio-3` |
| 3 | Precisión agrícola: zonas, cultivos, reglas, motor de riego, alertas, CRUD | ✅ `ejercicio-3` |
| 4 | AWS IaC (Terraform: VPC+RDS+Fargate+ALB+Secrets+SNS) | ✅ validado en lab, destruido p/ahorrar créditos |
| 5 | MQTT (Mosquitto local + ingestor, mismos topics de IoT Core) | ✅ `ejercicio-3` |
| 5b | LoRaWAN (diseño TTN + firmware OTAA T-Beam) | 📦 diseñado (`docs/LORAWAN.md`) |
| 6 | ETc evapotranspiración + TinyML anomalías | ⏳ próximo |

## Inicio rápido (local, 2 min)

```bash
cd iot-poc/deploy
docker compose -f docker-compose.dev.yml up -d            # db+apis+rule-engine+mosquitto
docker compose -f docker-compose.dev.yml --profile demo up -d   # placas simuladas
```
- Dashboard: **http://localhost:8001/dashboard-v2** (API-Key: `demo-key-cambiar`)
- Admin-Key: `demo-admin-cambiar` (riego manual, alertas)
- Tests: `docker compose -f docker-compose.dev.yml exec api-v2 pytest -q` (8 e2e)
- Reset: `docker compose -f docker-compose.dev.yml down -v`

## Producción (AWS Academy / cuenta propia)

IaC completa en `deploy/terraform/` — ver **`docs/DEPLOYMENT_REPORT.md`** para el runbook
de redespliegue (10 min), costos reales medidos y las limitaciones del lab resueltas.

## Estructura

```
iot-poc/backend/       Flask v1 (clase) + app_v2.py (autónomo) + mqtt_ingest.py + motor de reglas
automation/            rule_engine.py (riego de precisión) + loop.py
iot-poc/firmware/      LilyGo: genérico, riego (relé+caudalímetro+offline), MQTT, LoRaWAN
iot-poc/deploy/        docker-compose.dev.yml (todo el stack local) + terraform/ (AWS)
db/migrations/         002_precision.sql (zonas/reglas/actuadores/usuarios)
tools/                 stress_test.py + seed_burn.sql (demo local)
docs/                  CRUD_DESIGN, AUTOMATION, DATA_MODEL, SECURITY, STRESS_TEST,
                       LORAWAN, DEV_DOCKER, DEPLOYMENT_REPORT
```

## Seguridad (resumen)
- Dispositivos: HMAC-SHA256 + timestamp anti-replay + rate-limit por device
- Humanos: API-Key (solo lectura) / Admin-Key (escritura) — jamás en URLs ni commits
- Producción: RDS privado, secretos en Secrets Manager, claves aleatorias de 40-64 chars

## Equipo y contexto
Proyecto universitario (UPB) — de POC de clase a plataforma comunitaria de agricultura
de precisión. Metodología: desarrollo por ejercicios incrementales en ramas, tests e2e,
benchmark con 1M de filas y despliegue real en AWS.
