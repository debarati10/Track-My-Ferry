"""Runtime settings, read from environment variables (see .env.example).

Every service (ingest workers, processor, API) builds one `Settings` object at start-up, so the
same image runs in any role with different environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _float(name: str, default: float) -> float:
    value = _get(name)
    return float(value) if value is not None else default


def _int(name: str, default: int) -> int:
    value = _get(name)
    return int(value) if value is not None else default


@dataclass(frozen=True)
class Settings:
    # "sim" runs the built-in fleet simulator; "live" connects to AIS + NYC Ferry feeds.
    mode: str = "sim"
    # Message bus: "memory" (single process) or "kafka" (distributed services).
    bus: str = "memory"
    kafka_bootstrap: str = "localhost:9092"
    database_url: str | None = None  # postgresql://user:pass@host:5432/trackmyferry
    redis_url: str | None = None  # redis://localhost:6379/0

    # Live data sources
    aisstream_api_key: str | None = None
    aisstream_url: str = "wss://stream.aisstream.io/v0/stream"
    gtfs_static_url: str = "http://nycferry.connexionz.net/rtt/public/resource/gtfs.zip"
    gtfs_static_path: str | None = None  # local zip or folder; overrides the URL
    gtfs_rt_trip_updates_url: str = (
        "https://nycferry.connexionz.net/rtt/public/utility/gtfsrealtime.aspx/tripupdate"
    )
    gtfs_rt_alerts_url: str = "https://nycferry.connexionz.net/rtt/public/utility/gtfsrealtime.aspx/alert"
    gtfs_rt_poll_s: float = 30.0
    # NY Harbor bounding box: south, west, north, east
    bbox: tuple[float, float, float, float] = (40.54, -74.12, 40.83, -73.78)
    noaa_current_station: str = "NYH1924"  # Hell Gate, East River
    noaa_current_bin: int | None = None
    noaa_tide_station: str = "8518750"  # The Battery
    weather_lat: float = 40.73
    weather_lon: float = -73.97
    env_poll_s: float = 600.0
    ridership_dataset: str = "t5n6-gx8c"  # NYC Open Data: NYC Ferry Ridership (hourly boardings)

    # Paths
    data_dir: Path = field(default_factory=lambda: Path("data"))
    model_dir: Path = field(default_factory=lambda: Path("models"))
    web_dist: Path = field(default_factory=lambda: Path("web/dist"))

    # Simulator
    sim_speedup: float = 1.0
    sim_seed: int = 7
    sim_include_vehicle_in_rt: bool = False  # if True, GTFS-RT names the vessel on each trip

    # Prediction / crowding
    vessel_capacity: int = 150
    prediction_interval_s: float = 15.0
    max_stops_ahead: int = 8

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @classmethod
    def from_env(cls) -> Settings:
        bbox = _get("TRACKMYFERRY_BBOX")
        return cls(
            mode=_get("TRACKMYFERRY_MODE", "sim"),
            bus=_get("TRACKMYFERRY_BUS", "memory"),
            kafka_bootstrap=_get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            database_url=_get("DATABASE_URL"),
            redis_url=_get("REDIS_URL"),
            aisstream_api_key=_get("AISSTREAM_API_KEY"),
            gtfs_static_url=_get("GTFS_STATIC_URL", cls.gtfs_static_url),
            gtfs_static_path=_get("GTFS_STATIC_PATH"),
            gtfs_rt_trip_updates_url=_get("GTFS_RT_TRIP_UPDATES_URL", cls.gtfs_rt_trip_updates_url),
            gtfs_rt_alerts_url=_get("GTFS_RT_ALERTS_URL", cls.gtfs_rt_alerts_url),
            gtfs_rt_poll_s=_float("GTFS_RT_POLL_S", 30.0),
            bbox=tuple(float(x) for x in bbox.split(",")) if bbox else cls.bbox,  # type: ignore[arg-type]
            noaa_current_station=_get("NOAA_CURRENT_STATION", "NYH1924"),
            noaa_current_bin=int(_get("NOAA_CURRENT_BIN")) if _get("NOAA_CURRENT_BIN") else None,
            noaa_tide_station=_get("NOAA_TIDE_STATION", "8518750"),
            env_poll_s=_float("ENV_POLL_S", 600.0),
            data_dir=Path(_get("TRACKMYFERRY_DATA_DIR", "data")),
            model_dir=Path(_get("TRACKMYFERRY_MODEL_DIR", "models")),
            web_dist=Path(_get("TRACKMYFERRY_WEB_DIST", "web/dist")),
            sim_speedup=_float("SIM_SPEEDUP", 1.0),
            sim_seed=_int("SIM_SEED", 7),
            sim_include_vehicle_in_rt=_get("SIM_INCLUDE_VEHICLE_IN_RT", "0") == "1",
            vessel_capacity=_int("VESSEL_CAPACITY", 150),
            prediction_interval_s=_float("PREDICTION_INTERVAL_S", 15.0),
            max_stops_ahead=_int("MAX_STOPS_AHEAD", 8),
            api_host=_get("API_HOST", "0.0.0.0"),
            api_port=_int("API_PORT", 8000),
        )
