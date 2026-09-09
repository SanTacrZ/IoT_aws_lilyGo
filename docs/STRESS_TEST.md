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

## Pendientes de medir (proxima pasada)
- [ ] `GET /history` con 1M+ filas por sensor (con continuous aggregates)
- [ ] rule-engine: latencia de ciclo con 1,000 zonas
- [ ] rebote de conexiones (keep-alive HTTP) vs connection-per-request actual
