"""Motor de alertas: lee el estado de la finca, aplica las reglas y gestiona el
ciclo de vida de cada alerta (abrir -> recordar -> escalar -> resolver).

Una alerta abierta y sin reconocer se recuerda cada ALERT_RECORDATORIO_MIN.
Si es critica y nadie la reconoce en ALERT_ESCALAR_MIN, se escala (SMS).
Con /ack en Telegram deja de insistir, pero se sigue cerrando sola cuando la
condicion desaparece.
"""
import logging
import os
from datetime import datetime, timezone
from fnmatch import fnmatch

import psycopg2
import psycopg2.extras

import mensajes
from notificadores import Despachador
from reglas import Umbrales, evaluar

logger = logging.getLogger("alertas.motor")

DB_URL = os.getenv("DB_URL", "")       # la arma compose con POSTGRES_PASSWORD
RECORDATORIO_MIN = float(os.getenv("ALERT_RECORDATORIO_MIN", "60"))
ESCALAR_MIN = float(os.getenv("ALERT_ESCALAR_MIN", "10"))
NODOS_DIAS = int(os.getenv("ALERT_NODOS_DIAS", "30"))

COLUMNAS = ("id", "clave", "tipo", "entidad", "severidad", "titulo", "detalle", "accion",
            "valor", "abierta_en", "ultima_notif", "avisos", "escalada_en",
            "reconocida_en", "reconocida_por", "resuelta_en")


def db():
    return psycopg2.connect(DB_URL, connect_timeout=5)


# --- Lectura del estado -----------------------------------------------------

def leer_nodos(cur):
    """Ultima lectura de cada dispositivo visto en los ultimos NODOS_DIAS."""
    cur.execute(
        """
        SELECT DISTINCT ON (dispositivo)
               dispositivo, time_index,
               EXTRACT(EPOCH FROM (NOW() - time_index))::int, payload
        FROM lecturas
        WHERE time_index >= NOW() - make_interval(days => %s) AND dispositivo IS NOT NULL
        ORDER BY dispositivo, time_index DESC
        """,
        (NODOS_DIAS,),
    )
    return [{"dispositivo": d, "ultima": t, "segundos_sin_datos": s, "payload": p or {}}
            for d, t, s, p in cur.fetchall()]


def leer_analisis(cur):
    """Ultimo dictamen reciente del agente IA por entidad."""
    cur.execute(
        """
        SELECT DISTINCT ON (entidad) entidad, estado, diagnostico, accion_recomendada, modelo
        FROM analisis_ia
        WHERE created_at >= NOW() - interval '45 minutes'
        ORDER BY entidad, created_at DESC
        """
    )
    return [dict(zip(("entidad", "estado", "diagnostico", "accion_recomendada", "modelo"), r))
            for r in cur.fetchall()]


def leer_abiertas(cur):
    cur.execute(f"SELECT {', '.join(COLUMNAS)} FROM alertas WHERE resuelta_en IS NULL ORDER BY id")
    return {r[1]: dict(zip(COLUMNAS, r)) for r in cur.fetchall()}


def registrar_notificacion(alerta_id, canal, destino, texto, ok, error):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO notificaciones (alerta_id, canal, destino, texto, ok, error) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (alerta_id, canal, destino, texto, ok, error),
        )


# --- Ciclo ------------------------------------------------------------------

