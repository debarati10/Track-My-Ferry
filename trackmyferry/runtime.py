"""Process wiring for every deployment shape.

* `all-in-one` (default, `trackmyferry run`): sources + processor + API in one process on the
  in-memory bus. Simplest way to demo or to run on one small VM.
* distributed (docker compose): `ingest-*` services publish to Kafka, `process` runs the pipeline
  and writes PostgreSQL/Redis, `api` replicas serve the map from Kafka's derived topics.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from . import metrics
from .api.hub import Hub
from .bus import MemoryBus, make_bus, publish_message
from .config import Settings
from .gtfs.static import GTFS
from .messages import Message, Prediction, StopEvent, Topics, VesselState, decode
from .processing.pipeline import Pipeline
from .storage.cache import make_store

log = logging.getLogger(__name__)


def load_gtfs(settings: Settings) -> GTFS:
    if settings.gtfs_static_path:
        return GTFS.load(settings.gtfs_static_path)
    if settings.mode == "sim":
        from .sim.sample_network import SAMPLE_GTFS_DIR

        return GTFS.load(SAMPLE_GTFS_DIR)
    return GTFS.load(settings.gtfs_static_url)


def load_models(settings: Settings, gtfs: GTFS):
    eta = crowding = None
    from .ml.crowding import CrowdingModel
    from .ml.eta_model import QuantileEtaModel

    if QuantileEtaModel.exists(settings.model_dir):
        try:
            eta = QuantileEtaModel.load(settings.model_dir)
            log.info("loaded ETA model %s", eta.version)
        except Exception as exc:
            log.warning("could not load ETA model (%s); using physics baseline", exc)
    if CrowdingModel.exists(settings.model_dir):
        try:
            crowding = CrowdingModel.load(settings.model_dir, gtfs)
            crowding.capacity = settings.vessel_capacity
        except Exception as exc:
            log.warning("could not load crowding model (%s)", exc)
    return eta, crowding


class Runtime:
    def __init__(self, settings: Settings, gtfs: GTFS | None = None, in_process: bool = True):
        self.settings = settings
        self.gtfs = gtfs or load_gtfs(settings)
        self.eta, self.crowding = load_models(settings, self.gtfs)
        self.bus = make_bus(settings.bus, settings.kafka_bootstrap) if not in_process else MemoryBus()
        self.store = make_store(settings.redis_url)
        self.hub = Hub()
        self.storage = None
        self.pipeline: Pipeline | None = None
        if in_process:
            self.pipeline = self._make_pipeline()
        self._tasks: list[asyncio.Task] = []
        self._sim = None

    @property
    def model_version(self) -> str:
        return self.eta.version if self.eta is not None else "physics-baseline"

    def clock(self) -> float:
        """'Now' for the API: simulated time in sim mode (the demo can run sped up)."""
        if self._sim is not None:
            return self._sim.clock
        if self.settings.mode == "sim":  # simulator runs in another service; follow the data's clock
            return self.store.updated_ts or time.time()
        return time.time()

    def _make_pipeline(self) -> Pipeline:
        registry = None
        if self.settings.mode == "live":
            fleet = Path(self.settings.data_dir) / "fleet.json"
            if fleet.exists():
                from .processing.fleet import FleetRegistry

                used = {s for p in self.gtfs.patterns.values() for s in p.stop_ids}
                registry = FleetRegistry.from_file(fleet, {s: self.gtfs.stops[s].name for s in used})
        priors = self.eta.priors if self.eta is not None else None
        return Pipeline(self.gtfs, eta_model=self.eta, crowding=self.crowding, registry=registry, priors=priors,
                        prediction_interval_s=self.settings.prediction_interval_s,
                        max_stops_ahead=self.settings.max_stops_ahead)

    # ------------------------------------------------------------------ lifecycle
    async def start_storage(self) -> None:
        if self.settings.database_url:
            from .storage.db import Storage

            self.storage = Storage(self.settings.database_url)
            await self.storage.start()
            self._tasks.append(asyncio.create_task(self.storage.run_flusher(), name="db-flusher"))

    async def start_all_in_one(self) -> None:
        await self.bus.start()
        await self.start_storage()
        self._tasks.append(asyncio.create_task(self.hub.run(), name="hub"))
        self._tasks.append(asyncio.create_task(self.process_loop(), name="processor"))
        await asyncio.sleep(0)  # let the processor subscribe before sources publish
        for coro in self.source_coroutines():
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            # runs even when Ctrl+C cancels the wait, so buffered rows are still flushed
            if self.storage is not None:
                await self.storage.stop()
            await self.bus.stop()

    # ------------------------------------------------------------------ sources
    def source_coroutines(self):
        if self.settings.mode == "sim":
            return [self.run_simulator()]
        return [self.run_ais(), self.run_trip_updates(), self.run_alerts(), self.run_environment()]

    async def run_simulator(self) -> None:
        from .sim.simulator import FleetSim, SimConfig, default_demo_start, run_realtime

        self._sim = FleetSim(self.gtfs, SimConfig(seed=self.settings.sim_seed,
                                                  include_vehicle_in_rt=self.settings.sim_include_vehicle_in_rt,
                                                  capacity=self.settings.vessel_capacity))
        if self.pipeline is not None:  # the demo fleet is known; skip discovery
            self.pipeline.registry.allow.update(range(366_900_000, 366_901_000))

        async def publish(msg: Message) -> None:
            await publish_message(self.bus, msg)

        await run_realtime(self._sim, publish, speedup=self.settings.sim_speedup, start_ts=default_demo_start())

    async def _pump(self, agen) -> None:
        async for msg in agen:
            await publish_message(self.bus, msg)

    async def run_ais(self) -> None:
        from .ingest.ais import stream_aisstream

        if not self.settings.aisstream_api_key:
            log.error("AISSTREAM_API_KEY is not set: live vessel positions are disabled")
            return
        await self._pump(stream_aisstream(self.settings.aisstream_api_key, self.settings.bbox, self.settings.aisstream_url))

    async def run_trip_updates(self) -> None:
        from .ingest.gtfsrt import poll_trip_updates

        await self._pump(poll_trip_updates(self.settings.gtfs_rt_trip_updates_url, self.settings.gtfs_rt_poll_s))

    async def run_alerts(self) -> None:
        from .ingest.gtfsrt import poll_alerts

        await self._pump(poll_alerts(self.settings.gtfs_rt_alerts_url))

    async def run_environment(self) -> None:
        from .ingest.environment import poll_environment

        s = self.settings
        await self._pump(poll_environment(s.noaa_current_station, s.weather_lat, s.weather_lon, s.env_poll_s,
                                          s.noaa_current_bin))

    # ------------------------------------------------------------------ processing
    async def process_loop(self, publish_derived: bool = False) -> None:
        assert self.pipeline is not None
        async for topic, payload in self.bus.subscribe(Topics.INGEST, group="processor"):
            t0 = time.perf_counter()
            ingest_ts = payload.pop("_ingest_ts", None)
            msg = decode(topic, payload)
            metrics.MESSAGES.labels(topic).inc()
            try:
                outputs = self.pipeline.handle(msg)
            except Exception:
                log.exception("pipeline failed on %s", topic)
                continue
            if self.storage is not None and topic in (Topics.AIS_POSITIONS, Topics.TRIP_UPDATES, Topics.ENV):
                if not (topic == Topics.AIS_POSITIONS and not self.pipeline.registry.is_ferry(msg.mmsi)):
                    self.storage.add(msg)
            self.apply_outputs(outputs)
            if publish_derived:
                for out in outputs:
                    await publish_message(self.bus, out, ingest_ts)
            metrics.PROCESS_SECONDS.observe(time.perf_counter() - t0)
            if ingest_ts and any(isinstance(o, VesselState) for o in outputs):
                metrics.END_TO_END.observe(time.time() - ingest_ts)

    def apply_outputs(self, outputs: list[Message]) -> None:
        burst: dict[int, list[dict]] = {}
        for out in outputs:
            metrics.OUTPUTS.labels(out.topic).inc()
            if isinstance(out, VesselState):
                d = out.to_dict()
                self.store.put_state(d)
                self.hub.vessel(d)
            elif isinstance(out, Prediction):
                burst.setdefault(out.mmsi, []).append(out.to_dict())
            if self.storage is not None and isinstance(out, (VesselState, StopEvent, Prediction)):
                self.storage.add(out)
        for mmsi, preds in burst.items():
            self.store.put_predictions(mmsi, preds)
            self.hub.predictions(mmsi, preds)

    async def follow_derived(self) -> None:
        """API replicas in the distributed deployment: mirror derived topics into the store + hub."""
        group = f"api-{uuid.uuid4().hex[:8]}"  # unique group: every replica sees every message
        async for topic, payload in self.bus.subscribe(Topics.DERIVED, group=group):
            payload.pop("_ingest_ts", None)
            if topic == Topics.VESSEL_STATE:
                self.store.put_state(payload)
                self.hub.vessel(payload)
            elif topic == Topics.PREDICTIONS:
                self.store.add_prediction(payload)
                mmsi = int(payload["mmsi"])
                self.hub.predictions(mmsi, self.store.get_predictions(mmsi))


async def run_ingest_service(settings: Settings, source: str) -> None:
    rt = Runtime(settings, in_process=False)
    await rt.bus.start()
    coro = {"ais": rt.run_ais, "trip_updates": rt.run_trip_updates, "alerts": rt.run_alerts,
            "env": rt.run_environment, "sim": rt.run_simulator}[source]()
    try:
        await coro
    finally:
        await rt.bus.stop()


async def run_processor_service(settings: Settings, metrics_port: int = 9100) -> None:
    try:  # the processor has no HTTP server of its own; expose Prometheus metrics separately
        from prometheus_client import start_http_server

        start_http_server(metrics_port)
    except Exception as exc:
        log.warning("metrics server not started: %s", exc)
    rt = Runtime(settings, in_process=False)
    rt.pipeline = rt._make_pipeline()
    if settings.mode == "sim":
        rt.pipeline.registry.allow.update(range(366_900_000, 366_901_000))
    await rt.bus.start()
    await rt.start_storage()
    try:
        await rt.process_loop(publish_derived=True)
    finally:
        await rt.stop()
