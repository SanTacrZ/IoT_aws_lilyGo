"""Contrato entre firmware, iot-agent, base de datos y alertas.

Un campo de la lectura vive en varios sitios: el JSON que arma main.cpp, la
validacion de rangos del iot-agent, la vista SQL y las reglas de alerta. Si se
agrega o se quita en uno y se olvida en otro, nada falla en el momento: el dato
llega sin validar o la vista lo pierde en silencio. Estas pruebas lo detectan.
"""
import re

from conftest import RAIZ

FIRMWARE = (RAIZ / "firmware/lilygo_planta/src/main.cpp").read_text()
IOT_AGENT = (RAIZ / "services/iot-agent/app.py").read_text()
REGLAS = (RAIZ / "services/alert-agent/reglas.py").read_text()
VISTA = (RAIZ / "db/migrations/003_luz_pct.sql").read_text()


def campos_firmware():
    lineas = [linea for linea in FIRMWARE.splitlines() if "body +=" in linea]
    return {c for linea in lineas for c in re.findall(r'\\"(\w+)\\":', linea)}


def rangos_servidor():
    bloque = IOT_AGENT[IOT_AGENT.index("RANGOS = {"):IOT_AGENT.index("def fuera_de_rango")]
    return set(re.findall(r'"(\w+)": \(', bloque))


def obligatorios_servidor():
    bloque = IOT_AGENT[IOT_AGENT.index("CAMPOS_PLANTA = ["):]
    return set(re.findall(r'"(\w+)"', bloque[:bloque.index("]")]))


def test_el_parser_del_firmware_encuentra_los_campos():
    assert {"temperatura_aire", "humedad_suelo_pct", "bateria_v"} <= campos_firmware()


def test_todo_campo_del_firmware_tiene_rango_en_el_servidor():
    sin_rango = campos_firmware() - rangos_servidor() - {"alerta_seco"}   # booleano
    assert not sin_rango, f"campos que llegan sin validar: {sin_rango}"


def test_el_servidor_no_exige_campos_que_el_firmware_no_manda():
    faltan = obligatorios_servidor() - campos_firmware()
    assert not faltan, f"el iot-agent rechazaria todas las lecturas: falta {faltan}"


def test_la_vista_sql_solo_lee_campos_que_existen():
    leidos = set(re.findall(r"payload->>'(\w+)'", VISTA))
    huerfanos = leidos - campos_firmware()
    assert not huerfanos, f"columnas de lecturas_planta siempre NULL: {huerfanos}"


def test_las_reglas_de_alerta_leen_campos_que_existen():
    leidos = set(re.findall(r'p\.get\("(\w+)"\)', REGLAS))
    assert leidos and not leidos - campos_firmware(), leidos - campos_firmware()
