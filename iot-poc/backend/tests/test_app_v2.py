"""Pruebas de integracion v2: CRUD + ingesta firmada + comandos de riego
contra Postgres REAL (docker dev o CI con servicio timescaledb).
Correr: docker compose -f iot-poc/deploy/docker-compose.dev.yml exec api-v2 pytest -q
"""
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import pytest

# forzar claves de prueba (el contenedor puede traer otras en su entorno)
os.environ["DEVICE_API_KEY"] = "test-key-v2"
os.environ["HMAC_SECRET"] = "test-hmac-v2"
os.environ["ADMIN_KEY"] = "test-admin-v2"
os.environ.setdefault("DB_HOST", os.getenv("DB_HOST", "db"))
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "iot")
os.environ.setdefault("DB_USER", "iot")
os.environ.setdefault("DB_PASSWORD", "iot")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# si test_app.py corrio antes, deja un stub de psycopg2: lo quitamos para DB real
sys.modules.pop("psycopg2", None)
import app_v2 as backend  # noqa: E402

SUF = uuid.uuid4().hex[:8]

def admin_h():
    return {"X-Admin-Key": "test-admin-v2", "Content-Type": "application/json"}

def dev_h():
    return {"X-Api-Key": "test-key-v2", "Content-Type": "application/json"}

def signed_h(body: bytes):
    ts = str(int(time.time()))
    sig = hmac.new(b"test-hmac-v2", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Api-Key": "test-key-v2",
            "X-Timestamp": ts, "X-Signature": sig}

def post(client, path, payload, hdrs):
    return client.post(path, data=json.dumps(payload), headers=hdrs)

@pytest.fixture(scope="module")
def client():
    backend.init_db()
    return backend.app.test_client()

# ---------------- salud ----------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"

# ---------------- CRUD completo ----------------

def test_crud_flow(client):
    fid = post(client, "/api/v2/farms", {"name": f"Farm-{SUF}"}, admin_h()).get_json()["farm_id"]
    assert post(client, "/api/v2/farms", {}, admin_h()).status_code == 422
    assert client.get("/api/v2/farms", headers=admin_h()).status_code == 200
    assert client.get("/api/v2/farms", headers=dev_h()).status_code == 401  # admin-key != api-key

    z = post(client, "/api/v2/zones", {"farm_id": fid, "name": f"Z-{SUF}", "crop": "tomate",
                                       "soil_min_pct": 35}, admin_h()).get_json()
    assert z["zone_id"]
    assert post(client, "/api/v2/zones", {"name": "x"}, admin_h()).status_code == 422  # sin farm_id
    assert client.patch(f"/api/v2/zones/{z['zone_id']}", data=json.dumps({"soil_min_pct": 32}),
                        headers=admin_h()).status_code == 200
    assert client.get(f"/api/v2/zones/{z['zone_id']}", headers=admin_h()).get_json()["soil_min_pct"] == 32

    assert post(client, "/api/v2/rules", {"zone_id": z["zone_id"], "name": f"R-{SUF}",
                                          "condition": "below", "threshold": 30,
                                          "action": "irrigate+alert"}, admin_h()).status_code == 201
    assert post(client, "/api/v2/rules", {"zone_id": z["zone_id"], "name": "x",
                                          "condition": "fuego", "threshold": 30}, admin_h()).status_code == 422
    a = post(client, "/api/v2/actuators", {"device_id": f"fake-{SUF}", "zone_id": z["zone_id"],
                                           "kind": "pump", "label": f"bomba-{SUF}"}, admin_h())
    assert a.status_code == 422  # device no existe (auto-registro obliga a existir antes)
    post(client, "/api/v2/readings", _ingest_payload(), signed_h(json.dumps(_ingest_payload()).encode()))
    dev = _ingest_payload()["device_id"]
    ok = post(client, "/api/v2/actuators", {"device_id": dev, "zone_id": z["zone_id"],
                                            "kind": "pump", "label": f"bomba-{SUF}"}, admin_h())
    assert ok.status_code == 201
    assert post(client, "/api/v2/actuators", {"device_id": "x", "kind": "laser",
                                              "label": "x"}, admin_h()).status_code == 422

    assert post(client, "/api/v2/users", {"email": f"u{SUF}@t.org", "role": "farmer"},
                admin_h()).status_code == 201
    assert post(client, "/api/v2/users", {"email": f"u{SUF}@t.org"}, admin_h()).status_code == 409

    assert client.delete(f"/api/v2/farms/{fid}", headers=admin_h()).status_code == 409  # tiene zona
    assert client.delete(f"/api/v2/farms/{fid}?force=1", headers=admin_h()).status_code == 200
    assert client.get(f"/api/v2/farms/{fid}", headers=admin_h()).status_code == 404

def _ingest_payload(**kw):
    p = {"device_id": f"auto-{SUF}", "name": "Equipo test", "fw": "t",
         "sensors": [{"sensor_id": "soil1", "type": "soil", "unit": "%", "value": 41.5},
                     {"sensor_id": "batt1", "type": "battery", "unit": "V", "value": 4.0}]}
    p.update(kw)
    return p

# ---------------- ingesta firmada + registro autonomo ----------------

def test_ingesta_auth(client):
    body = json.dumps(_ingest_payload()).encode()
    assert client.post("/api/v2/readings", data=body, headers=dev_h()).status_code == 401  # sin HMAC
    ts = str(int(time.time()))
    bad = {"Content-Type": "application/json", "X-Api-Key": "test-key-v2",
           "X-Timestamp": ts, "X-Signature": "muerte" * 8}
    assert client.post("/api/v2/readings", data=body, headers=bad).status_code == 401
    old = dict(signed_h(body)); old["X-Timestamp"] = "1000000000"  # replay viejo
    assert client.post("/api/v2/readings", data=body, headers=old).status_code == 401

