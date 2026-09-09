"""Ejecuta el motor de reglas cada RULE_INTERVAL_S (default 60s)."""
import logging
import os
import time

from automation.rule_engine import run_cycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
INTERVAL = int(os.getenv("RULE_INTERVAL_S", "60"))

while True:
    try:
        run_cycle()
    except Exception:
        logging.exception("ciclo de reglas fallo")
    time.sleep(INTERVAL)