def ciclo(despachador: Despachador, umbrales: Umbrales | None = None, ahora=None, solo=None):
    """Una pasada completa. Devuelve un resumen de lo que hizo (para logs/tests).

    solo: conjunto de dispositivos a gestionar (prueba e2e aislada). Por defecto
    se gestionan todos menos los de ALERT_IGNORAR, cuyas alertas no se tocan.
    """
    umbrales = umbrales or Umbrales.desde_env()
    ahora = ahora or datetime.now(timezone.utc)
    hecho = {"abiertas": [], "recordadas": [], "escaladas": [], "resueltas": []}

    with db() as conn, conn.cursor() as cur:
        nodos = leer_nodos(cur)
        analisis = leer_analisis(cur)
        abiertas = leer_abiertas(cur)
    if solo is not None:
        nodos = [n for n in nodos if n["dispositivo"] in solo]
        analisis = []
        abiertas = {k: a for k, a in abiertas.items() if a["entidad"] in solo}
    else:
        abiertas = {k: a for k, a in abiertas.items()
                    if not any(fnmatch(a["entidad"], p) for p in umbrales.ignorar)}
    ev = evaluar(nodos, analisis, umbrales, frozenset(abiertas))

    for clave, c in ev.condiciones.items():
        a = abiertas.get(clave)
        if a is None:
            a = _abrir(c, ahora)
            if a is None:
                continue
            despachador.enviar(a["id"], *mensajes.nueva(a, ahora))
            _marcar_notificada(a["id"], ahora)
            hecho["abiertas"].append(clave)
            continue

        _actualizar(a["id"], c)
        a.update(detalle=c.detalle, accion=c.accion, valor=c.valor, severidad=c.severidad)
        if a["reconocida_en"]:
            continue
        minutos_abierta = (ahora - a["abierta_en"]).total_seconds() / 60
        if c.severidad == "critico" and not a["escalada_en"] and minutos_abierta >= ESCALAR_MIN:
            despachador.enviar(a["id"], *mensajes.escalado(a, ahora), nivel="escalado")
            _set(a["id"], "escalada_en", ahora)
            hecho["escaladas"].append(clave)
        ultima = a["ultima_notif"] or a["abierta_en"]
        if (ahora - ultima).total_seconds() / 60 >= RECORDATORIO_MIN:
            despachador.enviar(a["id"], *mensajes.recordatorio(a, ahora))
            _marcar_notificada(a["id"], ahora)
            hecho["recordadas"].append(clave)

    # Un nodo que ya no aparece (mas de NODOS_DIAS mudo) no "se arreglo": sus
    # alertas se mantienen en vez de cerrarse en falso.
    vigentes = {n["dispositivo"] for n in nodos} | {"finca"}
    for clave, a in abiertas.items():
        if clave in ev.condiciones or a["entidad"] in ev.congeladas:
            continue
        if a["tipo"] != "ia_critico" and a["entidad"] not in vigentes:
            continue
        _set(a["id"], "resuelta_en", ahora)
        despachador.enviar(a["id"], *mensajes.resuelta(a, ahora))
        hecho["resueltas"].append(clave)

    if any(hecho.values()):
        logger.info("Ciclo: %s", {k: v for k, v in hecho.items() if v})
    return hecho


def _abrir(c, ahora):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO alertas (clave, tipo, entidad, severidad, titulo, detalle, accion, valor, abierta_en)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (clave) WHERE resuelta_en IS NULL DO NOTHING
            RETURNING {', '.join(COLUMNAS)}
            """,
            (c.clave, c.tipo, c.entidad, c.severidad, c.titulo, c.detalle, c.accion, c.valor, ahora),
        )
        fila = cur.fetchone()
    return dict(zip(COLUMNAS, fila)) if fila else None


def _actualizar(alerta_id, c):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE alertas SET detalle=%s, accion=%s, valor=%s, severidad=%s WHERE id=%s",
                    (c.detalle, c.accion, c.valor, c.severidad, alerta_id))


def _marcar_notificada(alerta_id, ahora):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE alertas SET ultima_notif=%s, avisos=avisos+1 WHERE id=%s", (ahora, alerta_id))


_CAMPOS_FECHA = {"escalada_en", "resuelta_en"}


def _set(alerta_id, campo, valor):
    assert campo in _CAMPOS_FECHA          # el nombre de columna va en el SQL
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE alertas SET {campo}=%s WHERE id=%s", (valor, alerta_id))


# --- Consultas para el bot ----------------------------------------------------

def reconocer(alerta_id, quien):
    """alerta_id=None reconoce todas las abiertas. Devuelve los ids reconocidos."""
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE alertas SET reconocida_en=NOW(), reconocida_por=%s
            WHERE resuelta_en IS NULL AND reconocida_en IS NULL AND (%s::bigint IS NULL OR id=%s)
            RETURNING id
            """,
            (quien, alerta_id, alerta_id),
        )
        return [r[0] for r in cur.fetchall()]


def abiertas_ordenadas():
    with db() as conn, conn.cursor() as cur:
        return sorted(leer_abiertas(cur).values(),
                      key=lambda a: (a["severidad"] != "critico", a["abierta_en"]))


def resumen_finca():
    with db() as conn, conn.cursor() as cur:
        nodos = leer_nodos(cur)
        cur.execute("SELECT created_at, estado, diagnostico, modelo FROM analisis_ia "
                    "WHERE error IS DISTINCT FROM 'sin_lecturas' ORDER BY created_at DESC LIMIT 1")
        ia = cur.fetchone()
    return nodos, ia
