# Sensores: rangos y umbrales

Cada dato tiene dos pares de límites distintos:

* **Rango del sensor**: lo que el componente puede medir según su datasheet. Un
  valor fuera de él no es una medida sino un fallo (bus I2C, cableado,
  conversión) o una manipulación. El firmware descarta la lectura y el
  `iot-agent` la rechaza con HTTP 422 (`RANGOS` en `services/iot-agent/app.py`).
* **Umbral de alerta**: lo que es malo para el cultivo. Son valores genéricos
  de partida y dependen del cultivo; se ajustan en `.env` (`ALERT_*`).

## Tabla

| Dato | Sensor | Rango del sensor (mín – máx) | Precisión / resolución | Alerta mín | Alerta máx |
|---|---|---|---|---|---|
| `temperatura_aire` (°C) | HDC1080 | −40 – 125 | ±0.2 °C típ.; 14 bit | < 5 (`ALERT_TEMP_MIN`) | > 35 (`ALERT_TEMP_MAX`) |
| `humedad_aire` (%HR) | HDC1080 | 0 – 100 | ±2 %HR típ.; 14 bit | < 30 (`ALERT_HR_MIN`) | > 90 (`ALERT_HR_MAX`) |
| `luz_lux` (lux) | AP3216C, Range 1 | 0 – 20 661 | 0.35 lux/cuenta, 16 bit | — | — |
| `luz_pct` (%) | índice calibrado | 0 – 100 | relativo a la instalación | — | — |
| `humedad_suelo_pct` (%) | sonda + ADC | 0 – 100 | según calibración seco/agua | < 30 crítica (`ALERT_SUELO_MIN`) | > 90 (`ALERT_SUELO_MAX`) |
| `humedad_suelo_raw` | ADC1_CH3 (GPIO39) | 0 – 4095 | 12 bit; válido 1100 – 4000 con la calibración actual | — | — |
| `latitud` / `longitud` (°) | GPS u-blox | −90 – 90 / −180 – 180 | ~2.5 m CEP con fix | — | — |
| `altitud` (m) | GPS u-blox | −500 – 50 000 | límite operativo u-blox | — | — |
| `satelites` | GPS u-blox | 0 – 50 | — | — | — |
| `bateria_v` (V) | AXP192 / AXP2101 | 2.5 – 4.5 | 1.1 mV (AXP192), 1 mV (AXP2101) | < 3.4 (`ALERT_BATERIA_MIN`) | — |
| `wifi_rssi` (dBm) | ESP32 | −120 – 0 | 1 dBm | < −85 (`ALERT_RSSI_MIN`) | — |
| `heap_libre` (bytes) | ESP32 | 0 – 524 288 | — | — | — |
| `pendientes` | cola NVS | 0 – 8 | `MAX_PENDIENTES` | — | — |

Los umbrales usan histéresis para no repetir avisos cuando el valor oscila:
el suelo seco abre bajo 30 % y cierra sobre 35 %; la humedad del aire, ±3 %HR;
la temperatura, ±1 °C; la batería, +0.1 V; la señal, +5 dBm.

## Notas de los datasheets que afectan al código

**HDC1080** (TI SNAS672)
* Dirección 0x40. El firmware comprueba al arrancar Manufacturer ID (0xFE) =
  `0x5449` y Device ID (0xFF) = `0x1050`, para notar si otro chip responde ahí.
* Modo de adquisición individual (configuración por defecto): se apunta al
  registro 0x00 o 0x01 y se espera la conversión antes de leer (6.35 ms a
  14 bit; el firmware espera 20 ms).
* Exposición larga sobre ~80 %HR puede desplazar la lectura de humedad; el
  chip trae un calentador interno (bit HEAT del registro 0x02) para secarlo.
  No se usa todavía.

**AP3216C** (rev 0.86)
* Tras el reset por software (0x00 = 0x04) hay que **esperar 10 ms** antes de
  configurar. La librería AP3216_WE no lo hacía; el firmware ahora hace la
  secuencia a mano y ya no usa esa librería.
* Solo ALS (0x00 = 0x01): el modo ALS+PS enciende el LED IR cada 112.5 ms.
* Satura en 20 661 lux; el sol directo ronda los 100 000. Por eso `luz_pct`
  es un índice relativo y no un porcentaje de lux reales.
* Si va dentro de una caja con ventana, la ventana atenúa la luz: el registro
  0x19 (ALS calibration) permite compensarlo.

**Sonda de suelo (módulo resistivo con AO/DO)**
* La salida baja al mojarse: seco ≈ 3700, agua ≈ 1400 cuentas (medido
  2026-10-16).
* El ADC del ESP32 con atenuación de 11 dB es lineal entre ~150 y 2450 mV;
  el extremo seco (~3 V) cae en la zona no lineal, que comprime las lecturas
  de suelo seco. El modo calibración muestra mV con `analogReadMilliVolts`
  (calibración de fábrica en eFuse).
* Las sondas resistivas se corroen con corriente continua. Alimentarla desde
  un GPIO solo durante la medida o cambiar a una sonda capacitiva alarga su vida.
* AO nunca por encima de 3.3 V: los GPIO del ESP32 no toleran 5 V.

**GPS u-blox del T-Beam**
* Se alimenta desde el PMU: LDO3 en el AXP192 (T-Beam v1.0/1.1) y ALDO3 en el
  AXP2101 (v1.2). El firmware ahora la enciende a 3.3 V; antes no lo hacía y
  el historial nunca registró un fix.
* El log serie muestra `GPS: N caracteres NMEA`: con 0, el módulo no transmite
  (energía o pines); con más de 0 y sin fix, le falta cielo abierto. El primer
  fix en frío puede tardar varios minutos.
