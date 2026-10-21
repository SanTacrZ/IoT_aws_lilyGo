"""Firma v2 del iot-agent: cubre metodo, ruta y query, y ata cada clave a su nodo."""
import hashlib
import hmac
import importlib.util
import json
import time

import pytest

from conftest import RAIZ

CREDS = {"zona-1": ("clave-zona-1", "secreto-zona-1"), "zona-2": ("clave-zona-2", "secreto-zona-2")}
LECTURA = {"temperatura_aire": 22.5, "humedad_aire": 70, "luz_lux": 120, "humedad_suelo_pct": 55,
           "humedad_suelo_raw": 2400, "alerta_seco": False, "latitud": 0, "longitud": 0,
           "altitud": 0, "satelites": 0}


@pytest.fixture
def agente(monkeypatch):
    monkeypatch.setenv("DEVICE_CREDENTIALS", ",".join(f"{d}:{k}:{s}" for d, (k, s) in CREDS.items()))
    monkeypatch.setenv("READ_API_KEY", "clave-lectura")
    monkeypatch.delenv("FIRMA_V1_PERMITIDA", raising=False)
    spec = importlib.util.spec_from_file_location("iot_agent_app", RAIZ / "services/iot-agent/app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    guardadas = []
    monkeypatch.setattr(mod, "upsert_orion_entity", lambda *a: True)
    monkeypatch.setattr(mod, "guardar_lectura", lambda *a: (guardadas.append(a), (True, True))[1])
    mod.guardadas = guardadas
    return mod


def firmar(secreto, recurso, body, ts=None, version="2"):
    ts = str(ts or int(time.time()))
    msg = (f"{ts}.POST.{recurso}.".encode() + body) if version == "2" else (ts.encode() + b"." + body)
    return ts, hmac.new(secreto.encode(), msg, hashlib.sha256).hexdigest()


def post(cliente, disp_clave, recurso, body=None, firmado_para=None, version="2", ts=None):
    body = body or json.dumps(LECTURA).encode()
    clave, secreto = CREDS[disp_clave]
    ts, sig = firmar(secreto, firmado_para or recurso, body, ts, version)
    return cliente.post(recurso, data=body, headers={
        "Content-Type": "application/json", "X-Api-Key": clave, "X-Timestamp": ts,
        "X-Signature": sig, "X-Firma-Version": version})


def test_firma_v2_valida(agente):
    r = post(agente.app.test_client(), "zona-1", "/api/plantas?dispositivo=zona-1&lectura_id=1700000000")
    assert r.status_code == 200, r.json
    assert agente.guardadas[0][2] == "zona-1"


def test_cambiar_la_query_rompe_la_firma(agente):
    """El ataque de la revision: reenviar un POST capturado cambiando ?dispositivo=."""
    r = post(agente.app.test_client(), "zona-1", "/api/plantas?dispositivo=zona-2&lectura_id=1",
             firmado_para="/api/plantas?dispositivo=zona-1&lectura_id=1")
    assert r.status_code == 401 and "firma" in r.json["error"]


def test_clave_de_otro_nodo_prohibida(agente):
    """Firma valida, pero la clave de zona-1 no puede publicar como zona-2."""
    r = post(agente.app.test_client(), "zona-1", "/api/plantas?dispositivo=zona-2&lectura_id=1")
    assert r.status_code == 403


def test_sin_query_usa_el_dispositivo_de_la_clave(agente):
    assert post(agente.app.test_client(), "zona-2", "/api/plantas").status_code == 200
    assert agente.guardadas[0][2] == "zona-2"


def test_firma_v1_rechazada_por_defecto(agente):
    r = post(agente.app.test_client(), "zona-1", "/api/plantas?dispositivo=zona-1", version="1")
    assert r.status_code == 401 and "v1" in r.json["error"]


def test_replay_viejo_rechazado(agente):
    r = post(agente.app.test_client(), "zona-1", "/api/plantas", ts=int(time.time()) - 3600)
    assert r.status_code == 401 and "expirado" in r.json["error"]


def test_get_con_clave_de_lectura(agente, monkeypatch):
    monkeypatch.setattr(agente, "estado_nodos", lambda m: [])
    c = agente.app.test_client()
    assert c.get("/api/nodos", headers={"X-Api-Key": "clave-lectura"}).status_code == 200
    # Una clave de nodo no sirve para leer cuando hay READ_API_KEY.
    assert c.get("/api/nodos", headers={"X-Api-Key": "clave-zona-1"}).status_code == 401


@pytest.mark.parametrize("campo,valor", [
    ("temperatura_aire", 130), ("humedad_aire", -1), ("luz_lux", 25000),
    ("humedad_suelo_raw", 5000), ("latitud", 91), ("bateria_v", 6.0), ("humedad_suelo_pct", "seco"),
])
def test_valores_fuera_del_datasheet_se_rechazan(agente, campo, valor):
    body = json.dumps(dict(LECTURA, **{campo: valor})).encode()
    r = post(agente.app.test_client(), "zona-1", "/api/plantas", body=body)
    assert r.status_code == 422 and campo in r.json["campos"]
    assert agente.guardadas == []


def test_limites_exactos_del_datasheet_se_aceptan(agente):
    body = json.dumps(dict(LECTURA, temperatura_aire=-40, humedad_aire=100, luz_lux=20661,
                           humedad_suelo_raw=4095, bateria_v=4.2)).encode()
    assert post(agente.app.test_client(), "zona-1", "/api/plantas", body=body).status_code == 200
