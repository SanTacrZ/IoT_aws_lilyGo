"""Reglas de alerta (funciones puras, sin BD)."""
from reglas import Umbrales, evaluar

U = Umbrales()


def nodo(disp, sin_datos=30, **payload):
    base = {"humedad_suelo_pct": 60, "temperatura_aire": 22, "wifi_rssi": -60}
    base.update(payload)
    return {"dispositivo": disp, "segundos_sin_datos": sin_datos, "payload": base}


def claves(ev):
    return set(ev.condiciones)


def test_todo_normal_no_alerta():
    assert claves(evaluar([nodo("zona-1"), nodo("zona-2")], [], U)) == set()


def test_suelo_seco_es_critico():
    ev = evaluar([nodo("zona-1", humedad_suelo_pct=22)], [], U)
    c = ev.condiciones["suelo_seco:zona-1"]
    assert c.severidad == "critico" and "22%" in c.detalle


def test_histeresis_del_suelo():
    """Abre bajo 30; ya abierta, no cierra hasta superar 35."""
    n = nodo("zona-1", humedad_suelo_pct=32)
    assert "suelo_seco:zona-1" not in claves(evaluar([n], [], U))
    assert "suelo_seco:zona-1" in claves(evaluar([n], [], U, frozenset({"suelo_seco:zona-1"})))
    n = nodo("zona-1", humedad_suelo_pct=36)
    assert "suelo_seco:zona-1" not in claves(evaluar([n], [], U, frozenset({"suelo_seco:zona-1"})))


def test_nodo_caido_congela_sus_alertas_de_sensor():
    """Un nodo mudo no puede 'resolver' su suelo seco: sus valores son viejos."""
    ev = evaluar([nodo("zona-1", sin_datos=3600, humedad_suelo_pct=80), nodo("zona-2")], [], U)
    assert claves(ev) == {"nodo_caido:zona-1"}
    assert "zona-1" in ev.congeladas


def test_todos_caidos_un_solo_aviso():
    ev = evaluar([nodo("zona-1", sin_datos=3600), nodo("zona-2", sin_datos=7200)], [], U)
    assert claves(ev) == {"sin_datos_finca"}
    assert "Los 2 nodos llevan al menos 1 h 0 min" in ev.condiciones["sin_datos_finca"].detalle
    assert ev.congeladas == {"zona-1", "zona-2"}


def test_simuladores_ignorados():
    ev = evaluar([nodo("sim-tbeam-01", sin_datos=99999), nodo("zona-1")], [], U)
    assert claves(ev) == set()


def test_bateria_temperatura_y_senal():
    ev = evaluar([nodo("zona-1", bateria_v=3.31, temperatura_aire=37.2, wifi_rssi=-91)], [], U)
    assert claves(ev) == {"bateria_baja:zona-1", "temp_alta:zona-1", "senal_debil:zona-1"}
    assert all(c.severidad == "atencion" for c in ev.condiciones.values())


def test_sin_dato_de_bateria_no_alerta():
    assert claves(evaluar([nodo("zona-1")], [], U)) == set()


def test_ia_critico_solo_si_es_gemini():
    analisis = [
        {"entidad": "planta:zona-1", "estado": "critico", "modelo": "gemini-3.8-flash",
         "diagnostico": "Hojas en estres hidrico", "accion_recomendada": "Regar"},
        {"entidad": "planta:zona-2", "estado": "critico", "modelo": "reglas-locales",
         "diagnostico": "x", "accion_recomendada": "y"},
    ]
    ev = evaluar([nodo("zona-1"), nodo("zona-2")], analisis, U)
    assert claves(ev) == {"ia_critico:zona-1"}
