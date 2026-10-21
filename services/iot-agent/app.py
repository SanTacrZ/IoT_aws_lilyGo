import hashlib
import hmac
import os
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, g, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

ORION_URL = f"http://{os.getenv('ORION_HOST', 'orion')}:{os.getenv('ORION_PORT', '1026')}"
DB_URL = os.getenv('DB_URL', "postgresql://iot:iot-local@postgres:5432/iot")

# Seguridad del canal dispositivo -> servidor.
# Mismo esquema que el POC de AWS: X-Api-Key + HMAC-SHA256 sobre "timestamp.body".
# Claves autorizadas. DEVICE_API_KEYS permite una por dispositivo: si filtran
# una, el resto sigue a salvo. DEVICE_API_KEY es el caso de un solo nodo.
DEVICE_API_KEYS = [k.strip() for k in os.getenv("DEVICE_API_KEYS", "").split(",") if k.strip()]
if not DEVICE_API_KEYS:
    _legacy = os.getenv("DEVICE_API_KEY", "")
    DEVICE_API_KEYS = [_legacy] if _legacy else []
RATE_LIMIT_IP_POR_MIN = int(os.getenv("RATE_LIMIT_IP_POR_MIN", "30"))  # anti-inundacion
HMAC_SECRET = os.getenv("HMAC_SECRET", "")
MAX_SKEW_S = int(os.getenv("MAX_SKEW_S", "300"))                   # ventana anti-replay 5 min
RATE_LIMIT_POR_MIN = int(os.getenv("RATE_LIMIT_POR_MIN", "12"))    # el firmware envia cada 5 min


def leer_credenciales(texto):
    """DEVICE_CREDENTIALS = "id:api_key:hmac_secret,id2:...". Devuelve {api_key: (id, secreto)}.

    Con credenciales por dispositivo cada clave queda atada a su nodo: robar la
    flash de uno no permite firmar lecturas a nombre de los demas.
    """
    creds = {}
    for item in texto.split(","):
        partes = item.strip().split(":")
        if len(partes) == 3 and all(partes):
            creds[partes[1]] = (partes[0], partes[2])
    return creds


DEVICE_CREDENTIALS = leer_credenciales(os.getenv("DEVICE_CREDENTIALS", ""))
# Clave para los GET (dashboard). Separada de las de los nodos: quien lee no firma.
READ_API_KEY = os.getenv("READ_API_KEY", "")
# Firma v1 = HMAC(timestamp.body): no cubre la URL, asi que un POST capturado se
# puede reenviar cambiando ?dispositivo=. Solo para la transicion del firmware viejo.
FIRMA_V1_PERMITIDA = os.getenv("FIRMA_V1_PERMITIDA", "0") == "1"

_hits = defaultdict(list)
_hits_lock = threading.Lock()


def _coincide(enviada, claves):
    """compare_digest contra todas: tiempo constante y sin filtrar cual fallo."""
    hallada = None
    for k in claves:
        if hmac.compare_digest(enviada, k):
            hallada = k
    return hallada


def verificar_api_key():
    """Solo X-Api-Key, para endpoints de lectura (GET), donde no hay body que firmar."""
    claves = [READ_API_KEY] if READ_API_KEY else DEVICE_API_KEYS + list(DEVICE_CREDENTIALS)
    if not claves:
        return True, "auth no configurado (abierto)"
    if _coincide(request.headers.get("X-Api-Key", ""), claves) is None:
        return False, "api-key invalida"
    return True, "ok"


def credencial_de(api_key):
    """(dispositivo, secreto) de la clave o None. dispositivo=None es el modo
    heredado de clave global, que no ata la clave a ningun nodo."""
    if DEVICE_CREDENTIALS:
        k = _coincide(api_key, DEVICE_CREDENTIALS)
        return DEVICE_CREDENTIALS[k] if k is not None else None
    if HMAC_SECRET and _coincide(api_key, DEVICE_API_KEYS) is not None:
        return (None, HMAC_SECRET)
    return None


