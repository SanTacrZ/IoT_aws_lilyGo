# LoRaWAN — parcelas sin WiFi (fase 5b)
#
# La LilyGo T-Beam trae modulo LoRa SX1276 de fabrica: no compra extra.
# Modelo propuesto para la comunidad:
#
#   [N placas T-Beam (nodo LoRa + sensores)]
#        | LoRa 868/915 MHz (2-15 km a la vista)
#        v
#   [1 gateway en la casa comunal con WiFi/internet]
#        v
#   [The Things Network (TTN) gratis / o ChirpStack self-hosted]
#        | MQTT (mismos topics de fase 5)
#        v
#   [mqtt_ingest.py ya existente — cero cambios backend]
#
# Decisiones:
# - OTAA (JoinEUI/AppKey por nodo) en lugar de ABP (seguro y revocable).
# - Payload compacto binario (9 bytes por muestra) para ahorrar airtime:
#   [soil u8] [temp s8] [hum u8] [batt u8 x0.02V] + opcional solar u8.
# - El backend recibe desde TTN con el MISMO esquema JSON v2 (decoder abajo).
# - dr/latencia: 1 muestra por 15 min (T-Beam deep-sleep entre muestras).

## Alta de la placa (T-Beam v1.1, SX1276) en TTN
1. Crear cuenta en ttneu/tna (o self-host ChirpStack en el gateway).
2. Crear Application -> End Device OTAA -> copiar JoinEUI/AppKey/NwkKey.
3. Gateway: RAK7268/comunero con Raspberry Pi (packet-forwarder) apuntando al server.

## Decoder (reemplaza TTN payload-formatters JS) -> emite el JSON v2
```javascript
function decodeUplink(input) {
  const b = input.bytes;
  if (b.length < 4) return { errors: ["payload corto"] };
  const data = {
    soil: b[0],                    // % 0-100
    temp: (b[1] > 127 ? b[1] - 256 : b[1]),  // °C -128..127
    hum: b[2],                     // %
    batt: +(b[3] * 0.02).toFixed(2) // V
  };
  return {
    data: {
      device_id: input.devEui ? "ttn-" + input.devEui.slice(-8) : "ttn-node",
      name: "Nodo LoRa",
      fw: "lorawan-v1",
      sensors: [
        { sensor_id: "soil1", type: "soil", unit: "%", value: data.soil },
        { sensor_id: "temp1", type: "temperature", unit: "C", value: data.temp },
        { sensor_id: "hum1", type: "humidity", unit: "%", value: data.hum },
        { sensor_id: "batt1", type: "battery", unit: "V", value: data.batt }
      ]
    }
  };
}
```
4. En TTN: Integrations -> MQTT -> endpoint publico (misma lib paho de mqtt_ingest.py,
   topic `v3/{app-id}/devices/{dev-id}/up` -> transformar con el decoder a nuestro esquema
   y republicar a `agrosense/<device_id>/data` — o apuntar mqtt_ingest.py directo a TTN
   con ese mapeo (5 lineas).

## Firmware
`iot-poc/firmware/lilygo_lorawan.ino` (LMIC/MCCI LoRaWAN, OTAA, deep-sleep 15 min).
NOTA de pinout: verificar DIO0/DIO1 de tu revision de T-Beam antes de flashear.

## Costos
- TTN publico: $0 (fair use) — ideal para comunidad.
- ChirpStack self-hosted en la Raspberry del gateway: $0, control total, y sin depender de terceros.
