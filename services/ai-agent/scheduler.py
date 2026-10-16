"""Scheduler del agente IA: dispara un analisis cada AGENT_INTERVAL_MIN minutos.

Corre como contenedor aparte (misma imagen que la API) para que el gunicorn de
la API no dependa de threads ni se dupliquen los ciclos con varios workers.
"""
import logging
import os
import signal
import sys
import time

from app import analizar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("agente-scheduler")

INTERVALO_MIN = int(os.getenv("AGENT_INTERVAL_MIN", "15"))
INTERVALO_S = max(INTERVALO_MIN, 1) * 60
ENTIDAD = os.getenv("AGENT_ENTIDAD") or None
HEARTBEAT = "/tmp/agente-heartbeat"

_corriendo = True


def latido():
    """El healthcheck del contenedor mira la frescura de este archivo: el
    scheduler no sirve HTTP, asi que esta es la senal de que sigue vivo."""
    with open(HEARTBEAT, "w") as f:
        f.write(str(time.time()))


def parar(signum, frame):
    global _corriendo
    logger.info("Senal %s recibida, terminando...", signum)
    _corriendo = False


def main():
    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)

    logger.info("Scheduler arrancado: cada %d min, entidad=%s", INTERVALO_MIN, ENTIDAD or "todas")
    latido()

    # Primer analisis apenas arranca (con pequeno retardo para que la BD este lista).
    time.sleep(10)
    while _corriendo:
        inicio = time.time()
        try:
            analizar(ENTIDAD, motivo="scheduler")
        except Exception:
            logger.exception("Fallo el ciclo de analisis")
        latido()
        if not _corriendo:
            break
        # Duerme lo que falte del intervalo (no acumula drift), mantendo el latido.
        fin = time.time() + max(INTERVALO_S - (time.time() - inicio), 5)
        while _corriendo and time.time() < fin:
            time.sleep(2)
            latido()

    logger.info("Scheduler detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())