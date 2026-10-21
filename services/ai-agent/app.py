"""Agente IA del cultivo: vigila las lecturas del LilyGo y pide un dictamen a Gemini.

Flujo:
  TimescaleDB (lecturas_planta) -> resumen numerico -> Gemini -> JSON validado
  -> tabla analisis_ia + entidad Orion agente:agrosense

Solo se mandan NUMEROS a Gemini, nunca el body crudo: asi ningun sensor puede
inyectar instrucciones al prompt. La respuesta se valida y se recorta antes de
guardarla o mostrarla.

Endpoints:
  GET  /api/agente/ultimo      ultimo dictamen
  GET  /api/agente/historial    ultimos dictamenes (JSON)
  POST /api/agente/analizar     fuerza un analisis ahora
  GET  /api/agente/health       estado del agente
"""
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("agente-ia")
app = Flask(__name__)

DB_URL = os.getenv("DB_URL", "postgresql://iot:iot-local@postgres:5432/iot")
ORION_URL = f"http://{os.getenv('ORION_HOST', 'orion')}:{os.getenv('ORION_PORT', '1026')}"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Google deprecia modelos y tiene picos de demanda: si el preferido falla por
# 404/429/5xx se prueba el siguiente de la lista.
GEMINI_FALLBACKS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACKS",
    "gemini-3.6-flash,gemini-3.5-flash,gemini-3.7-flash,gemini-flash-latest",
).split(",") if m.strip()]
GEMINI_REINTENTOS = int(os.getenv("GEMINI_REINTENTOS", "2"))
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
VENTANA_MIN = int(os.getenv("AGENT_VENTANA_MIN", "180"))
SILENCIO_MIN = int(os.getenv("AGENT_SILENCIO_MIN", "30"))
# Los modelos "thinking" gastan tokens razonando antes de responder: con 600 el
# JSON llegaba cortado ("Gemini no devolvio JSON: {"estado":"ok","diagnostico":"La").
MAX_TOKENS = int(os.getenv("GEMINI_MAX_TOKENS", "4096"))
# Ubicacion de respaldo para el pronostico si el nodo aun no tiene fix de GPS.
FINCA_LAT = os.getenv("FINCA_LAT", "")
FINCA_LON = os.getenv("FINCA_LON", "")

ESTADOS = ("ok", "atencion", "critico", "error")

# Disparar un analisis cuesta una llamada a Gemini: solo quien tenga la clave
# (el scheduler y un operador humano) puede pedirlo.
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")

SYSTEM_PROMPT = """Eres un agronomo que vigila un cultivo IoT en Colombia.
Te llega un resumen de sensores de una planta monitoreada. Devuelve SOLO un objeto
JSON con estas claves exactas:
{
  "estado": "ok" | "atencion" | "critico",
  "diagnostico": "1-2 frases cortas en espanol, quoting solo los numeros recibidos",
  "accion_recomendada": "1 accion concreta para el agricultor (riegar, mover sombra, etc)",
  "confianza": 0.0 - 1.0
}
Reglas: no inventes valores que no esten en el resumen; si falta un sensor, dilo;
si todo esta dentro de rango, estado = "ok" y la accion debe ser de mantenimiento.
Si el resumen trae "pronostico" y se espera lluvia suficiente, recomienda esperarla
antes de regar (salvo que el suelo este critico)."""

SCHEMA_JSON = {
    "type": "object",
    "properties": {
        "estado": {"type": "string", "enum": list(ESTADOS[:3])},
        "diagnostico": {"type": "string"},
        "accion_recomendada": {"type": "string"},
        "confianza": {"type": "number"},
    },
    "required": ["estado", "diagnostico", "accion_recomendada", "confianza"],
}


# --- Base de datos ----------------------------------------------------------

def _num(valor):
    """psycopg2 devuelve Decimal en numeric: el prompt de Gemini y el JSON
    necesitan float plano."""
    return float(valor) if valor is not None else None


def db():
    return psycopg2.connect(DB_URL, connect_timeout=5)


