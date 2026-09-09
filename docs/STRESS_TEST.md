# Benchmark / Pruebas de estres — API AgroSense

Herramienta: `tools/stress_test.py` (stdlib, hilos, percentiles).
Entorno de prueba: Docker local (Fargate-size minusculo: 4 workers gunicorn + Postgres 16
+ TimescaleDB en la misma maquina). Los numeros ABSOLUTOS varian por maquina; los
ratios y cuellos de botella son lo que importa.

## Como reproducir
```bash
cd iot-poc/deploy && docker compose -f docker-compose.dev.yml up -d
python3 tools/stress_test.py http://localhost:8001 reads 2000 100
python3 tools/stress_test.py http://localhost:8001 writes 1000 50
python3 tools/stress_test.py http://localhost:8001 dash 300 30
```

## Resultados (antes -> despues de optimizar)

| Escenario | Metrica | Antes | Despues |
|---|---|---|---|
| reads mixtas conc30 | rps / p99 | 1364 / 42ms | 1660 / 40ms |
| **`/state` con 100+ devices** | rps / p90 | **135 / 460ms** | **1660 / 29ms** (16x) |
| reads heavy conc100 | rps / p99 | 551 / 273ms | 2408 / 76ms (4.4x) |
| writes (ingest HMAC) conc50 | rps / p99 | 598 / 159ms | 858 / 128ms, 0 errores |
| dashboard HTML conc30 | rps / p50 | 2917 / 8ms | (no cambio) |
| anti-abuso | 429 rate-limit | — | funciona: 6/20 bloqueados mismo device |

## Cuellos de botella encontrados y arreglados
1. **N+1 en `/api/v2/state`**: 1 query de sensores por dispositivo (101 queries con 100
   equipos). Fix: lote con `WHERE device_id = ANY(%s)` -> 2 queries fijas. 16x mas rapido.
2. **Gunicorn 2 workers**: saturaba ~550 rps bajo conc100. Fix dev: 4 workers x 4 threads.

## Limites observados (con este sizing)
- ~2,400 rps de lectura, ~860 rps de ingesta firmada sin errores (local, DB co-ubicada).
- Rate-limit 12/min/device dispara 429 correctos (proteccion, no bug).
- No se observaron 5xx ni timeouts en ninguna corrida (5,000+ requests totales).

## Camino de escala en produccion (cuando estos numeros se queden cortos)
| Sintoma | Solucion (en orden de costo) |
|---|---|
| p99 > 100ms con >1k devices | read-replica RDS para /state, cache 5s en memoria |
| Ingesta > 2k rps | SQS entre ALB e insercion; batch INSERT multi-row |
| Conexiones DB agotadas | PgBouncer (sidecar) |
| Historial lento (TB) | Timescale chunks ya; continuos aggregates para dashboard |
| Multi-region | Replicas + Route53 latencia (lejos: no necesario <10k devices) |

## Segunda pasada: 1,000,000 de filas + continuous aggregates (Timescale)

Dataset: 1M lecturas (100 devices x soil1, ~2 anos de minutos).

| Query | Antes (crudo) | Con `readings_hourly` (continuous agg) | Mejora |
|---|---|---|---|
| Tendencia 7 dias por sensor | 11.8 ms | **0.155 ms** | 76x |
| Tendencia 1 anio horaria | 613.7 ms (chunks comprimidos) | **67.8 ms** | 9x |
| Tendencia 1 anio diaria (re-agg) | — | **4.7 ms** | 130x |
| `/history` ultimos 100 pts de 1M | 2.9 ms (indice) | — | ok |
| **HTTP** `/api/v2/history-agg` x500 conc30 | — | **2,315 rps · p99 35 ms** | — |

Por que es rapido: chunk pruning (7 dias = solo chunk fresco), compresion columnar
(~90% en chunks >7 dias) y el continuous aggregate pre-materializado con refresh
automatico por hora (`add_continuous_aggregate_policy`).
Endpoint nuevo: `GET /api/v2/history-agg?device_id&sensor_id&bucket=1h|1d&hours<=8760`.
Dashboard: boton 📈 por sensor -> graficas (Chart.js) de promedio/minimo horario con
rangos 24h/7d/1a + ultimos 200 puntos crudos.

## Pendientes de medir (proxima pasada)
- [ ] rule-engine: latencia de ciclo con 1,000 zonas
- [ ] rebote de conexiones (keep-alive HTTP) vs connection-per-request actual
