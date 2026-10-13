"""Prometheus metrics (no-ops if prometheus_client isn't installed)."""
from __future__ import annotations

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

    ENABLED = True
except ImportError:  # pragma: no cover
    ENABLED = False

    class _Noop:
        def __init__(self, *a, **k): ...
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): ...
        def set(self, *a, **k): ...
        def observe(self, *a, **k): ...

    Counter = Gauge = Histogram = _Noop  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest(*a, **k) -> bytes:  # type: ignore
        return b""

MESSAGES = Counter("trackmyferry_messages_total", "Messages handled by the processor", ["topic"])
OUTPUTS = Counter("trackmyferry_outputs_total", "Derived messages emitted", ["topic"])
PROCESS_SECONDS = Histogram("trackmyferry_process_seconds", "Time to process one input message",
                            buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1))
END_TO_END = Histogram("trackmyferry_end_to_end_seconds", "Bus ingest -> vessel state published",
                       buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10))
FEED_LAG = Histogram("trackmyferry_feed_lag_seconds", "Message timestamp -> processed (source + network delay)",
                     buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300))
VESSELS = Gauge("trackmyferry_vessels_tracked", "Fleet vessels currently tracked")
WS_CLIENTS = Gauge("trackmyferry_ws_clients", "Connected live-map clients")
DB_ROWS = Counter("trackmyferry_db_rows_total", "Rows written to PostgreSQL")