def recurso_firmado():
    """Ruta + query tal como llegaron: la firma v2 los cubre."""
    qs = request.query_string.decode()
    return request.path + ("?" + qs if qs else "")


def mensaje_a_firmar(ts, metodo, recurso, body, version):
    if version == "2":
        return f"{ts}.{metodo}.{recurso}.".encode() + body
    return ts.encode() + b"." + body


def verificar_firma(payload: bytes):
    """X-Api-Key + X-Signature = HMAC-SHA256 del mensaje de la version de firma.

    v2: "timestamp.METODO.ruta?query.body" (X-Firma-Version: 2).
    Devuelve (ok, motivo, dispositivo_atado).
    """
    if not DEVICE_CREDENTIALS and not (DEVICE_API_KEYS and HMAC_SECRET):
        return False, "auth del servidor no configurado (DEVICE_CREDENTIALS)", None
    cred = credencial_de(request.headers.get("X-Api-Key", ""))
    if cred is None:
        return False, "api-key invalida", None
    dispositivo, secreto = cred
    version = request.headers.get("X-Firma-Version", "1")
    if version != "2" and not FIRMA_V1_PERMITIDA:
        return False, "firma v1 deshabilitada: actualiza el firmware a la firma v2", None
    ts = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")
    try:
        skew = abs(time.time() - int(ts))
    except ValueError:
        return False, "timestamp invalido", None
    if skew > MAX_SKEW_S:
        return False, f"timestamp expirado ({int(skew)}s, replay?)", None
    mensaje = mensaje_a_firmar(ts, request.method, recurso_firmado(), payload, version)
    esperado = hmac.new(secreto.encode(), mensaje, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperado, sig):
        return False, "firma HMAC invalida", None
    return True, "ok", dispositivo


def rate_limit_excedido(clave, limite=None):
    """Ventana deslizante de 1 minuto (anti-flood)."""
    ahora = time.time()
    limite = limite or RATE_LIMIT_POR_MIN
    with _hits_lock:
        marcas = [t for t in _hits[clave] if ahora - t < 60]
        marcas.append(ahora)
        _hits[clave] = marcas
        # Limpia claves que ya noogonen actividad.
        if len(_hits) > 4096:
            for vieja in [k for k, v in _hits.items() if not v or ahora - v[-1] > 300]:
                del _hits[vieja]
        return len(marcas) > limite


def exigir_firma():
    """before_request: los POST van firmados y con rate limit.

    El limite por IP va ANTES de autenticar: si no, alguien podria inundar el
    log con peticiones sin clave (cada una genera un warning).
    """
    if request.method != "POST":
        return None
    if rate_limit_excedido(f"ip:{request.remote_addr}", RATE_LIMIT_IP_POR_MIN):
        logger.warning("Rate limit por IP excedido (%s)", request.remote_addr)
        return jsonify({"error": f"rate limit: max {RATE_LIMIT_IP_POR_MIN} POST/min por IP"}), 429
    ok, motivo, atado = verificar_firma(request.get_data())
    if not ok:
        logger.warning("Auth rechazado en %s: %s", request.path, motivo)
        return jsonify({"error": motivo}), 401
    pedido = request.args.get("dispositivo")
    if atado and pedido and pedido != atado:
        logger.warning("Clave de %s usada para publicar como %s", atado, pedido)
        return jsonify({"error": "la clave no pertenece a ese dispositivo"}), 403
    g.dispositivo = atado or pedido
    # Por dispositivo (o por hash de la clave en modo heredado): un prefijo de 8
    # caracteres podia juntar claves distintas en el mismo contador.
    clave_rl = atado or hashlib.sha256(request.headers.get("X-Api-Key", "").encode()).hexdigest()[:16]
    if rate_limit_excedido(f"disp:{clave_rl}"):
        logger.warning("Rate limit por clave excedido")
        return jsonify({"error": f"rate limit: max {RATE_LIMIT_POR_MIN} POST/min"}), 429
    return None


app.before_request(exigir_firma)