def test_ingesta_valida_y_registra_solo(client):
    body = json.dumps(_ingest_payload()).encode()
    r = client.post("/api/v2/readings", data=body, headers=signed_h(body))
    assert r.status_code == 201
    bad = json.dumps(_ingest_payload(device_id="j", sensors=[{"sensor_id": "x", "value": "infinito"}])).encode()
    assert client.post("/api/v2/readings", data=bad, headers=signed_h(bad)).status_code == 422
    state = client.get("/api/v2/state", headers=dev_h()).get_json()
    d = [x for x in state["devices"] if x["device_id"] == _ingest_payload()["device_id"]][0]
    assert d["status"] == "online" and {s["sensor_id"] for s in d["sensors"]} == {"soil1", "batt1"}

# ---------------- ciclo de riego: config -> comando -> done -> auditoria ----------------

def test_ciclo_riego(client):
    dev = _ingest_payload()["device_id"]
    body = json.dumps(_ingest_payload()).encode()
    client.post("/api/v2/readings", data=body, headers=signed_h(body))
    assert client.get(f"/api/v2/config?device_id={dev}", headers=dev_h()).get_json() == {}  # sin zona aun

    fid = post(client, "/api/v2/farms", {"name": f"FarmC-{SUF}"}, admin_h()).get_json()["farm_id"]
    zid = post(client, "/api/v2/zones", {"farm_id": fid, "name": f"ZC-{SUF}",
                                         "soil_min_pct": 30}, admin_h()).get_json()["zone_id"]
    client.patch(f"/api/v2/devices/{dev}", data=json.dumps({"zone_id": zid}), headers=admin_h())
    post(client, "/api/v2/actuators", {"device_id": dev, "zone_id": zid, "kind": "pump",
                                       "label": f"bombaC-{SUF}"}, admin_h())
    cfg = client.get(f"/api/v2/config?device_id={dev}", headers=dev_h()).get_json()
    assert cfg["zone_id"] == zid and cfg["soil_min_pct"] == 30

    r = post(client, f"/api/v2/zones/{zid}/irrigate", {"duration_min": 1}, admin_h())
    assert r.status_code == 201
    cid = r.get_json()["cmd_id"]
    pend = client.get(f"/api/v2/commands/pending?device_id={dev}", headers=dev_h()).get_json()
    assert [c["cmd_id"] for c in pend] == [cid] and pend[0]["action"] == "on"
    # segunda consulta no re-entrega (ya delivered)
    assert client.get(f"/api/v2/commands/pending?device_id={dev}", headers=dev_h()).get_json() == []

    assert post(client, f"/api/v2/commands/{cid}/done", {"result": "ok", "liters": 0.8},
                dev_h()).status_code == 200
    evs = client.get(f"/api/v2/irrigation-events?zone_id={zid}", headers=admin_h()).get_json()
    e = [x for x in evs if x["duration_min"] is not None]
    assert e and e[0]["liters"] == 0.8 and e[0]["trigger"] == "manual"

def test_reporte_offline(client):
    dev = _ingest_payload()["device_id"]
    body = json.dumps(_ingest_payload()).encode()
    client.post("/api/v2/readings", data=body, headers=signed_h(body))
    fid = post(client, "/api/v2/farms", {"name": f"FarmO-{SUF}"}, admin_h()).get_json()["farm_id"]
    zid = post(client, "/api/v2/zones", {"farm_id": fid, "name": f"ZO-{SUF}"}, admin_h()).get_json()["zone_id"]
    client.patch(f"/api/v2/devices/{dev}", data=json.dumps({"zone_id": zid}), headers=admin_h())
    post(client, "/api/v2/actuators", {"device_id": dev, "zone_id": zid, "kind": "pump",
                                       "label": f"bombaO-{SUF}"}, admin_h())
    ev = {"started_at": int(time.time()) - 120, "duration_min": 4, "liters": 3.1}
    r = post(client, "/api/v2/irrigation/report", {"device_id": dev, "events": [ev]}, dev_h())
    assert r.status_code == 201
    evs = client.get(f"/api/v2/irrigation-events?zone_id={zid}", headers=admin_h()).get_json()
    assert any(x["trigger"] == "offline" and x["liters"] == 3.1 for x in evs)
    assert post(client, "/api/v2/irrigation/report", {"device_id": dev, "events": []}, dev_h()).status_code == 422

# ---------------- alertas ----------------

def test_alertas_endpoints(client):
    r = client.get("/api/v2/alerts", headers=admin_h())
    assert r.status_code == 200 and isinstance(r.get_json(), list)
    assert client.post("/api/v2/alerts/999999/ack", headers=admin_h()).status_code == 404

# ---------------- stats + lecturas de solo consulta con device-key ----------------

def test_stats_y_lecturas(client):
    s = client.get("/api/v2/stats", headers=dev_h())
    j = s.get_json()
    assert s.status_code == 200 and set(j) == {"devices_online", "devices_total",
                                               "irrigations_today", "liters_today", "alerts_open"}
    assert j["devices_total"] >= j["devices_online"] >= 0
    assert client.get("/api/v2/zones", headers=dev_h()).status_code == 200      # solo lectura ok
    assert client.get("/api/v2/irrigation-events", headers=dev_h()).status_code == 200
    assert client.post("/api/v2/zones", data="{}", headers=dev_h()).status_code == 401  # escritura NO
