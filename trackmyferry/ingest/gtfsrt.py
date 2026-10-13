"""Poll NYC Ferry's GTFS-realtime TripUpdates and Alerts feeds."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import requests

from ..gtfs.realtime import parse_alerts, parse_trip_updates
from ..messages import ServiceAlert, TripUpdate

log = logging.getLogger(__name__)


class FeedPoller:
    def __init__(self, url: str, timeout_s: float = 15.0):
        self.url = url
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "trackmyferry/0.1 (+https://github.com/debarati10)"
        self._last_feed_ts: float | None = None

    def fetch(self) -> bytes | None:
        resp = self.session.get(self.url, timeout=self.timeout_s)
        resp.raise_for_status()
        return resp.content or None


async def poll_trip_updates(url: str, interval_s: float = 30.0) -> AsyncIterator[TripUpdate]:
    """Yields trip updates from each new feed snapshot (unchanged snapshots are skipped)."""
    poller = FeedPoller(url)
    last_ts = None
    while True:
        try:
            data = await asyncio.to_thread(poller.fetch)
            if data:
                feed_ts, updates = parse_trip_updates(data)
                if feed_ts != last_ts:
                    last_ts = feed_ts
                    for u in updates:
                        yield u
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("trip updates poll failed: %s", exc)
        await asyncio.sleep(interval_s)


async def poll_alerts(url: str, interval_s: float = 120.0) -> AsyncIterator[ServiceAlert]:
    poller = FeedPoller(url)
    seen: dict[str, str] = {}
    while True:
        try:
            data = await asyncio.to_thread(poller.fetch)
            if data:
                for a in parse_alerts(data):
                    sig = f"{a.header}|{a.description}|{a.active_to}"
                    if seen.get(a.alert_id) != sig:
                        seen[a.alert_id] = sig
                        yield a
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("alerts poll failed: %s", exc)
        await asyncio.sleep(interval_s)
