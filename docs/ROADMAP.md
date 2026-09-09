# Roadmap — del POC al despliegue en finca

Objetivo: sensores reales → dato íntegro y sin pérdidas → AWS → escala a gran extensión.

## Fase 1 — Sensores en la LilyGo (próxima sesión)
- [ ] Conectar HyT (ideal SHT31/SHT4x por I2C), suelo capacitivo v1.2/v2.0, solar (INA219 o divisor ADC).
- [ ] Calibrar suelo seco/mojado → %; promediar 3–5 lecturas por muestra.
- [ ] Mapear pines en `lilygo_secure_post.ino` (bloque TODO) y reflashear.
- [ ] Validar que el backend acepte los rangos reales (ajustar `RANGES` si un sensor legítimo sale de rango).

## Fase 2 — Fiabilidad del dato (cero pérdidas)
- [ ] Cola en placa: buffer circular RAM + NVS; reintentos con backoff; solo borrar con `201`.
- [ ] `reading_id` único (`MAC + epoch + contador`) y timestamp de muestreo (no de envío).
- [ ] Backend: endpoint `POST /api/v1/readings:batch` + columna `reading_id UNIQUE` (idempotencia).
- [ ] Timestamp de origen obligatorio; rechazar muestras del futuro o muy viejas.

## Fase 3 — Tríada CIA (ver `docs/SECURITY.md`)
- [ ] **Confidencialidad**: TLS extremo a extremo (placa → Nginx/ALB con Let's Encrypt; cerrar `:8000`).
- [ ] **Integridad**: HMAC por dispositivo (clave única por `device_id`), nonce anti-replay dentro de la ventana.
- [ ] **Disponibilidad**: RDS Multi-AZ + backups, alarma `CAÍDO > 10 min` (SNS), `DeletionProtection`.

## Fase 4 — Escala a finca extensa
- [ ] Las parcelas sin WiFi no llegan al EC2: desplegar **gateways LoRaWAN** (la placa ya habla LoRa)
      → gateway con backhaul 4G/WiFi → AWS IoT Core (MQTT + TLS + certificados X.509 por equipo).
- [ ] Zonificar por `device_id` (lote/sector) y tableros por zona.
- [ ] Medir consumo/energía (solar + deep-sleep) antes de comprar hardware en volumen.
- [ ] Costos: estimar con calculadora AWS (nº equipos × muestras/día) antes de escalar.

## Fase 5 — Operación
- [ ] Rotación de claves por dispositivo cada 90 días; revocación individual.
- [ ] Runbook de reanudación (ver README) + backups probados (restore drill).
- [ ] CI en verde + releases versionadas del firmware por lote.
