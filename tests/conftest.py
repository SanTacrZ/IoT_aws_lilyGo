import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
# Los servicios no son paquetes: se importan sus modulos por ruta.
for servicio in ("alert-agent",):
    sys.path.insert(0, str(RAIZ / "services" / servicio))
