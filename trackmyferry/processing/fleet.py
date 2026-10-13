"""Which AIS targets are NYC Ferry boats?

The AIS feed covers every vessel in the harbour (tugs, barges, Staten Island Ferry, NY Waterway...).
A vessel is treated as part of the fleet if it is on the allowlist, or if it is *discovered*: it
docks at two or more NYC Ferry landings within three hours, at least one of which is not shared
with other operators. Non-passenger ship types (AIS type outside 60-69) are excluded once known.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

SHARED_LANDINGS_DEFAULT = ("Wall St/Pier 11", "East 34th Street", "Midtown West/W 39th St")
DISCOVERY_WINDOW_S = 3 * 3600


class FleetRegistry:
    def __init__(self, stop_names: dict[str, str], allow: set[int] | None = None, deny: set[int] | None = None,
                 auto_discover: bool = True, shared_landings: tuple[str, ...] = SHARED_LANDINGS_DEFAULT):
        self.allow = set(allow or ())
        self.deny = set(deny or ())
        self.auto_discover = auto_discover
        self.shared_stop_ids = {sid for sid, name in stop_names.items() if name in shared_landings}
        self.discovered: set[int] = set()
        self.ship_type: dict[int, int] = {}
        self._visits: dict[int, deque] = defaultdict(lambda: deque(maxlen=20))

    @classmethod
    def from_file(cls, path: Path | str, stop_names: dict[str, str], **kw) -> FleetRegistry:
        data = json.loads(Path(path).read_text())
        allow = {int(v["mmsi"]) for v in data.get("vessels", [])}
        return cls(stop_names, allow=allow, **kw)

    def note_static(self, mmsi: int, ship_type: int | None) -> None:
        if ship_type is not None:
            self.ship_type[mmsi] = int(ship_type)

    def note_visit(self, mmsi: int, stop_id: str, ts: float) -> bool:
        """Record a docking; returns True if this visit made the vessel newly discovered."""
        if mmsi in self.deny or mmsi in self.discovered or not self.auto_discover:
            return False
        visits = self._visits[mmsi]
        visits.append((ts, stop_id))
        recent = {s for t, s in visits if t >= ts - DISCOVERY_WINDOW_S}
        if len(recent) >= 2 and recent - self.shared_stop_ids and self._passenger_type(mmsi):
            self.discovered.add(mmsi)
            return True
        return False

    def _passenger_type(self, mmsi: int) -> bool:
        t = self.ship_type.get(mmsi)
        return t is None or 60 <= t <= 69

    def is_ferry(self, mmsi: int) -> bool:
        if mmsi in self.deny:
            return False
        return mmsi in self.allow or mmsi in self.discovered
