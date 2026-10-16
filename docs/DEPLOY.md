# Guia de operacion — IoT FIWARE + Agente IA

Plataforma de monitoreo de cultivo: nodo **LilyGo T-Beam** (AP3216 + HDC1080 + humedad de
suelo + GPS) → **IoT Agent** (firmado) → **Orion-LD** + **PostgreSQL/TimescaleDB** →
**Dashboard Dash** y **Grafana**, con un **agente de IA (Gemini)** que vigila el cultivo.

## 1. Arquitectura

```
LilyGo zona 1 ─┐
LilyGo zona 2 ─┼─ Wi-Fi/router de la finca ─▶ POST firmado ─▶ gateway TLS opcional
LilyGo zona N ─┘                                                     │
                                                                    ▼
                                              iot-agent :5000
                                              ├─ publica entidad NGSI v2 en Orion
                                              └─ inserta lectura en TimescaleDB
                                                                 │
                        ┌────────────────────────────────────────┼───────────────────────┐
                        ▼                                        ▼                       ▼
             orion-ld :1026 (contexto)                 PostgreSQL/TimescaleDB      Agente IA
             metadatos en mongo-db :27017              hypertable `lecturas`        ai-agent :5050
                                                       hypertable cagg 1 min         ai-scheduler
                                                       vista `lecturas_planta`       (analiza cada 15 min)
                                                       `analisis_ia`                      │
                        │                                        │                  publica en Orion
                        └────────────────┬───────────────────────┘                  y en la web
                                         ▼
                              frontend :8080 (Dash) · grafana :3000
```

Base de datos: **una sola familia, PostgreSQL 16 + TimescaleDB**. Orion-LD es el broker
(guarda el estado actual de cada entidad) y la hypertable `lecturas` guarda el historial.
MongoDB queda unicamente como almacenamiento de metadatos del broker.

| Servicio | Puerto | Que expone |
|---|---|---|
| Orion-LD | 1026 | API NGSI v2 / NGSI-LD |
| iot-agent | 5000 | `POST /api/plantas`, `POST /api/v1/data`, `GET /api/v1/lecturas`, `GET /api/v1/health` |
| ai-agent | 5050 | `GET /api/agente/ultimo`, `/historial`, `POST /analizar`, `/health` |
| frontend | 8080 | dashboard Dash (`/dashboard/`) |
| grafana | 3000 | tableros (admin/admin) |
| postgres | 5432 | TimescaleDB (iot / iot-local) |
| gateway (perfil tls) | 80/443 | HTTPS con certificado automatico |

## 2. Puesta en marcha

```bash
cp .env.example .env      # y rellenalo (ver seccion 3)
bash scripts/setup.sh     # levanta todo + migraciones + entidades base
```

Verificacion rapida:

```bash
curl -s http://localhost:5000/api/v1/health     # orion + timescaledb + auth
curl -s http://localhost:5050/api/agente/health  # agente IA (gemini_configurado)
curl -s http://localhost:8080/dashboard/         # dashboard web
```

## 3. Donde pegar la API key de Gemini

En **`.env`** (un solo lugar, nunca en el codigo):

```bash
# >>> PEGAR AQUÍ TU API KEY DE GEMINI <<<
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACKS=gemini-3.6-flash,gemini-3.5-flash,gemini-3.7-flash,gemini-flash-latest
GEMINI_REINTENTOS=2
AGENT_INTERVAL_MIN=15
AGENT_VENTANA_MIN=180     # ventana que mira hacia atras en cada analisis
AGENT_SILENCIO_MIN=30     # a partir de aqui se considera sensor caido
```

Luego `docker compose up -d ai-agent ai-scheduler` (o `bash scripts/deploy.sh`).

* Si `GEMINI_API_KEY` esta vacia el agente **no se cae**: entra en modo degradado con
  reglas locales sobre los umbrales (suelo < 30% = critico, < 45% = atencion) y lo dice
  en `/api/agente/health` (`modelo: reglas-locales`).
* **Modelos**: Google retira modelos (`404 "no longer available"`) y devuelve picos de
  demanda (`429`/demasiada carga). Por eso hay cadena de fallback con reintentos y
  backoff: `404/400` cambia de modelo, `429/5xx` reintenta y luego cambia, y un
  `401/403` (key invalida) corta de inmediato sin girar modelos.