# Campos que el firmware LilyGo T-Beam envia en /api/plantas.
CAMPOS_PLANTA = [
    "temperatura_aire",
    "humedad_aire",
    "luz_lux",
    "humedad_suelo_pct",
    "humedad_suelo_raw",
    "alerta_seco",
    "latitud",
    "longitud",
    "altitud",
    "satelites",
]


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "si")


def texto_para_orion(valor, maximo=280):
    """Orion-LD (API NGSI v2) responde 400 'Invalid characters in attribute value'
    si un atributo Text lleva parentesis. Se quitan solo para Orion; la lectura
    original queda intacta en TimescaleDB."""
    if valor is None:
        return ""
    limpio = str(valor).replace("(", "").replace(")", "").replace("\n", " ").strip()
    return limpio[:maximo]


# --- Orion (NGSI v2) -------------------------------------------------------

def upsert_orion_entity(entity_id, entity_type, attrs):
    """Create or update a FIWARE NGSI v2 entity in Orion.

    Los textos se sanean antes de salir: Orion-LD rechaza parentesis en atributos
    Text del API NGSI v2.
    """
    def sanear(dic):
        return {
            k: (dict(v, value=texto_para_orion(v["value"])) if v.get("type") == "Text" else v)
            for k, v in dic.items()
        }

    attrs = sanear(attrs)
    entity = {"id": entity_id, "type": entity_type}
    entity.update(attrs)
    entity["timestamp"] = {
        "value": datetime.now(timezone.utc).isoformat(),
        "type": "DateTime",
    }

    # Try PATCH first (update existing entity)
    url_patch = f"{ORION_URL}/v2/entities/{entity_id}/attrs"
    try:
        resp = requests.patch(
            url_patch, json=attrs, headers={"Content-Type": "application/json"}, timeout=5
        )
        if resp.status_code == 204:
            logger.info(f"Entity {entity_id} updated successfully")
            return True
        elif resp.status_code == 404:
            # Entity doesn't exist, create it
            url_create = f"{ORION_URL}/v2/entities"
            resp = requests.post(
                url_create, json=entity, headers={"Content-Type": "application/json"}, timeout=5
            )
            if resp.status_code == 201:
                logger.info(f"Entity {entity_id} created successfully")
                return True
            logger.error(f"Failed to create entity: {resp.status_code} {resp.text}")
            return False
        logger.error(f"Failed to update entity: {resp.status_code} {resp.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Connection error to Orion: {e}")
        return False


# --- TimescaleDB -----------------------------------------------------------

def time_index_de_lectura(lectura_id):
    """Hora de la lectura segun el dispositivo, si es coherente.

    Usar la hora del nodo (y no NOW()) es mejor ETL y ademas permite la
    deduplicacion: en un reintento llegan el mismo tiempo y el mismo
    lectura_id. Si el reloj del nodo no esta sincronizado, se usa la del
    servidor.
    """
    if not lectura_id:
        return datetime.now(timezone.utc)
    try:
        epoch = int(float(lectura_id))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if epoch < 1_000_000_000:                       # 1970: reloj sin sincronizar
        return datetime.now(timezone.utc)
    momento = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if abs((datetime.now(timezone.utc) - momento).total_seconds()) > 300:
        return datetime.now(timezone.utc)
    return momento


