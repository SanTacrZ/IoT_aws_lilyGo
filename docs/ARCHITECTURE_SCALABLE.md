# Arquitectura escalable y autónoma — rama `feat/sensores-autonomos`

## Problema actual (main)
- `readings` tiene columnas fijas: temperature, humidity, soil, solar, battery.
  Agregar un sensor nuevo = migración SQL + cambio backend + cambio firmware + cambio dashboard.
- Dashboard muestra solo el ÚLTIMO equipo global. Con N placas se pisan.
- Alta/baja de sensores es manual.

## Diseño propuesto: registro autónomo + métricas genéricas

```
[LilyGo N] --POST /api/v2/readings {device_id, sensors:[{type,unit,value},...]}--> [Flask]
   firma HMAC igual que v1, auto-registra device + sensores nuevos      |
                                                                        v
                                              +------------------+------------------+
                                              | devices (registry)                 |
                                              | device_id PK, name, fw, last_seen, |
                                              | status auto: online/stale/offline  |
                                              +------------------+------------------+
                                              | sensors (registry)                 |
                                              | device_id, sensor_id, type, unit,  |
                                              | enabled (soft-delete), last_value  |
                                              +------------------+------------------+
                                              | measurements (datos)               |
                                              | device_id, sensor_id, value, ts    |
                                              +------------------+------------------+
                                                                        |
                                                                        v
                                              GET /api/v2/state -> JSON para dashboard
                                              GET /dashboard-v2 -> tarjetas por equipo/sensor (poll 5s o SSE)
```

### Reglas de autonomía
1. **Auto-alta:** todo POST válido hace UPSERT en `devices` (actualiza `last_seen`) y UPSERT en
   `sensors` por cada `sensor_id` recibido. Nada que crear a mano.
2. **Auto-baja (soft):** `DELETE /api/v2/devices/<id>/sensors/<sid>` pone `enabled=false`
   (no borra historia). El dashboard lo oculta. Si el sensor vuelve a enviar datos, se
   re-habilita solo (`enabled=true`).
3. **Baja de equipo:** `DELETE /api/v2/devices/<id>` = baja lógica (`devices.enabled=false`).
   Reaparece solo si vuelve a postear.
4. **Estado online/stale/offline:** calculado, no almacenado: `now - last_seen < 3 min` online,
   `< 15 min` stale, resto offline. El firmware envía cada 60 s.
5. **Contrato flexible:** el firmware declara su lista de sensores en cada POST.
   Quitar un sensor físico = dejar de enviarlo + (opcional) DELETE para ocultarlo ya.
   Agregar uno = empezar a enviarlo. Cero migraciones.

### Endpoints v2 (conviven con v1)
- `POST /api/v2/readings` (firmado HMAC, igual que v1) body:
  `{"device_id":"lilygo-01","sensors":[{"sensor_id":"temp1","type":"temperature","unit":"°C","value":25.3}, ...]}`
- `GET /api/v2/state` (API-Key) -> `{devices:[{device_id,name,last_seen,status,sensors:[...]}]}` para dashboard JS.
- `GET /api/v2/history?device_id=X&sensor_id=Y&limit=100` (API-Key) para gráficas.
- `DELETE /api/v2/devices/<d>/sensors/<s>` y `DELETE /api/v2/devices/<d>` (API-Key) = bajas lógicas.
- `GET /dashboard-v2` -> HTML que renderiza tarjetas por equipo/sensor con polling 5 s. Sin cambios
  al agregar/quitar sensores.

### Firmware
Ver `iot-poc/firmware/lilygo_generic_post.ino`: tabla `SensorDef sensors[]` — para agregar un sensor
solo añades una línea `{ "soil2", "humidity", "%", pin, leerSuelo }`. Todo lo demás es genérico.

### Migración
- v1 intacto. v2 crea tablas nuevas `devices_v2, sensors_v2, measurements_v2`.
- Cuando quieras, `dashboard-v2` pasa a ser el principal.
