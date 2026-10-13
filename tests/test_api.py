from fastapi.testclient import TestClient

from trackmyferry.api.app import create_app
from trackmyferry.config import Settings
from trackmyferry.runtime import Runtime


def make_client(gtfs, sim_run, tmp_path):
    _, msgs = sim_run
    rt = Runtime(Settings(mode="sim", model_dir=tmp_path, web_dist=tmp_path / "none"), gtfs=gtfs, in_process=True)
    for m in msgs:
        rt.apply_outputs(rt.pipeline.handle(m))
    return rt, TestClient(create_app(rt))


def test_rest_endpoints(gtfs, sim_run, tmp_path):
    rt, client = make_client(gtfs, sim_run, tmp_path)
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["vessels"] >= 8 and health["model"] == "physics-baseline"

    net = client.get("/api/network").json()
    assert {r["route_id"] for r in net["routes"]} == {"ER", "AS", "SB"}
    kinds = [f["properties"]["kind"] for f in net["geojson"]["features"]]
    assert kinds.count("stop") == 16 and kinds.count("route") == 6

    vessels = client.get("/api/vessels").json()["vessels"]
    assert len(vessels) >= 8 and all({"lat", "lon", "status", "route_id"} <= v.keys() for v in vessels)

    one = client.get(f"/api/vessels/{vessels[0]['mmsi']}").json()
    assert one["vessel"]["mmsi"] == vessels[0]["mmsi"]
    assert client.get("/api/vessels/1").status_code == 404

    arr = client.get("/api/stops/PIER11/arrivals").json()
    assert arr["stop"]["name"] == "Wall St/Pier 11"
    assert arr["arrivals"], "expected boats heading to Pier 11 mid-morning"
    a = arr["arrivals"][0]
    assert a["eta_p10"] <= a["eta_p50"] <= a["eta_p90"] and a["route_color"].startswith("#")
    assert all(x["terminates"] for x in arr["arrivals"])  # Pier 11 is a terminal on all three routes
    assert client.get("/api/stops/NOPE/arrivals").status_code == 404

    mid = client.get("/api/stops/NWBG/arrivals").json()
    assert any(not x["terminates"] and x["headsign"] for x in mid["arrivals"])

    track = client.get(f"/api/vessels/{vessels[0]['mmsi']}/track?minutes=10").json()
    assert len(track["points"]) > 5

    metrics = client.get("/metrics").text
    assert "trackmyferry_vessels_tracked" in metrics


def test_websocket_snapshot(gtfs, sim_run, tmp_path):
    rt, client = make_client(gtfs, sim_run, tmp_path)
    with client.websocket_connect("/ws/live") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and len(snap["vessels"]) >= 8
        rt.hub.vessel(snap["vessels"][0])
        rt.hub.flush()
        upd = ws.receive_json()
        assert upd["type"] == "vessels" and upd["vessels"][0]["mmsi"] == snap["vessels"][0]["mmsi"]