* **Cuota agotada** (`429 "You exceeded your current quota"`): la cuota es del proyecto,
  no del modelo, asi que el agente corta al instante (no insiste ni gira modelos) y aplica
  el veredicto local, dejando el error en `analisis_ia.error` y en Orion. El estado sale en
  segundos en vez de agotar minutos reintentando. Se resuelve revisando el plan y los limites
  en <https://ai.google.dev>; el plan gratuito se reinicia a diario y las cuotas por minuto
  son bajas, asi que `AGENT_INTERVAL_MIN` debe quedar holgado (15 min va bien).
* Para ver que modelos acepta hoy tu key: `GET .../v1beta/models?key=$GEMINI_API_KEY`.
* El agente manda **solo numeros** a Gemini (un resumen agregado), nunca el payload crudo:
  asi un sensor manipulado no puede inyectar instrucciones en el prompt. La respuesta se
  valida contra un JSON Schema, se recorta y se sanea antes de guardarla.
* La key viaja por variable de entorno; `.env` esta en `.gitignore`.

## 4. Seguridad del envio sensor → servidor

Ningun POST se acepta sin firmar. Mismo esquema que el POC de AWS:

| Cabecera | Valor |
|---|---|
| `X-Api-Key` | `DEVICE_API_KEY` del `.env` |
| `X-Timestamp` | epoch en segundos |
| `X-Signature` | `HMAC-SHA256(timestamp + "." + body, HMAC_SECRET)` en hex |

* **Anti-replay**: si el timestamp se aparta mas de `MAX_SKEW_S` (300 s) se rechaza.
* **Integridad**: la firma cubre el body, asi que alterarlo rompe la verificacion.
* **Anti-flood**: `RATE_LIMIT_POR_MIN` (12) POST/min por clave de dispositivo y
  `RATE_LIMIT_IP_POR_MIN` (30) por IP. El límite por IP se aplica **antes** de
  autenticar; si no, bastarían peticiones sin clave para inundar el log.
* **Clave por dispositivo**: `DEVICE_API_KEYS` acepta una lista de claves separadas
  por coma. Si un nodo se compromete, el resto sigue operativo.
* **Idempotencia**: el nodo manda `lectura_id` (epoch en segundos) y el servidor
  deduce de ahí el `time_index`. Un reintento del mismo POST no duplica la fila
  (`UNIQUE` parcial + `ON CONFLICT DO NOTHING`). Un `lectura_id` no numérico se
  rechaza con 400, porque haría la deduplicación inútil en silencio.
* El firmware LilyGo usa los mismos valores (`DEVICE_API_KEY`, `HMAC_SECRET`)
  configurados en `src/secrets.h` y en el `.env` del servidor.
* El `iot-agent` corre con **un solo worker**: el limitador vive en memoria del
  proceso, y con varios workers el límite efectivo se multiplica.
* **Lo que el HMAC no protege**: la confidencialidad. Sin TLS, quien capture el
  tráfico del Wi-Fi lee todas las lecturas. Activa el perfil `tls`.

Probar la defensa:

```bash
curl -s -X POST http://localhost:5000/api/plantas \
  -H 'Content-Type: application/json' -d '{}'
# -> {"error":"api-key invalida"}
```

### TLS

Con el perfil `tls` se levanta Caddy delante del agente, que pide el certificado a
Let's Encrypt solo (define `DOMAIN` en `.env` y abre 80/443):

```bash
docker compose --profile tls up -d
```

Y en el firmware: `URL_SERVIDOR = "https://TU-DOMINIO/api/plantas"`.
El firmware necesita hora exacta para el timestamp: `configTime` con NTP (el codigo ya
llama a `sincronizarReloj()`); sin NTP el servidor rechaza por anti-replay.

## 5. Rollback sin perder datos

Regla de oro: **los datos viven en volumenes con nombre** (`postgres-data`, `mongo-db`,
`grafana`) y ni `deploy` ni `rollback` los tocan. Para volver atras el *codigo* se
reconstruye el contenedor; para volver atras el *esquema* se restaura un backup.

```bash
bash scripts/deploy.sh              # backup + build + healthcheck + rollback automatico
bash scripts/rollback.sh            # vuelve a la version anterior de imagenes
bash scripts/backup.sh              # PostgreSQL + Mongo a backups/<fecha>/
bash scripts/restore.sh backups/AAAmmdd-HHMMSS
bash scripts/migrate.sh             # aplica db/init + db/migrations (idempotente)
```

