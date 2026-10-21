"""alert-agent: evalua las reglas cada ALERT_INTERVALO_S y atiende el bot.

Dos hilos: el ciclo de alertas (principal) y el long polling de Telegram. El
healthcheck del contenedor mira la frescura de HEARTBEAT, igual que el
scheduler del agente IA.
"""
import logging
import os
import signal
import sys
import threading
import time

import motor
from bot import bucle
from notificadores import Despachador

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("alertas")

INTERVALO_S = max(int(os.getenv("ALERT_INTERVALO_S", "60")), 10)
HEARTBEAT = "/tmp/alertas-heartbeat"
_parar = threading.Event()


def latido():
    with open(HEARTBEAT, "w") as f:
        f.write(str(time.time()))


def main():
    signal.signal(signal.SIGTERM, lambda *_: _parar.set())
    signal.signal(signal.SIGINT, lambda *_: _parar.set())

    despachador = Despachador(motor.registrar_notificacion)
    logger.info("alert-agent arrancado: cada %d s, canales %s", INTERVALO_S, despachador.activos())
    threading.Thread(target=bucle, args=(_parar.is_set,), daemon=True, name="bot").start()

    latido()
    while not _parar.is_set():
        try:
            motor.ciclo(despachador)
        except Exception:
            logger.exception("Fallo el ciclo de alertas")
        latido()
        _parar.wait(INTERVALO_S)
    logger.info("alert-agent detenido.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