def resumir_cultivo(entidad=None, minutos=VENTANA_MIN):
    """Agrega la ventana reciente de lecturas. Devuelve {} si no hay datos."""
    with db() as conn, conn.cursor() as cur:
        where = "WHERE time_index >= NOW() - (%s || ' minutes')::interval"
        params = [minutos]
        if entidad:
            where += " AND entity_id = %s"
            params.append(entidad)

        cur.execute(
            f"""
            SELECT count(*) FILTER (WHERE time_index >= NOW() - interval '15 minutes') AS recientes,
                   count(*) AS total,
                   min(time_index), max(time_index),
                   round(avg(temperatura_aire)::numeric, 1)::float8, min(temperatura_aire), max(temperatura_aire),
                   round(avg(humedad_aire)::numeric, 1)::float8, min(humedad_aire), max(humedad_aire),
                   round(avg(luz_lux)::numeric, 1)::float8, max(luz_lux),
                   round(avg(humedad_suelo_pct)::numeric, 1)::float8, min(humedad_suelo_pct),
                   bool_or(alerta_seco) AS hubo_alerta,
                   bool_or(satelites > 0) AS gps_ok,
                   max(satelites) AS satelites,
                   max(altitud) AS altitud
            FROM lecturas_planta {where}
            """,
            params,
        )
        (recientes, total, t_min, t_max, t_avg, t_lo, t_hi,
         h_avg, h_lo, h_hi, luz_avg, luz_max,
         suelo_avg, suelo_min, hubo_alerta, gps_ok, sats, alt) = cur.fetchone()

        if not total:
            return {}

        silencio = (datetime.now(timezone.utc) - t_max).total_seconds() / 60.0

        # La ultima lectura manda: el promedio de la ventana suaviza una alerta.
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT temperatura_aire, humedad_aire, luz_lux, humedad_suelo_pct,
                       alerta_seco, satelites, altitud
                FROM lecturas_planta {where}
                ORDER BY time_index DESC LIMIT 1
                """,
                params,
            )
            ultima = cur.fetchone()

        return {
            "entidad": entidad or "todas",
            "muestras_ventana": int(total),
            "muestras_ultimos_15min": int(recientes or 0),
            "primera_lectura": t_min.isoformat(),
            "ultima_lectura": t_max.isoformat(),
            "minutos_sin_datos": round(silencio, 1),
            "sensor_presente": silencio <= SILENCIO_MIN,
            "ultima_lectura_valores": {
                "temperatura_aire_c": _num(ultima[0]),
                "humedad_aire_pct": _num(ultima[1]),
                "luz_lux": _num(ultima[2]),
                "humedad_suelo_pct": _num(ultima[3]),
                "alerta_seco": bool(ultima[4]),
                "satelites": int(ultima[5] or 0),
            },
            "ventana_temperatura_aire_c": {"prom": _num(t_avg), "min": _num(t_lo), "max": _num(t_hi)},
            "ventana_humedad_aire_pct": {"prom": _num(h_avg), "min": _num(h_lo), "max": _num(h_hi)},
            "ventana_luz_lux": {"prom": _num(luz_avg), "max": _num(luz_max)},
            "ventana_humedad_suelo_pct": {"prom": _num(suelo_avg), "min": _num(suelo_min)},
            "alerta_seco_disparada": bool(hubo_alerta),
            "gps": {"ok": bool(gps_ok), "satelites": int(sats or 0), "altitud_m": _num(alt)},
        }


def ubicacion_finca():
    """Ultima posicion GPS valida de cualquier nodo; si no hay, FINCA_LAT/LON."""
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT latitud, longitud FROM lecturas_planta
                WHERE satelites > 0 AND latitud <> 0 AND longitud <> 0
                ORDER BY time_index DESC LIMIT 1""")
            fila = cur.fetchone()
        if fila:
            return float(fila[0]), float(fila[1])
    except psycopg2.Error as e:
        logger.warning("No se pudo leer la ubicacion GPS: %s", e)
    if FINCA_LAT and FINCA_LON:
        return float(FINCA_LAT), float(FINCA_LON)
    return None


def pronostico(ubicacion):
    """Lluvia y temperatura de las proximas 24 h (Open-Meteo: gratis, sin API key).

    Si falla devuelve None y el analisis sigue sin pronostico: el clima es un
    dato de apoyo, no puede tumbar la vigilancia del cultivo.
    """
    if not ubicacion:
        return None
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": round(ubicacion[0], 3), "longitude": round(ubicacion[1], 3),
            "hourly": "precipitation,precipitation_probability,temperature_2m",
            "forecast_hours": 24, "timezone": "auto"}, timeout=8)
        r.raise_for_status()
        h = r.json()["hourly"]
        lluvia = [x or 0 for x in h["precipitation"]]
        prob = [x or 0 for x in h["precipitation_probability"]]
        temp = [x for x in h["temperature_2m"] if x is not None]
        return {
            "lluvia_24h_mm": round(sum(lluvia), 1),
            "prob_lluvia_max_pct": max(prob) if prob else None,
            "temp_max_24h_c": max(temp) if temp else None,
            "temp_min_24h_c": min(temp) if temp else None,
        }
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning("Pronostico no disponible: %s", e)
        return None


