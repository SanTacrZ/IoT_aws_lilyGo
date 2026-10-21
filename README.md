# IoT FIWARE + Agente IA — monitoreo de cultivo

Varios nodos **LilyGo T-Beam** (AP3216 + HDC1080 + humedad de suelo + GPS) que reportan por HTTP
firmado a través del Wi-Fi de la finca al **IoT Agent**, con el historial en **PostgreSQL + TimescaleDB**,
un **agente de IA (Gemini)** que vigila el cultivo, **alertas por Telegram / WhatsApp / SMS**
con bot de consulta, y un dashboard web.

## Puesta en marcha

```bash
cp .env.example .env      # pega la API key de Gemini y genera los secretos
bash scripts/setup.sh     # levanta todo, aplica el esquema y crea las entidades
```

| Servicio | URL | Qué es |
|---|---|---|
| Dashboard | http://localhost:8080/dashboard/ | lecturas + veredicto del agente IA (formulario de acceso) |
| IoT Agent | http://localhost:5000 | recibe el POST firmado del nodo (único puerto en la red) |
| Grafana | http://127.0.0.1:3000 | series largas, solo local (túnel SSH desde fuera) |
| alert-agent | sin puertos | alertas con estado + bot de Telegram (solo conexiones salientes) |
| Agente IA | 127.0.0.1:5050 | dictamen del cultivo, solo local |
| Orion-LD | 127.0.0.1:1026 | contexto de las entidades, solo local |
| PostgreSQL | red interna | usuario `iot`, contraseña `POSTGRES_PASSWORD` de `.env`, sin puertos publicados |

Desde el celular, abre `http://<IP-del-servidor>:8080/dashboard/` e ingresa
`WEB_USER` y `WEB_PASSWORD` del archivo `.env` en el formulario de acceso.

## Cómo circula un dato

```
LilyGo zona 1 ──┐
LilyGo zona 2 ──┼─ Wi-Fi/router ──POST firmado──▶ iot-agent :5000
LilyGo zona N ──┘                                  ├─▶ Orion-LD        (estado por nodo)
                                                   └─▶ TimescaleDB     (historial)
                                                              │
   Agente IA (cada 15 min, por zona) ──▶ Gemini + pronóstico ─┤
                                                              ▼
   alert-agent (cada 60 s) ──▶ Telegram · WhatsApp · SMS     dashboard web · Grafana
```

## Estructura

```
iot_final/
├── README.md                     este archivo
├── docker-compose.yml            orion, mongo, postgres, iot-agent, ai-agent,
│                                 ai-scheduler, frontend, grafana, gateway (perfil tls)
├── .env / .env.example           secretos y API keys (nunca al repo)
│
├── services/                     aplicaciones que corren en Docker
│   ├── iot-agent/                API Flask: valida la firma, publica en Orion,
│   │                             guarda en TimescaleDB
│   ├── alert-agent/              alertas con estado y bot de Telegram
│   │   ├── reglas.py               condiciones (puras, con histéresis)
│   │   ├── motor.py                abrir → recordar → escalar → resolver
│   │   ├── notificadores.py        Telegram, WhatsApp Cloud API, SMS Twilio
│   │   └── bot.py                  /estado /alertas /ack
│   ├── ai-agent/                 agente IA: resume, pregunta a Gemini, publica
│   │   ├── app.py                  el veredicto y lo deja en analisis_ia
│   │   └── scheduler.py            ciclo periódico (contenedor aparte)
│   └── frontend/                 dashboard Dash (tema oscuro)
│       └── assets/dark.css        colores del tema, fuera del código
│
├── firmware/                     sketches del hardware
│   └── lilygo_planta/            único firmware activo: T-Beam por Wi-Fi
│       ├── src/main.cpp           sensores + GPS + POST firmado
│       ├── src/secrets.h.example  plantilla de credenciales del nodo
│       └── platformio.ini         configuración de compilación
│
├── db/
│   ├── init/001_init.sql         esquema: hypertable, vista, cagg, retención
│   └── migrations/002_agente_ia.sql   historial de dictámenes del agente
│
├── grafana/                      datasource PostgreSQL + tablero provisionados
├── gateway/Caddyfile             TLS (perfil docker "tls")
├── docs/DEPLOY.md                operación, seguridad, rollback y despliegue AWS
└── scripts/                      setup, deploy, rollback, backup, restore, migrate
```

