"""Canonical messages that flow over the bus between services.

Every message is a plain dataclass that round-trips through JSON (`to_dict` / `from_dict`), so the
same objects work on the in-memory bus, on Kafka, in the recorder's JSONL logs and in replays.
Timestamps are Unix epoch seconds (UTC).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, ClassVar, TypeVar

T = TypeVar("T", bound="Message")


class Topics:
    AIS_POSITIONS = "ais.positions"
    AIS_STATIC = "ais.static"
    TRIP_UPDATES = "gtfsrt.trip_updates"
    ALERTS = "gtfsrt.alerts"
    ENV = "env.observations"
    VESSEL_STATE = "vessels.state"
    STOP_EVENTS = "vessels.stop_events"
    PREDICTIONS = "predictions.eta"

    INGEST = (AIS_POSITIONS, AIS_STATIC, TRIP_UPDATES, ALERTS, ENV)
    DERIVED = (VESSEL_STATE, STOP_EVENTS, PREDICTIONS)


@dataclass
class Message:
    topic: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def key(self) -> str | None:
        return None


@dataclass
class VesselPosition(Message):
    topic: ClassVar[str] = Topics.AIS_POSITIONS
    mmsi: int
    ts: float
    lat: float
    lon: float
    sog_kn: float | None = None  # speed over ground
    cog: float | None = None  # course over ground, degrees
    heading: float | None = None
    nav_status: int | None = None
    name: str | None = None
    source: str = "ais"

    def key(self) -> str:
        return str(self.mmsi)


@dataclass
class VesselStatic(Message):
    topic: ClassVar[str] = Topics.AIS_STATIC
    mmsi: int
    ts: float
    name: str | None = None
    ship_type: int | None = None
    call_sign: str | None = None
    length_m: float | None = None
    destination: str | None = None

    def key(self) -> str:
        return str(self.mmsi)


@dataclass
class StopTimeUpdate:
    stop_id: str
    stop_sequence: int | None = None
    arrival_ts: float | None = None
    departure_ts: float | None = None
    arrival_delay_s: int | None = None
    departure_delay_s: int | None = None
    skipped: bool = False


@dataclass
class TripUpdate(Message):
    """One trip from the operator's GTFS-realtime TripUpdates feed (the 'official' predictions)."""

    topic: ClassVar[str] = Topics.TRIP_UPDATES
    ts: float
    trip_id: str
    route_id: str | None = None
    direction_id: int | None = None
    start_date: str | None = None
    vehicle_id: str | None = None
    vehicle_label: str | None = None
    stop_updates: list[StopTimeUpdate] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TripUpdate:
        d = dict(data)
        d["stop_updates"] = [
            s if isinstance(s, StopTimeUpdate) else StopTimeUpdate(**s) for s in d.get("stop_updates", [])
        ]
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def key(self) -> str:
        return self.trip_id


@dataclass
class ServiceAlert(Message):
    topic: ClassVar[str] = Topics.ALERTS
    ts: float
    alert_id: str
    header: str | None = None
    description: str | None = None
    route_ids: list[str] = field(default_factory=list)
    stop_ids: list[str] = field(default_factory=list)
    active_from: float | None = None
    active_to: float | None = None


@dataclass
class EnvObservation(Message):
    """Environment reading. kind = 'current' (kn, + = flood), 'wind' (kn, deg), 'weather', 'tide'."""

    topic: ClassVar[str] = Topics.ENV
    ts: float
    kind: str
    values: dict[str, float] = field(default_factory=dict)
    source: str = "sim"


@dataclass
class StopEvent(Message):
    """A detected arrival at (and later departure from) a ferry landing."""

    topic: ClassVar[str] = Topics.STOP_EVENTS
    mmsi: int
    stop_id: str
    arrival_ts: float
    departure_ts: float | None = None
    trip_id: str | None = None
    route_id: str | None = None
    direction_id: int | None = None
    service_date: str | None = None
    vessel_name: str | None = None

    @property
    def dwell_s(self) -> float | None:
        return None if self.departure_ts is None else self.departure_ts - self.arrival_ts

    def key(self) -> str:
        return str(self.mmsi)


@dataclass
class VesselState(Message):
    """What the live map shows for one boat."""

    topic: ClassVar[str] = Topics.VESSEL_STATE
    mmsi: int
    ts: float
    lat: float
    lon: float
    name: str | None = None
    sog_kn: float | None = None
    cog: float | None = None
    heading: float | None = None
    status: str = "unassigned"  # underway | docked | unassigned
    route_id: str | None = None
    direction_id: int | None = None
    trip_id: str | None = None
    pattern_id: str | None = None
    along_m: float | None = None
    next_stop_id: str | None = None
    current_stop_id: str | None = None
    delay_s: float | None = None
    match_confidence: float | None = None

    def key(self) -> str:
        return str(self.mmsi)


@dataclass
class Prediction(Message):
    """Predicted arrival of a vessel/trip at a downstream stop, with an uncertainty band."""

    topic: ClassVar[str] = Topics.PREDICTIONS
    made_at: float
    mmsi: int
    trip_id: str
    route_id: str
    direction_id: int
    stop_id: str
    stops_ahead: int
    eta_p10: float
    eta_p50: float
    eta_p90: float
    official_eta: float | None = None
    scheduled_eta: float | None = None
    vessel_name: str | None = None
    p_full: float | None = None  # chance the boat is at capacity when it reaches this stop
    model_version: str = "unknown"

    def key(self) -> str:
        return f"{self.trip_id}:{self.stop_id}"


MESSAGE_TYPES: dict[str, type[Message]] = {
    Topics.AIS_POSITIONS: VesselPosition,
    Topics.AIS_STATIC: VesselStatic,
    Topics.TRIP_UPDATES: TripUpdate,
    Topics.ALERTS: ServiceAlert,
    Topics.ENV: EnvObservation,
    Topics.STOP_EVENTS: StopEvent,
    Topics.VESSEL_STATE: VesselState,
    Topics.PREDICTIONS: Prediction,
}


def decode(topic: str, data: dict[str, Any]) -> Message:
    return MESSAGE_TYPES[topic].from_dict(data)


def message_ts(msg: Message) -> float:
    for attr in ("ts", "made_at", "arrival_ts"):
        if hasattr(msg, attr):
            return float(getattr(msg, attr))
    raise AttributeError(f"{type(msg).__name__} has no timestamp")