def entidades_activas(minutos=VENTANA_MIN):
    """Entidades con lecturas en la ventana: el scheduler analiza cada zona por
    separado para que una zona seca no quede escondida en el promedio."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT entity_id FROM lecturas_planta
               WHERE time_index >= NOW() - (%s || ' minutes')::interval ORDER BY 1""",
            (minutos,))
        return [r[0] for r in cur.fetchall()]


def guardar_analisis(entidad, estado, diagnostico, accion, confianza, modelo, contexto, latencia_ms, error=None):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO analisis_ia
              (entidad, estado, diagnostico, accion_recomendada, confianza, modelo, contexto, latencia_ms, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at
            """,
            (entidad, estado, diagnostico, accion, confianza, modelo,
             psycopg2.extras.Json(contexto), latencia_ms, error),
        )
        return cur.fetchone()


def ultimo_analisis():
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, created_at, entidad, estado, diagnostico, accion_recomendada,
                   confianza, modelo, error
            FROM analisis_ia ORDER BY created_at DESC LIMIT 1
            """
        )
        r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0], "created_at": r[1].isoformat(), "entidad": r[2], "estado": r[3],
        "diagnostico": r[4], "accion_recomendada": r[5], "confianza": r[6],
        "modelo": r[7], "error": r[8],
    }