def guardar_lectura(entity_id, entity_type, dispositivo, payload, lectura_id=None):
    """Persiste la lectura en la hypertable. Best effort: nunca tumba el ingest.

    Idempotente por (time_index, entity_id, lectura_id): si el nodo reintenta el
    mismo POST (timeout de Wi-Fi, por ejemplo) la segunda fila se descarta.
    Devuelve (ok, insertada).
    """
    momento = time_index_de_lectura(lectura_id)
    try:
        with psycopg2.connect(DB_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO lecturas
                        (time_index, entity_id, entity_type, dispositivo, lectura_id, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (time_index, entity_id, lectura_id)
                    WHERE lectura_id IS NOT NULL
                    DO NOTHING
                    """,
                    (momento, entity_id, entity_type, dispositivo, lectura_id,
                     psycopg2.extras.Json(payload)),
                )
                return True, cur.rowcount > 0
    except psycopg2.Error as e:
        logger.error(f"Error guardando lectura en TimescaleDB: {e}")
        return False, False


def leer_lecturas(limite=50, entidad=None):
    try:
        with psycopg2.connect(DB_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if entidad:
                    cur.execute(
                        """
                        SELECT time_index, entity_id, dispositivo, payload
                        FROM lecturas
                        WHERE entity_id = %s
                        ORDER BY time_index DESC
                        LIMIT %s
                        """,
                        (entidad, limite),
                    )
                else:
                    cur.execute(
                        """
                        SELECT time_index, entity_id, dispositivo, payload
                        FROM lecturas
                        ORDER BY time_index DESC
                        LIMIT %s
                        """,
                        (limite,),
                    )
                return [
                    {
                        "time_index": row[0].isoformat(),
                        "entity_id": row[1],
                        "dispositivo": row[2],
                        **row[3],
                    }
                    for row in cur.fetchall()
                ]
    except psycopg2.Error as e:
        logger.error(f"Error leyendo de TimescaleDB: {e}")
        return []


# --- Estado de los nodos ---------------------------------------------------

def estado_nodos(minutos_considerados=1440):
    """Un registro por dispositivo: si esta vivo, cuando hablo, y como esta.

    Todo se saca de la propia fila que manda el nodo (uptime_s, heap_libre,
    wifi_rssi, arranques...), asi que no hace falta ningun agente extra.
    """
    with psycopg2.connect(DB_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH ult AS (
                    SELECT DISTINCT ON (dispositivo)
                           dispositivo, entity_id, time_index, payload
                    FROM lecturas
                    WHERE time_index >= NOW() - (%s || ' minutes')::interval
                      AND dispositivo IS NOT NULL
                    ORDER BY dispositivo, time_index DESC
                )
                SELECT
                    dispositivo,
                    entity_id,
                    time_index,
                    EXTRACT(EPOCH FROM (NOW() - time_index))::int AS segundos_sin_datos,
                    (payload->>'temperatura_aire')::float,
                    (payload->>'humedad_aire')::float,
                    (payload->>'luz_lux')::float,
                    (payload->>'humedad_suelo_pct')::float,
                    (payload->>'alerta_seco')::boolean,
                    (payload->>'satelites')::int,
                    (payload->>'uptime_s')::bigint,
                    (payload->>'heap_libre')::int,
                    (payload->>'wifi_rssi')::int,
                    (payload->>'arranques')::bigint,
                    (payload->>'pendientes')::int,
                    (payload->>'reinicia_en_s')::bigint,
                    (payload->>'lectura_id')::text
                FROM ult
                ORDER BY time_index DESC
                """,
                (minutos_considerados,),
            )
            filas = cur.fetchall()

    nodos = []
    for f in filas:
        (disp, entity_id, ts, sin_datos, temp, hum, lux, suelo, seco, sats,
         uptime, heap, rssi, arranques, pendientes, reinicia, lectura_id) = f
        nodos.append({
            "dispositivo": disp,
            "entity_id": entity_id,
            "ultima_lectura": ts.isoformat(),
            "segundos_sin_datos": sin_datos,
            "en_linea": sin_datos is not None and sin_datos < 300,
            "lectura_id": lectura_id,
            "sensores": {
                "temperatura_aire": temp, "humedad_aire": hum, "luz_lux": lux,
                "humedad_suelo_pct": suelo, "alerta_seco": seco, "satelites": sats,
            },
            "nodo": {
                "uptime_s": uptime, "heap_libre": heap, "wifi_rssi": rssi,
                "arranques": arranques, "pendientes": pendientes,
                "reinicia_en_s": reinicia,
            },
        })
    return nodos


def reinicios_detectados(dispositivo, horas=24):
    """Reinicios que el propio nodo reporta (su contador arranques)."""
    with psycopg2.connect(DB_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT (payload->>'arranques')::int AS arranque,
                       min(time_index), max(time_index), count(*)
                FROM lecturas
                WHERE dispositivo = %s
                  AND payload ? 'arranques'
                  AND time_index >= NOW() - (%s || ' hours')::interval
                GROUP BY 1 ORDER BY 1
                """,
                (dispositivo, horas),
            )
            return [{"arranque": r[0], "primera": r[1].isoformat(),
                     "ultima": r[2].isoformat(), "lecturas": r[3]} for r in cur.fetchall()]


# --- API -------------------------------------------------------------------

@app.route("/api/plantas", methods=["POST"])
def receive_planta():
    """Recibe el JSON que manda el firmware LilyGo T-Beam (AP3216 + HDC1080 + suelo + GPS).

    El payload contiene valores medidos por los sensores de aire, luz, suelo y GPS.
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No JSON data received"}), 400

    faltantes = [c for c in CAMPOS_PLANTA if c not in data]
    if faltantes:
        return jsonify({"error": f"Campos faltantes: {', '.join(faltantes)}"}), 400

    dispositivo = g.dispositivo or "lilygo-tbeam-01"
    entity_id = f"planta:{dispositivo}"

    lat = to_float(data["latitud"])
    lng = to_float(data["longitud"])

    attrs = {
        "temperatura_aire": {"value": round(to_float(data["temperatura_aire"]), 2), "type": "Float"},
        "humedad_aire": {"value": round(to_float(data["humedad_aire"]), 2), "type": "Float"},
        "luz_lux": {"value": round(to_float(data["luz_lux"]), 2), "type": "Float"},
        "humedad_suelo_pct": {"value": round(to_float(data["humedad_suelo_pct"])), "type": "Float"},
        "humedad_suelo_raw": {"value": int(to_float(data["humedad_suelo_raw"])), "type": "Integer"},
        "alerta_seco": {"value": to_bool(data["alerta_seco"]), "type": "Boolean"},
        "altitud": {"value": round(to_float(data["altitud"]), 2), "type": "Float"},
        "satelites": {"value": int(to_float(data["satelites"])), "type": "Integer"},
    }
    if "luz_pct" in data:
        attrs["luz_pct"] = {"value": round(to_float(data["luz_pct"])), "type": "Float"}
    if data["satelites"] and (lat or lng):
        attrs["location"] = {
            "type": "geojson",
            "value": {"type": "Point", "coordinates": [lng, lat]},
        }

    logger.info(
        "T=%s H=%s lux=%s suelo=%s%% seco=%s gps=%s,%s",
        data["temperatura_aire"],
        data["humedad_aire"],
        data["luz_lux"],
        data["humedad_suelo_pct"],
        data["alerta_seco"],
        lat,
        lng,
    )

    if not upsert_orion_entity(entity_id, "PlantSensor", attrs):
        return jsonify({"error": "Failed to update Orion"}), 502

    # lectura_id hace idempotente la carga: el nodo manda un id por lectura y si
    # reintenta el POST no se duplica la fila.
    lectura_id = request.args.get("lectura_id") or data.get("lectura_id")
    if lectura_id is not None and not str(lectura_id).isdigit():
        # El indice unico de la hypertable incluye time_index, que se deriva del
        # propio id; un id no numerico haria la deduplicacion silenciosamente
        # inutil, asi que se rechaza en vez de aceptarlo a medias.
        return jsonify({"error": "lectura_id debe ser un epoch en segundos (entero)"}), 400
    _, insertada = guardar_lectura(entity_id, "PlantSensor", dispositivo, data, lectura_id)

    return jsonify(
        {
            "status": "ok",
            "entity": entity_id,
            "orion": "actualizada",
            "historial": "timescaledb",
            "insertada": insertada,
        }
    ), 200


@app.route("/api/v1/data", methods=["POST"])
def receive_data():
    """Receive sensor data from ESP32 Gateway and forward to Orion."""
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No JSON data received"}), 400

        sensor_id = data.get("id")
        if not sensor_id:
            return jsonify({"error": "Missing 'id' field"}), 400
        if g.dispositivo and sensor_id != g.dispositivo:
            return jsonify({"error": "la clave no pertenece a ese dispositivo"}), 403

        required_fields = ["t", "h", "s", "p"]
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing '{field}' field"}), 400

        logger.info(f"Received data from {sensor_id}: T={data['t']} H={data['h']} S={data['s']} pH={data['p']}")

        entity_id = f"sensor:{sensor_id}"
        attrs = {
            "temperatura": {"value": round(data.get("t", 0), 2), "type": "Float"},
            "humedad": {"value": round(data.get("h", 0), 2), "type": "Float"},
            "radiacion_solar": {"value": round(data.get("s", 0), 2), "type": "Float"},
            "ph": {"value": round(data.get("p", 0), 2), "type": "Float"},
        }

        success = upsert_orion_entity(entity_id, "AmbientSensor", attrs)

        if success:
            guardar_lectura(entity_id, "AmbientSensor", sensor_id, data,
                            request.args.get("lectura_id"))
            return jsonify({"status": "ok", "entity": entity_id}), 200
        return jsonify({"error": "Failed to update Orion"}), 502
    except Exception as e:
        # El detalle va al log, no al cliente: la excepcion puede revelar internals.
        logger.exception(f"Error processing data: {e}")
        return jsonify({"error": "error interno"}), 500


@app.route("/api/v1/lecturas", methods=["GET"])
def get_lecturas():
    """Ultimas lecturas del historial (TimescaleDB). Requiere X-Api-Key."""
    ok, motivo = verificar_api_key()
    if not ok:
        return jsonify({"error": motivo}), 401
    limite = min(int(request.args.get("limite", 50)), 500)
    entidad = request.args.get("entidad")
    return jsonify({"lecturas": leer_lecturas(limite, entidad)}), 200


@app.route("/api/nodos", methods=["GET"])
def get_nodos():
    """Estado de cada dispositivo: vivo/caido, sensores y salud del equipo."""
    ok, motivo = verificar_api_key()
    if not ok:
        return jsonify({"error": motivo}), 401
    return jsonify({
        "nodos": estado_nodos(int(request.args.get("minutos", 1440))),
        "generado": datetime.now(timezone.utc).isoformat(),
    }), 200


@app.route("/api/nodos/<dispositivo>/reinicios", methods=["GET"])
def get_reinicios(dispositivo):
    """Reinicios declarados por el nodo en las ultimas horas."""
    ok, motivo = verificar_api_key()
    if not ok:
        return jsonify({"error": motivo}), 401
    return jsonify({
        "dispositivo": dispositivo,
        "reinicios": reinicios_detectados(dispositivo, int(request.args.get("horas", 24))),
    }), 200


@app.route("/api/v1/health", methods=["GET"])
def health():
    """Health check endpoint."""
    try:
        resp = requests.get(f"{ORION_URL}/version", timeout=3)
        orion_ok = resp.status_code == 200
    except Exception:
        orion_ok = False

    try:
        with psycopg2.connect(DB_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM lecturas LIMIT 1")
                cur.fetchall()
        db_ok = True
    except psycopg2.Error:
        db_ok = False

    return jsonify(
        {
            "status": "healthy",
            "orion_connected": orion_ok,
            "orion_url": ORION_URL,
            "timescaledb_connected": db_ok,
            "auth_hmac_activo": bool(DEVICE_CREDENTIALS or (DEVICE_API_KEYS and HMAC_SECRET)),
            "credenciales_por_dispositivo": len(DEVICE_CREDENTIALS),
            "claves_autorizadas": len(DEVICE_CREDENTIALS) or len(DEVICE_API_KEYS),
            "firma_v1_permitida": FIRMA_V1_PERMITIDA,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ), 200


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "service": "IoT Agent",
            "version": "2.1.0",
            "endpoints": ["/api/plantas", "/api/v1/data", "/api/v1/lecturas", "/api/v1/health"],
            "auth": "X-Api-Key + X-Timestamp + X-Firma-Version: 2 + "
                    "X-Signature (HMAC-SHA256 de timestamp.METODO.ruta?query.body)",
        }
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
