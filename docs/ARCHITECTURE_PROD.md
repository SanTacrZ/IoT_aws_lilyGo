# AgroSense — Arquitectura de Producción para Agricultura de Precisión

> Sistema autónomo de monitoreo y riego para comunidades agrícolas.
> AWS + PostgreSQL. Evolución del POC de clase hacia plataforma comunitaria.

## 1. Visión

Una comunidad con N parcelas, M zonas de riego por parcela y K sensores/actuadores.
El sistema debe: monitorear en tiempo real, decidir cuándo regar (motor de reglas +
evapotranspiración), actuar (bomba/válvulas), alertar (SNS) y escalar sin rediseño.

## 2. Arquitectura de 4 capas

```
CAPA 1 — CAMPO (sensores/actuadores)
  LilyGo ESP32: suelo (capacitivo), DHT22, lluvia, solar, batería.
  Actuadores: relé bomba 12V, electroválvulas por zona, caudalímetro.
  Power: panel solar + Li-ion, deep-sleep entre muestras.

CAPA 2 — CONECTIVIDAD
  Corto plazo (POC actual): WiFi → HTTPS firmado (HMAC) → backend.
  Medio plazo: MQTT/TLS con X.509 hacia AWS IoT Core (porThings/certificados,
  shadow para estado offline, reglas IoT Rules). WiFi solo si hay cobertura;
  alternativas: LoRaWAN (Helium/TTN) + gateway para parcelas remotas.

CAPA 3 — NUBE AWS
  Ingesta:   IoT Core (MQTT)  o  ALB → ECS Fargate (API Flask, actual)
  Cola:      IoT Rule → SQS (buffer/anti-pico) → Lambda ingest
  Datos:     RDS PostgreSQL 16 + extensión TimescaleDB (hypertable measurements,
             compresión ~90%, retención automática)  [ref: Tiger Data/Timescale]
  Histórico: S3 (raw JSONL diario, Glue+Athena opcional para analítica)
  Alertas:   SNS (SMS/email a comuneros): sensor caído, riego anómalo, batería baja
  Secrets:   Secrets Manager (rotación), IAM por servicio, VPC privada para RDS
  Observ.:   CloudWatch (logs/métricas/alarmas)
  BI:        QuickSight (conecta a Postgres vía JDBC) o dashboard propio

CAPA 4 — APLICACIÓN
  Dashboard web (actual /dashboard-v2, evolución a React/PWA offline-first).
  Motor de automatización (ver docs/AUTOMATION.md).
```

### Diagrama lógico (flujo de datos)

```
[Sensores] --MQTT/HTTPS--> [Ingesta] --INSERT--> [PostgreSQL/Timescale]
                                  |                      |
                                  |                [Motor de reglas] --comandos--> [Cola cmd] --> [ESP32 polling / GET cmd]
                                  |                      |
                                  |                [Alertas SNS]  [Agregados p/ dashboard]
                                  v
                               [S3 raw] --> Athena/QuickSight (analítica)
```

## 3. Decisiones de diseño (y por qué)

| Decisión | Justificación |
|---|---|
| PostgreSQL único (no NoSQL) | Metadatos relacionales + series de tiempo en 1 DB; TimescaleDB da particionado/compresión sin operar 2 sistemas (ref: tigerdata.com/blog/storing-iot-data). El Guidance de AWS usa DocumentDB+MSK, pero es sobredimensionado para una comunidad; empezamos simple y escalable. |
| TimescaleDB hypertable | INSERT alto rendimiento, `time_bucket` para agregados del dashboard, compresión 90%+, retención `drop_chunks`. RDS lo soporta (parameter group `shared_preload_libraries=timescaledb`). |
| MQTT+X.509 (fase 2) | TLS mutuo por dispositivo, shadow = estado si la placa se cae, reglas sin backend intermedio. Mientras tanto HMAC actual es suficiente y ya funciona. |
| Comandos por polling | La placa consulta `GET /api/v2/commands/pending` cada ciclo. Simple, funciona con HTTPS actual; en fase 2 pasa a MQTT topic `cmd/<device>` (push). |
| Bajas lógicas (enabled=false) | Nunca perdemos historia; el sensor que vuelve se re-habilita solo (ya implementado en v2). |
| Multi-tenancy comunitario | Tabla `farms` y `zones`; cada dispositivo pertenece a una zona. Permisos por rol (propietario/comunero/admin). |

## 4. Modelo de datos de producción

Ver `docs/DATA_MODEL.md` y `db/migrations/002_precision.sql`. Resumen:

- `farms` (parcelas) → `zones` (zonas de riego con cultivo y umbrales) → `devices_v2` → `sensors_v2` → `measurements_v2` (hypertable).
- `actuators` (bomba/válvulas) → `commands` (cola) → `irrigation_events` (auditoría de riegos).
- `rules` (umbrales por zona con histéresis) → `alerts` (eventos generados).
- `users` + `farm_members` (roles comunitarios).

## 5. Roadmap de implementación

| Fase | Alcance | Estado |
|---|---|---|
| 1. POC | HTTPS+HMAC, dashboard, CRUD | ✅ (main) |
| 2. Autonomía | Registro auto, dashboard multi-equipo v2 | ✅ (rama actual) |
| 3. Precisión | Zonas/cultivos, motor de riego, comandos, alertas | 🔨 (esta rama) |
| 4. Nube AWS | RDS+TimescaleDB, ECS Fargate, S3, SNS, CI/CD | ⏳ docs/AWS_DEPLOYMENT.md |
| 5. MQTT+X.509 | Ingesta MQTT local lista (Mosquitto + mqtt_ingest.py + firmware lilygo_mqtt.ino, esquema y topics idénticos a IoT Core). Falta: X.509 + shadows en AWS | 🔄 en progreso |
| 6. Inteligencia | ET0 (Penman-Monteith) con datos OpenWeather, TinyML anomalías | ⏳ |

## 6. Costos estimados (comunidad pequeña, fase 4)

- RDS db.t4g.micro Multi-AZ off + TimescaleDB: ~$15–25/mes
- ECS Fargate 0.25 vCPU: ~$10/mes (o Lambda casi $0 si ingest <1M eventos/mes)
- S3 + IoT Core (<100 dispositivos): <$5/mes
- Total: **~$30–40/mes** (candidato a AWS Activate/EdTech credits)

## 7. Referencias

- AWS Guidance: Building an Agricultural Sensor Network using IoT (2023)
- AWS Reference Architecture: Smart Farm on AWS (archivado, aplica conceptos)
- Timescale/Tiger Data: "Storing IoT Data: Why PostgreSQL" y arquitectura real-time analytics
- Scientific Reports (2026): IoT-driven smart irrigation (ESP32 + SSE, validación de eficiencia hídrica)
- FAO-56: Penman-Monteith para ET0 (fase 6)
