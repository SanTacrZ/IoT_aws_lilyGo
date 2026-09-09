# Motor de Automatización — Riego de Precisión

## Principios
1. **El campo manda:** los datos de sensores deciden; las reglas solo codifican la agronomía.
2. **Histéresis siempre:** regar cuando `soil < min`, parar cuando `soil > max`. Sin histéresis
   el relé rebota (desgaste + picos de corriente).
3. **Seguridad > eficiencia:** todo riego tiene `max_irrigation_min` (tope duro) y el firmware
   tiene watchdog local: si pierde conexión con el backend, usa sus últimos umbrales o apaga.
4. **Todo riego queda auditado** en `irrigation_events` (trigger, duración, litros).

## Ciclo de evaluación (backend, cada minuto — CloudWatch Events/EventBridge o hilo en la API)

```
por cada zona habilitada:
  1. sensor = último 'soil' de la zona (sensors_v2 + measurements_v2)
  2. si sensor viejo (> STALE_AFTER_S) o fuera de rango → alerta 'sensor_down', NO regar
  3. reglas activas de la zona con condition:
       below:  value < threshold - hysteresis/2
       above:  value > threshold + hysteresis/2
  4. cooldown: si rules.last_fired hace < cooldown_min → skip
  5. si condición cumple → INSERT commands (on, payload {duration_min: min(max, estimado)})
                         → UPDATE rules.last_fired
                         → INSERT irrigation_events (trigger='rule', rule_id)
                         → si action incluye 'alert' → INSERT alerts + publish SNS
  6. parada: si soil > max (o payload.duration cumplida y reportada por firmware)
             → INSERT commands (off)
```

## Cálculo de duración estimada (v1, sin caudalímetro)
```
duration_min = clamp(ceiling((soil_max - soil) / refill_rate_pct_min), 2, max_irrigation_min)
refill_rate_pct_min: calibración por zona (ej: 2.5 %/min con goteo). Empieza en 2.0 y se
ajusta con datos reales de irrigation_events (litros vs delta de humedad).
```

## Contrato de comandos (firmware ↔ backend)

- La placa hace `GET /api/v2/commands/pending?device_id=...` (X-Api-Key) tras cada POST de datos.
- Backend marca `delivered` y devuelve:
  ```json
  [{"cmd_id": 42, "actuator": "bomba1", "action": "on", "payload": {"duration_min": 10}}]
  ```
- La placa ejecuta (relé GPIO, con **tope local de `duration_min + 2` min de seguridad**) y al
  terminar hace `POST /api/v2/commands/<id>/done` con `{"result":"ok","liters":12.3}`.
  El backend cierra el `irrigation_event` abierto de ese actuador y registra los litros.
- Comandos `pending` con más de 30 min → `expired` (nunca regar con orden vieja).

## Contrato auxiliar (modo degradado)

- `GET /api/v2/config?device_id=X` → `{zone_id, soil_min_pct, soil_max_pct, max_irrigation_min}`.
  La placa lo cachea en **NVS (Preferences)**; sirve para regar sin internet.
- `POST /api/v2/irrigation/report` → `{"device_id", "events":[{"started_at": epoch,
  "duration_min": 8, "liters": 6.2}]}`: al reconectar, la placa reporta los riegos que hizo
  en modo offline; quedan auditados con `trigger='offline'`.
- Firmware de referencia: `iot-poc/firmware/lilygo_irrigation.ino`
  (relé GPIO26, caudalímetro YF-S201 GPIO27, suelo GPIO34, watchdog local, NVS).

## Modo degradado (campo sin internet)
El firmware guarda los últimos umbrales (`min/max`) recibidos en NVS (Preferences). Si no hay
red, aplica la misma lógica de histéresis en local y al reconectar reporta los riegos hechos.

## Fase 6 — Evapotranspiración (Penman-Monteith, FAO-56)
```
ETc = Kc(cultivo) × ET0
ET0 = [0.408·Δ·(Rn−G) + γ·(900/T+273)·u2·(es−ea)] / [Δ + γ·(1+0.34·u2)]
Necesita: temp, humedad, viento, radiación (OpenWeather API o estación propia).
Riego neto (mm) = ETc − lluvia_efectiva. Con área y caudal → minutos exactos.
```
Reemplaza gradualmente a los umbrales fijos: `rules` gana `mode: 'threshold' | 'etc'`.

## Alertas (kind → severidad → acción)
| kind | sev | cuándo |
|---|---|---|
| soil_dry | warn | humedad bajo umbral y riego no resolvió en 2 h |
| sensor_down | critical | última muestra > STALE_AFTER_S |
| battery_low | warn | battery_v < 3.5 |
| irrigation_stuck | critical | bomba on > max_irrigation_min |
| soil_flood | warn | humedad > max sostenido 1 h (fuga/avería válvula) |