## Operación

```bash
bash scripts/setup.sh       # primera vez
bash scripts/deploy.sh      # backup + build + healthcheck + rollback automático
bash scripts/rollback.sh    # volver a la imagen anterior
bash scripts/backup.sh      # PostgreSQL + Mongo a backups/<fecha>/
bash scripts/restore.sh backups/<fecha>
bash scripts/migrate.sh     # aplicar db/init + db/migrations (idempotente)
python3 scripts/nuevo_nodo.py zona-norte   # alta de un nodo con credencial propia
python3 -m pytest                          # pruebas unitarias
python3 -m pytest -m e2e -s                # prueba de punta a punta contra el stack
```

## Alertas

`alert-agent` revisa la base cada 60 s. Una alerta es un **estado**: se abre, se
recuerda cada hora mientras nadie la reconozca, se **escala por SMS** si es crítica
y sigue sin `/ack` a los 10 min, y avisa ✅ cuando se resuelve sola.

| Regla | Severidad |
|---|---|
| Ningún nodo reporta (internet/energía de la finca) | crítica, un solo aviso |
| Nodo sin datos > 15 min | crítica |
| Suelo < 30 % (cierra al superar 35 %) | crítica |
| Temperatura > 35 °C o < 5 °C | atención |
| Batería < 3.4 V · Wi-Fi < −85 dBm | atención |
| Gemini marca estado crítico | atención |

Activar Telegram: `@BotFather` → `/newbot` → token en `TELEGRAM_BOT_TOKEN`; escríbele
`/start` al bot, te responde tu chat_id → `TELEGRAM_CHAT_IDS`; luego
`docker compose up -d alert-agent`. Sin canales configurados todo queda en la
tabla `notificaciones` (modo registro).

Los datos viven en volúmenes con nombre (`postgres-data`, `mongo-db`, `grafana`): ni
`deploy` ni `rollback` los tocan, así que volver atrás nunca pierde lecturas.

## Nodo T-Beam

```bash
cd firmware/lilygo_planta
cp -n src/secrets.h.example src/secrets.h  # créalo solo si todavía no existe
pio run                                # compilar
pio device list                       # identificar el puerto USB de la placa
pio run -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyACM0
```

Abre `firmware/lilygo_planta` en VS Code para trabajar con PlatformIO. Cambia
`/dev/ttyUSB0` por el puerto que muestre `pio device list` (puede ser
`/dev/ttyACM0`).

Configura `WIFI_SSID` y `WIFI_PASSWORD` en `src/secrets.h` con la red Wi-Fi normal
de 2.4 GHz de la finca. Configura `URL_SERVIDOR` en `src/main.cpp`. Cada nodo nuevo
se da de alta con `python3 scripts/nuevo_nodo.py <id>`: genera su clave y su secreto
HMAC propios en `DEVICE_CREDENTIALS` y deja su `secrets.h` (con `DEVICE_ID_NODO`) en
`firmware/lilygo_planta/nodos/<id>/`. El sistema no genera lecturas de demostración: el historial se
llena únicamente con POST de sensores reales.

## Seguridad del envío

Firma v2: `X-Api-Key` + `X-Timestamp` + `X-Firma-Version: 2` +
`X-Signature = HMAC-SHA256(timestamp.POST.ruta?query.body)` con el secreto **del nodo**.
La firma cubre la URL (no se puede reenviar cambiando `?dispositivo=`) y cada clave solo
publica como su propio dispositivo. Ventana anti-replay de 300 s y rate limit de
12 POST/min por nodo. Detalles en `docs/DEPLOY.md`.