def historial_analisis(limite=20):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at, entidad, estado, diagnostico, accion_recomendada, confianza
            FROM analisis_ia ORDER BY created_at DESC LIMIT %s
            """,
            (min(limite, 200),),
        )
        return [
            {"created_at": r[0].isoformat(), "entidad": r[1], "estado": r[2],
             "diagnostico": r[3], "accion_recomendada": r[4], "confianza": r[5]}
            for r in cur.fetchall()
        ]


# --- Orion ------------------------------------------------------------------

def publicar_en_orion(entidad, estado, diagnostico, accion, confianza):
    entity_id = "agente:agrosense"
    attrs = {
        "estado": {"value": estado, "type": "Text"},
        "diagnostico": {"value": texto_para_orion(diagnostico), "type": "Text"},
        "accion_recomendada": {"value": texto_para_orion(accion), "type": "Text"},
        "confianza": {"value": float(confianza or 0), "type": "Float"},
        "cultivo_observado": {"value": texto_para_orion(entidad), "type": "Text"},
    }
    try:
        r = requests.patch(f"{ORION_URL}/v2/entities/{entity_id}/attrs", json=attrs, timeout=5)
        if r.status_code == 204:
            return True
        if r.status_code == 404:
            body = {"id": entity_id, "type": "AIAgent"}
            body.update(attrs)
            r = requests.post(f"{ORION_URL}/v2/entities", json=body, timeout=5)
            if r.status_code == 201:
                return True
        logger.warning("Orion no acepto el dictamen: PATCH/POST HTTP %s %s",
                       r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        logger.warning("Orion no disponible: %s", e)
        return False


# --- Gemini -----------------------------------------------------------------

def limpiar_texto(valor, maximo=280):
    if not isinstance(valor, str):
        return ""
    limpio = re.sub(r"[`\r\n\t]+", " ", valor).strip()
    return limpio[:maximo]


def texto_para_orion(valor):
    """Orion-LD (API NGSI v2) devuelve 400 'Invalid characters in attribute value'
    si el texto lleva parentesis. Se quitan solo para Orion; PostgreSQL guarda
    el texto completo."""
    return (valor or "").replace("(", "").replace(")", "").strip()


def _parsear_dictamen(data):
    """Saca el JSON del dictamen y lo valida. Lanza excepcion si no sirve."""
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini no devolvio candidatos: {str(data)[:160]}")

    # Los modelos "thinking" pueden traer partes de razonamiento + la respuesta.
    texto = "".join(p.get("text", "") for p in
                    (candidates[0].get("content", {}).get("parts") or []))
    if not texto.strip():
        raise RuntimeError(f"Gemini devolvio una respuesta vacia "
                           f"(finishReason={candidates[0].get('finishReason')})")

    try:
        obj = json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if not m:
            raise RuntimeError(f"Gemini no devolvio JSON: {texto[:160]}") from None
        obj = json.loads(m.group(0))

    estado = str(obj.get("estado", "")).lower()
    if estado not in ESTADOS[:3]:
        raise RuntimeError(f"estado invalido de Gemini: {estado!r}")
    try:
        confianza = max(0.0, min(1.0, float(obj.get("confianza", 0.5))))
    except (TypeError, ValueError):
        confianza = 0.5

    return {
        "estado": estado,
        "diagnostico": limpiar_texto(obj.get("diagnostico")),
        "accion_recomendada": limpiar_texto(obj.get("accion_recomendada")),
        "confianza": confianza,
    }


def pedir_dictamen_a_gemini(contexto):
    """Llama a Gemini y devuelve (dictamen, modelo_usado).

    Recorre [GEMINI_MODEL] + GEMINI_FALLBACKS. Reintenta con backoff los errores
    transitorios (429 de caudal / 5xx) y cambia de modelo ante 404 (modelo retirado).
    Un 401/403 (key invalida) o un 429 de *cuota agotada* corta de inmediato: la
    cuota es del proyecto, asi que girar modelos o insistir solo gasta mas.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY vacio: define la key en .env")

    cuerpo = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "role": "user",
            "parts": [{"text": "Resumen de sensores:\n" + json.dumps(contexto, ensure_ascii=False)}],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": MAX_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA_JSON,
        },
    }

    candidatos = list(dict.fromkeys([GEMINI_MODEL] + GEMINI_FALLBACKS))
    errores = []

    for modelo in candidatos:
        for intento in range(GEMINI_REINTENTOS + 1):
            if intento:
                time.sleep(2 ** intento)   # backoff: 2s, 4s...
            try:
                r = requests.post(f"{GEMINI_BASE}/{modelo}:generateContent",
                                  params={"key": GEMINI_API_KEY}, json=cuerpo, timeout=45)
            except requests.RequestException as e:
                errores.append(f"{modelo}: {type(e).__name__}")
                break   # problema de red: no tiene sentido reintentar ya
            if r.status_code == 200:
                try:
                    return _parsear_dictamen(r.json()), modelo
                except Exception as e:
                    errores.append(f"{modelo}: {e}")
                    break   # otro modelo puede responder mejor
            if r.status_code in (401, 403):
                raise RuntimeError(f"Gemini rechazo la API key (HTTP {r.status_code}): {r.text[:200]}")
            if r.status_code == 429 and "quota" in r.text.lower():
                # Cuota del proyecto agotada: ningun modelo va a responder.
                raise RuntimeError(
                    "cuota de Gemini agotada (HTTP 429). Revisar plan/limites en "
                    "ai.google.dev; el agente sigue con reglas locales"
                )
            if r.status_code == 429 or r.status_code >= 500:
                errores.append(f"{modelo}: HTTP {r.status_code}")
                continue   # transitorio: reintenta el mismo modelo
            errores.append(f"{modelo}: HTTP {r.status_code} {r.text[:100]}")
            break   # 404/400: el modelo no sirve, prueba el siguiente

    raise RuntimeError("Gemini fallo con todos los modelos -> " + " | ".join(dict.fromkeys(errores[:6])))


def reglas_locales(contexto):
    """Veredicto sin Gemini: umbrales sobre la ultima lectura.

    Es el plan B para que el cultivo nunca se quede sin vigilar aunque la API de
    Google este caida o saturada.
    """
    ultima = contexto.get("ultima_lectura_valores", {})
    suelo = ultima.get("humedad_suelo_pct")
    if not contexto.get("sensor_presente"):
        return {"estado": "critico",
                "diagnostico": f"Sin datos del sensor hace {contexto['minutos_sin_datos']} min.",
                "accion_recomendada": "Revisar bateria y cobertura Wi-Fi del nodo.",
                "confianza": 0.95}
    if ultima.get("alerta_seco") or (suelo is not None and suelo < 30):
        return {"estado": "critico",
                "diagnostico": f"Suelo seco: {suelo}% en la ultima lectura (umbral 30%).",
                "accion_recomendada": "Regar la planta hoy mismo.",
                "confianza": 0.9}
    lluvia = (contexto.get("pronostico") or {}).get("lluvia_24h_mm") or 0
    if suelo is not None and suelo < 45 and lluvia >= 5:
        return {"estado": "atencion",
                "diagnostico": f"Humedad de suelo en {suelo}%, pero se esperan {lluvia} mm de lluvia en 24 h.",
                "accion_recomendada": "Esperar la lluvia antes de regar y volver a revisar.",
                "confianza": 0.7}
    if suelo is not None and suelo < 45:
        return {"estado": "atencion",
                "diagnostico": f"Humedad de suelo en {suelo}%, por debajo del ideal (>45%).",
                "accion_recomendada": "Programar riego antes de que llegue al umbral.",
                "confianza": 0.7}
    return {"estado": "ok",
            "diagnostico": "Todos los indicadores dentro de rango.",
            "accion_recomendada": "Mantenimiento normal: revisar riego y luz.",
            "confianza": 0.6}


