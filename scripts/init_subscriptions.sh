#!/bin/bash
ORION_URL="http://localhost:1026"

echo "Creating initial entity sensor:nodo001..."
curl -s -o /dev/null -w "%{http_code}" -iX POST "${ORION_URL}/v2/entities" \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "sensor:nodo001",
    "type": "AmbientSensor",
    "temperatura": {"value": 0, "type": "Float"},
    "humedad": {"value": 0, "type": "Float"},
    "radiacion_solar": {"value": 0, "type": "Float"},
    "ph": {"value": 0, "type": "Float"},
    "timestamp": {"value": "2024-01-01T00:00:00Z", "type": "DateTime"}
  }'
echo ""

# El historial ya no pasa por QuantumLeap: el iot-agent escribe cada lectura
# directo en la hypertable `lecturas` de TimescaleDB. Las suscripciones de
# Orion solo hacen falta si se suscribe otro consumidor (reglas, webhooks...).
echo "Sin suscripciones a QuantumLeap (historial persistido en PostgreSQL/TimescaleDB)."
echo "Subscriptions script finished."