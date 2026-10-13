"""Batched PostgreSQL writer (psycopg 3, async) plus a few read queries for the API.

Messages are buffered per table and flushed every second (or every `batch_size` rows) with COPY,
which keeps the database comfortably ahead of the stream.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..messages import EnvObservation, Message, Prediction, StopEvent, TripUpdate, VesselPosition, VesselState

log = logging.getLogger(__name__)
SCHEMA = Path(__file__).with_name("schema.sql")


def _ts(x: float | None) -> datetime | None:
    return None if x is None else datetime.fromtimestamp(float(x), UTC)


COPY_COLUMNS = {
    "vessel_positions": ("ts", "mmsi", "lat", "lon", "sog_kn", "cog", "heading", "name"),
    "vessel_states": ("ts", "mmsi", "lat", "lon", "sog_kn", "status", "route_id", "direction_id", "trip_id",
                      "along_m", "next_stop_id", "delay_s", "match_confidence"),
    "trip_updates": ("ts", "trip_id", "start_date", "stop_id", "arrival_ts", "departure_ts", "delay_s"),
    "predictions": ("made_at", "mmsi", "trip_id", "route_id", "direction_id", "stop_id", "stops_ahead", "eta_p10",
                    "eta_p50", "eta_p90", "official_eta", "scheduled_eta", "p_full", "model_version"),
    "env_observations": ("ts", "kind", "values", "source"),
}

UPSERT_STOP_EVENT = """
INSERT INTO stop_events (mmsi, stop_id, arrival_ts, departure_ts, trip_id, route_id, direction_id, service_date, vessel_name)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (mmsi, stop_id, arrival_ts) DO UPDATE SET
  departure_ts = COALESCE(EXCLUDED.departure_ts, stop_events.departure_ts),
  trip_id = COALESCE(EXCLUDED.trip_id, stop_events.trip_id),
  route_id = COALESCE(EXCLUDED.route_id, stop_events.route_id),
  direction_id = COALESCE(EXCLUDED.direction_id, stop_events.direction_id),
  service_date = COALESCE(EXCLUDED.service_date, stop_events.service_date),
  vessel_name = COALESCE(EXCLUDED.vessel_name, stop_events.vessel_name)
"""


def rows_for(msg: Message) -> list[tuple[str, tuple]]:
    """Map a message to (table, row) pairs."""
    if isinstance(msg, VesselPosition):
        return [("vessel_positions", (_ts(msg.ts), msg.mmsi, msg.lat, msg.lon, msg.sog_kn, msg.cog, msg.heading, msg.name))]
    if isinstance(msg, VesselState):
        return [("vessel_states", (_ts(msg.ts), msg.mmsi, msg.lat, msg.lon, msg.sog_kn, msg.status, msg.route_id,
                                   msg.direction_id, msg.trip_id, msg.along_m, msg.next_stop_id, msg.delay_s,
                                   msg.match_confidence))]
    if isinstance(msg, TripUpdate):
        return [("trip_updates", (_ts(msg.ts), msg.trip_id, msg.start_date, s.stop_id, _ts(s.arrival_ts),
                                  _ts(s.departure_ts), s.arrival_delay_s)) for s in msg.stop_updates]
    if isinstance(msg, Prediction):
        return [("predictions", (_ts(msg.made_at), msg.mmsi, msg.trip_id, msg.route_id, msg.direction_id, msg.stop_id,
                                 msg.stops_ahead, _ts(msg.eta_p10), _ts(msg.eta_p50), _ts(msg.eta_p90),
                                 _ts(msg.official_eta), _ts(msg.scheduled_eta), msg.p_full, msg.model_version))]
    if isinstance(msg, EnvObservation):
        return [("env_observations", (_ts(msg.ts), msg.kind, json.dumps(msg.values), msg.source))]
    if isinstance(msg, StopEvent):
        return [("stop_events", (msg.mmsi, msg.stop_id, _ts(msg.arrival_ts), _ts(msg.departure_ts), msg.trip_id,
                                 msg.route_id, msg.direction_id, msg.service_date, msg.vessel_name))]
    return []


class Storage:
    def __init__(self, dsn: str, batch_size: int = 1000, flush_s: float = 1.0,
                 tables: set[str] | None = None):
        self.dsn = dsn
        self.batch_size = batch_size
        self.flush_s = flush_s
        self.tables = tables  # None = store everything
        self.conn = None
        self._buf: dict[str, list[tuple]] = {}
        self._lock = asyncio.Lock()
        self.written = 0

    async def start(self) -> None:
        import psycopg  # pip install "psycopg[binary]"

        self.conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
        await self.conn.execute(SCHEMA.read_text())

    async def stop(self) -> None:
        await self.flush()
        if self.conn is not None:
            await self.conn.close()

    def add(self, msg: Message) -> None:
        for table, row in rows_for(msg):
            if self.tables is None or table in self.tables:
                self._buf.setdefault(table, []).append(row)

    def pending(self) -> int:
        return sum(len(v) for v in self._buf.values())

    async def flush(self) -> int:
        if self.conn is None:
            return 0
        async with self._lock:
            buf, self._buf = self._buf, {}
            n = 0
            for table, rows in buf.items():
                if not rows:
                    continue
                try:
                    if table == "stop_events":
                        async with self.conn.cursor() as cur:
                            await cur.executemany(UPSERT_STOP_EVENT, rows)
                    else:
                        cols = COPY_COLUMNS[table]
                        quoted = ", ".join(f'"{c}"' for c in cols)
                        async with self.conn.cursor() as cur:
                            async with cur.copy(f"COPY {table} ({quoted}) FROM STDIN") as copy:
                                for row in rows:
                                    await copy.write_row(row)
                    n += len(rows)
                except Exception:
                    log.exception("failed to write %d rows to %s", len(rows), table)
            self.written += n
            from .. import metrics

            metrics.DB_ROWS.inc(n)
            return n

    async def run_flusher(self) -> None:
        while True:
            await asyncio.sleep(self.flush_s)
            if self.pending():
                await self.flush()

    async def maybe_flush(self) -> None:
        if self.pending() >= self.batch_size:
            await self.flush()

    # ------------------------------------------------------------------ reads used by the API
    async def fetch(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    async def track(self, mmsi: int, minutes: int = 30) -> list[dict[str, Any]]:
        return await self.fetch(
            "SELECT extract(epoch FROM ts) AS ts, lat, lon, sog_kn FROM vessel_positions "
            "WHERE mmsi = %s AND ts > now() - make_interval(mins => %s) ORDER BY ts", (mmsi, minutes))

    async def accuracy(self, hours: int = 24) -> list[dict[str, Any]]:
        return await self.fetch(
            "SELECT route_id, count(*) AS n, avg(abs(error_s)) AS mae_s, avg(abs(official_error_s)) AS official_mae_s, "
            "avg(inside_band::int) AS coverage FROM prediction_errors "
            "WHERE made_at > now() - make_interval(hours => %s) GROUP BY route_id ORDER BY route_id", (hours,))
