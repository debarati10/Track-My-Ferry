"""HTTP + WebSocket API for the live map and for anyone who wants the data.

REST
  GET /api/health                     service status
  GET /api/network                    routes + landings as GeoJSON
  GET /api/vessels                    every tracked boat right now
  GET /api/vessels/{mmsi}             one boat + its upcoming-stop predictions
  GET /api/vessels/{mmsi}/track       recent path (database if configured, else in-memory)
  GET /api/stops/{stop_id}/arrivals   next boats at a landing: ETA range, official ETA, chance it's full
  GET /api/model                      model version + offline evaluation metrics
  GET /api/accuracy                   live accuracy from the database (predictions vs actual arrivals)
  GET /metrics                        Prometheus
WebSocket
  /ws/live                            snapshot on connect, then batched vessel/prediction updates
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__, metrics


class WebFiles(StaticFiles):
    """The map page must never be served stale; Vite's hashed /assets files can be cached for good."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.replace("\\", "/").startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(rt) -> FastAPI:
    """`rt` is a `trackmyferry.runtime.Runtime` (or anything exposing the same attributes)."""
    app = FastAPI(title="Track My Ferry", version=__version__,
                  description="Live NYC Ferry positions, arrival predictions and crowding forecasts.")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
    network_cache: dict[str, Any] = {}

    def now() -> float:
        return rt.clock()

    @app.get("/api/health")
    def health() -> dict:
        vessels = rt.store.get_vessels(now())
        return {"status": "ok", "mode": rt.settings.mode, "version": __version__, "vessels": len(vessels),
                "data_ts": rt.store.updated_ts or None, "server_ts": time.time(), "model": rt.model_version,
                "crowding": rt.crowding is not None, "database": rt.storage is not None}

    @app.get("/api/network")
    def network() -> JSONResponse:
        if "geojson" not in network_cache:
            gj = rt.gtfs.to_geojson()
            routes = [{"route_id": r.route_id, "short_name": r.short_name, "name": r.long_name, "color": f"#{r.color}"}
                      for r in rt.gtfs.routes.values() if rt.gtfs.patterns_for_route(r.route_id)]
            network_cache["geojson"] = {"routes": routes, "geojson": gj}
        return JSONResponse(network_cache["geojson"], headers={"Cache-Control": "no-cache"})

    @app.get("/api/vessels")
    def vessels() -> dict:
        return {"ts": now(), "vessels": rt.store.get_vessels(now())}

    @app.get("/api/vessels/{mmsi}")
    def vessel(mmsi: int) -> dict:
        st = rt.store.get_vessel(mmsi)
        if st is None:
            raise HTTPException(404, "vessel not tracked")
        return {"vessel": st, "predictions": rt.store.get_predictions(mmsi)}

    @app.get("/api/vessels/{mmsi}/track")
    async def track(mmsi: int, minutes: int = 20) -> dict:
        minutes = max(1, min(minutes, 240))
        if rt.storage is not None:
            return {"mmsi": mmsi, "points": await rt.storage.track(mmsi, minutes)}
        tr = rt.pipeline.tracker.tracks.get(mmsi) if rt.pipeline is not None else None
        pts = [{"ts": f.ts, "lat": f.lat, "lon": f.lon, "sog_kn": f.sog_kn} for f in (tr.recent(minutes * 60) if tr else [])]
        return {"mmsi": mmsi, "points": pts}

    @app.get("/api/stops/{stop_id}/arrivals")
    def arrivals(stop_id: str, limit: int = 6) -> dict:
        stop = rt.gtfs.stops.get(stop_id)
        if stop is None:
            raise HTTPException(404, "unknown stop")
        t = now()
        preds = rt.store.arrivals_at(stop_id, now=t, limit=limit)
        out, covered = [], set()
        for p in preds:
            route = rt.gtfs.routes.get(p["route_id"])
            pattern = next((pp for pp in rt.gtfs.patterns_for_route(p["route_id"]) if pp.direction_id == p["direction_id"]), None)
            terminates = pattern is not None and pattern.stop_ids[-1] == stop_id
            covered.add(p["trip_id"])
            out.append({**p, "route_name": route.long_name if route else p["route_id"],
                        "route_color": f"#{route.color}" if route else None,
                        "headsign": None if terminates else (pattern.headsign if pattern else None),
                        "origin": rt.gtfs.stops[pattern.stop_ids[0]].name if pattern else None,
                        "terminates": terminates,
                        "minutes": round(max(0.0, p["eta_p50"] - t) / 60, 1),
                        "minutes_range": [round(max(0.0, p["eta_p10"] - t) / 60, 1), round(max(0.0, p["eta_p90"] - t) / 60, 1)]})
        # timetabled departures that no tracked boat is predicted for yet (e.g. trips starting here)
        scheduled = []
        for inst in rt.gtfs.instances_between(t, t + 3600):
            if inst.trip_id in covered or stop_id not in inst.stop_ids[:-1]:
                continue
            k = inst.stop_ids.index(stop_id)
            dep = float(inst.dep[k])
            if t - 60 <= dep <= t + 3600:
                route = rt.gtfs.routes.get(inst.route_id)
                pattern = rt.gtfs.patterns.get(inst.pattern_id)
                scheduled.append({"trip_id": inst.trip_id, "route_id": inst.route_id,
                                  "route_name": route.long_name if route else inst.route_id,
                                  "route_color": f"#{route.color}" if route else None,
                                  "headsign": pattern.headsign if pattern else None, "departure_ts": dep})
        scheduled.sort(key=lambda d: d["departure_ts"])
        return {"stop": {"stop_id": stop.stop_id, "name": stop.name, "lat": stop.lat, "lon": stop.lon},
                "ts": t, "arrivals": out, "scheduled_departures": scheduled[:4]}

    @app.get("/api/model")
    def model() -> dict:
        path = Path(rt.settings.model_dir) / "metrics.json"
        metrics_json = json.loads(path.read_text()) if path.exists() else None
        brief = None
        if metrics_json:
            eta = metrics_json["eta"]
            brief = {"mae_s": {k: v["mae_s"] for k, v in eta["overall"].items()},
                     "interval": eta["interval"], "improvement_vs_official_pct": eta["improvement_vs_official_pct"],
                     "source": metrics_json["info"]["source"]}
        return {"version": rt.model_version, "evaluation": brief}

    @app.get("/api/accuracy")
    async def accuracy(hours: int = 24) -> dict:
        if rt.storage is None:
            raise HTTPException(503, "live accuracy needs DATABASE_URL")
        rows = await rt.storage.accuracy(max(1, min(hours, 24 * 30)))
        return {"hours": hours, "by_route": [{k: (float(v) if isinstance(v, (int, float)) or hasattr(v, "is_finite") else v)
                                              for k, v in r.items()} for r in rows]}

    @app.get("/metrics")
    def prometheus() -> Response:
        metrics.VESSELS.set(len(rt.store.get_vessels(now())))
        return Response(metrics.generate_latest(), media_type=metrics.CONTENT_TYPE_LATEST)

    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        await ws.accept()
        q = rt.hub.register()
        try:
            await ws.send_text(json.dumps({"type": "snapshot", "ts": now(), "vessels": rt.store.get_vessels(now()),
                                           "predictions": {str(m): rt.store.get_predictions(m)
                                                           for m in list(rt.store.predictions)}}))
            while True:
                payload = await q.get()
                await ws.send_text(payload)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            rt.hub.unregister(q)

    web = Path(rt.settings.web_dist)
    if (web / "index.html").exists():
        app.mount("/", WebFiles(directory=str(web), html=True), name="web")
    else:
        @app.get("/")
        def root() -> dict:
            return {"message": "Track My Ferry API. Build the map with `cd web && npm install && npm run build`.",
                    "docs": "/docs"}

    return app