# --- Nucleo -----------------------------------------------------------------

def analizar(entidad=None, motivo="programado"):
    """Un ciclo completo: resume, consulta a Gemini, guarda y publica."""
    t0 = time.time()
    contexto = resumir_cultivo(entidad)
    if not contexto:
        logger.warning("Sin lecturas en la ventana de %d min (%s)", VENTANA_MIN, motivo)
        guardar_analisis(entidad or "-", "error", "Sin lecturas del sensor en la ventana.",
                         "Revisar alimentacion/Wi-Fi del nodo.", 0.0, None, {}, None,
                         error="sin_lecturas")
        return ultimo_analisis()

    clima = pronostico(ubicacion_finca())
    if clima:
        contexto["pronostico"] = clima

    error_ia = None
    if not GEMINI_API_KEY:
        d = reglas_locales(contexto)
        modelo = "reglas-locales"
    else:
        try:
            d, modelo = pedir_dictamen_a_gemini(contexto)
        except Exception as e:
            # Gemini caido o saturado: el cultivo sigue vigilado con los umbrales.
            error_ia = str(e)[:500]
            d = reglas_locales(contexto)
            modelo = "reglas-locales (gemini no disponible)"
            logger.error("Gemini fallo (%s); se aplica el veredicto local", e)

    latencia = int((time.time() - t0) * 1000)
    guardar_analisis(contexto["entidad"], d["estado"], d["diagnostico"],
                     d["accion_recomendada"], d["confianza"], modelo, contexto, latencia, error_ia)
    publicar_en_orion(contexto["entidad"], d["estado"], d["diagnostico"],
                      d["accion_recomendada"], d["confianza"])
    logger.info("Analisis [%s/%s] %s (%.0f%%) en %dms -- %s",
                contexto["entidad"], motivo, d["estado"], d["confianza"] * 100, latencia, d["diagnostico"])
    return ultimo_analisis()


# --- API --------------------------------------------------------------------

@app.route("/api/agente/ultimo", methods=["GET"])
def agente_ultimo():
    return jsonify({"analisis": ultimo_analisis()})


@app.route("/api/agente/historial", methods=["GET"])
def agente_historial():
    return jsonify({"historial": historial_analisis(int(request.args.get("limite", 20)))})


def _exigir_clave_agente():
    """401 si no llega la clave correcta del agente."""
    if not AGENT_API_KEY:
        return None            # sin clave configurada: no se bloquea (dev)
    enviada = request.headers.get("X-Agent-Key", "")
    if not hmac.compare_digest(enviada, AGENT_API_KEY):
        return jsonify({"error": "clave de agente invalida"}), 401
    return None


@app.route("/api/agente/analizar", methods=["POST"])
def agente_analizar():
    denegado = _exigir_clave_agente()
    if denegado:
        return denegado
    entidad = request.args.get("entidad")
    if entidad and entidad != "-" and not entidad.startswith("planta:"):
        entidad = f"planta:{entidad}"
    return jsonify({"analisis": analizar(entidad, motivo="manual")})


@app.route("/api/agente/health", methods=["GET"])
def agente_health():
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        db_ok = True
    except psycopg2.Error:
        db_ok = False
    return jsonify({
        "status": "healthy" if db_ok else "degraded",
        "postgres": db_ok,
        "gemini_configurado": bool(GEMINI_API_KEY),
        "modelo": GEMINI_MODEL if GEMINI_API_KEY else "reglas-locales",
        "ventana_min": VENTANA_MIN,
    }), (200 if db_ok else 503)


@app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "Agente IA cultivo", "endpoints": [
        "/api/agente/ultimo", "/api/agente/historial", "/api/agente/analizar", "/api/agente/health"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5050")))
