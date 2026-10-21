#!/bin/bash
set -e
echo "========================================"
echo "  IoT FIWARE Platform - Setup"
echo "========================================"

# Parametros del kernel (utiles si se corre CrateDB/Elasticsearch en el host)
echo "[1/5] Configuring system parameters..."
sudo sysctl -w vm.max_map_count=262144

# Start all services
echo "[2/5] Starting Docker services..."
docker compose up -d --build

# Wait for Orion to be healthy
echo "[3/5] Waiting for Orion Context Broker..."
for i in $(seq 1 30); do
    if curl -s http://localhost:1026/version > /dev/null 2>&1; then
        echo "Orion is ready!"
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 5
done

# Apply pending migrations (db/init only runs on an empty volume)
echo "[4/5] Applying database migrations..."
bash scripts/migrate.sh

# Seed entity / subscriptions
echo "[5/5] Initializing broker entities..."
bash scripts/init_subscriptions.sh

echo ""
echo "========================================"
echo "  ✅ Platform Ready!"
echo "========================================"
echo "  Orion:        http://localhost:1026"
echo "  IoT Agent:    http://localhost:5000  (POST firmados a /api/plantas)"
echo "  Agente IA:    http://localhost:5050  (/api/agente/health)"
echo "  Frontend:     http://localhost:8080"
echo "  Grafana:      http://127.0.0.1:3000 (GF_ADMIN_USER / GF_ADMIN_PASSWORD de .env)"
echo "  Alertas:      docker logs -f alert-agent  (Telegram: TELEGRAM_BOT_TOKEN en .env)"
echo "========================================"
echo "  API key de Gemini: pegarla en .env -> GEMINI_API_KEY"
echo "========================================"
