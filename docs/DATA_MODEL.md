# Modelo de Datos — AgroSense (v2 + precisión)

## Diagrama entidad-relación (resumen)

```
farms 1──* zones 1──* devices_v2 1──* sensors_v2 1──* measurements_v2
              │            │
              │            └──* actuators 1──* commands
              │
              ├──* rules
              └──* irrigation_events
users *──* farm_members *──1 farms
alerts (zone_id, device_id)
```

## Tablas y propósito

| Tabla | Propósito | Claves |
|---|---|---|
| `farms` | Parcelas/comunidades | farm_id PK |
| `zones` | Zonas de riego con cultivo + umbrales (min/max/histéresis) | (farm_id,name) UNIQUE |
| `devices_v2` | Registro autónomo de placas (ya en v2) + `zone_id` | device_id PK |
| `sensors_v2` | Registro autónomo de sensores por placa (ya en v2) | (device_id,sensor_id) PK |
| `measurements_v2` | Datos crudos (hipertable TimescaleDB en prod) | id PK, (device_id,sensor_id,ts) idx |
| `actuators` | Bomba/válvulas, pin GPIO, estado actual | (device_id,label) UNIQUE |
| `commands` | Cola backend→firmware (pending→delivered→done) | idx partial status='pending' |
| `irrigation_events` | Auditoría: cada riego, duración, litros, trigger, regla | event_id PK |
| `rules` | Reglas por zona: condición+umbral+acción+cooldown | rule_id PK |
| `alerts` | Eventos anómalos, con ack comunitario | idx abiertos (acked_at NULL) |
| `users`/`farm_members` | Multi-tenancy: quién ve/gobierna qué parcela | roles viewer/farmer/admin |

## Consultas clave del dashboard (TimescaleDB)

```sql
-- Último valor por sensor (rápido con index (device_id,sensor_id,ts DESC))
SELECT DISTINCT ON (device_id, sensor_id) device_id, sensor_id, value, ts
FROM measurements_v2 ORDER BY device_id, sensor_id, ts DESC;

-- Serie horaria para gráfica (time_bucket de Timescale)
SELECT sensor_id, time_bucket('1 hour', ts) AS h,
       avg(value) AS avg_v, min(value) AS min_v, max(value) AS max_v
FROM measurements_v2
WHERE device_id = $1 AND ts > now() - interval '7 days'
GROUP BY sensor_id, h ORDER BY h;

-- Humedad media por zona (join registro)
SELECT z.name, avg(m.value) AS soil_avg
FROM measurements_v2 m
JOIN sensors_v2 s USING (device_id, sensor_id)
JOIN devices_v2 d USING (device_id)
JOIN zones z USING (zone_id)
WHERE s.type = 'soil' AND m.ts > now() - interval '1 hour'
GROUP BY z.name;

-- Agua usada por parcela este mes
SELECT z.name, sum(liters) FROM irrigation_events i JOIN zones z USING (zone_id)
WHERE i.started_at > date_trunc('month', now()) GROUP BY z.name;
```

## Convenciones
- Bajas siempre lógicas (`enabled=false`); sin DELETE físico salvo `ON DELETE CASCADE` al
  eliminar una zona/parcela completa.
- `measurements_v2` nunca se UPDATEA (append-only). Correcciones = nueva lectura.
- Tipos de sensor normalizados (text): `soil, temperature, humidity, rain, solar, battery,
  ph, ec, water_level, flow`.