Como funciona el rollback automatico:

1. `deploy.sh` guarda un `pg_dump`/CSV de los datos.
2. Snapshot de las imagenes **por digest** en `deploy/previous.lock`.
3. Cada build recibe un tag propio (`APP_VERSION=AAAmmdd-HHMMSS`), asi la version
   anterior sigue existiendo en disco.
4. Si el build falla o algun contenedor no queda `healthy` en `HEALTH_TIMEOUT`, reetiqueta
   las imagenes anteriores y recrea los contenedores. Los volumenes no se tocan.

Probado: desplegando una version rota, `deploy.sh` detecto el fallo y revirtio solo,
conservando las lecturas y los analisis del agente.

Notas:

* Las imagenes de terceros van pineadas (`grafana/grafana:11.5.2`, `caddy:2.8-alpine`).
* `db/init/*.sql` solo corre cuando el volumen de PostgreSQL esta vacio; para el resto de
  los casos esta `scripts/migrate.sh`, que es idempotente.
* Los datos de la hypertable se respaldan con `COPY ... TO STDOUT` en CSV, **no** con
  `pg_dump -t`: pg_dump ve la tabla padre vacia porque las filas viven en los chunks.
* `analisis_ia` se restaura con sus ids; el script resincroniza la secuencia.

## 6. Nodos LilyGo por Wi-Fi

Cada LilyGo T-Beam usa el firmware real de `firmware/lilygo_planta/src/main.cpp`.
Todos se conectan al router Wi-Fi de la finca y publican al IoT Agent. No hace
falta un broker MQTT en una placa LilyGo: Orion-LD mantiene el estado actual y
TimescaleDB conserva las lecturas históricas.

```bash
cd firmware/lilygo_planta
cp -n src/secrets.h.example src/secrets.h
# Pon WIFI_SSID y WIFI_PASSWORD de la red normal 2.4 GHz en src/secrets.h.
# Configura URL_SERVIDOR y un DEVICE_ID único por placa en src/main.cpp.
# Completa en src/secrets.h DEVICE_API_KEY y HMAC_SECRET del servidor.
pio run
pio device list
pio run -t upload --upload-port /dev/ttyUSB0
pio device monitor --port /dev/ttyUSB0
```

Para subir el firmware, primero identifica el puerto USB con `pio device list` y
úsalo explícitamente en la carga y el monitor; puede llamarse `/dev/ttyACM0` o
`/dev/ttyUSB0`.
No selecciones `gateway_rx`: el único proyecto de placa activo es
`firmware/lilygo_planta`.

El nodo toma lecturas según `INTERVALO_LECTURA_S` y las envía firmadas al
`iot-agent`. El backend crea una entidad `planta:<DEVICE_ID>` y guarda el
historial en TimescaleDB. Usa una API key diferente por placa y añádelas a
`DEVICE_API_KEYS`. Con una lectura cada 60 s, el límite actual por IP permite
hasta 30 nodos; para más, ajusta `RATE_LIMIT_IP_POR_MIN` según
`ceil(60 × cantidad_de_nodos / intervalo_en_segundos)`, con margen. Los
ejemplos LoRa antiguos se guardaron fuera de `iot_final`, en
`../archivo_legado/lora_practicas`, y no forman parte del sistema activo.

## 7. Despliegue en AWS

1. EC2 Ubuntu 22.04, `t2.medium` o mayor. En el security group abre **solo**
   `22`, `80`, `443` y `5000` (la entrada de los sensores). El dashboard, si lo
   quieres desde fuera, publícalo por el perfil `tls` (443), nunca el 8080 pelado.
2. Instalar Docker + plugin compose (ver el procedimiento estandar de Docker).
3. Subir el proyecto y `cp .env.example .env`; generar secretos:

   ```bash
   python3 -c "import secrets;print(secrets.token_hex(32))"   # para DEVICE_API_KEY y HMAC_SECRET
   ```

4. `bash scripts/deploy.sh` y, con dominio propio, `docker compose --profile tls up -d`.
5. `crontab -e` para respaldos diarios:

   ```cron
   30 2 * * * cd /ruta/iot_final && bash scripts/backup.sh
   ```
