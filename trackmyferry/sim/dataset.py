"""Generate a simulated multi-week dataset in the recorder's format, plus ground truth.

Output layout (same as the live recorder, so training code can't tell the difference):

    <out>/raw/<topic>/<YYYY-MM-DD>.jsonl.gz     messages, exactly as they'd arrive live
    <out>/truth/stop_visits.jsonl.gz            real arrival/departure times, loads, left-behind
    <out>/ridership.csv                         hourly boardings (NYC Open Data schema)
    <out>/meta.json
"""
from __future__ import annotations

import csv
import gzip
import json
import time
from datetime import date
from pathlib import Path

from ..gtfs.static import GTFS
from ..record import Recorder
from .simulator import FleetSim, SimConfig


def generate(gtfs: GTFS, out: Path | str, start: date, days: int, seed: int = 7, progress: bool = True) -> dict:
    out = Path(out)
    (out / "truth").mkdir(parents=True, exist_ok=True)
    sim = FleetSim(gtfs, SimConfig(seed=seed))
    t0 = time.time()
    n = 0
    with Recorder(out / "raw") as rec:
        for i in range(days):
            day = date.fromordinal(start.toordinal() + i)
            for msg in sim.iter_days(day, 1):
                rec.write(msg)
                n += 1
            if progress:
                print(f"  simulated {day} ({n:,} messages, {time.time() - t0:.0f}s)", flush=True)
    with gzip.open(out / "truth" / "stop_visits.jsonl.gz", "wt") as fh:
        for row in sim.truth:
            fh.write(json.dumps(row) + "\n")
    rows = sim.ridership_rows()
    with open(out / "ridership.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "hour", "route", "direction", "stop", "boardings", "typeday"])
        w.writeheader()
        w.writerows(rows)
    meta = {"start": start.isoformat(), "days": days, "seed": seed, "messages": n,
            "vessels": {r: len(v) for r, v in sim.fleet.items()}, "capacity": sim.cfg.capacity,
            "generated_s": round(time.time() - t0, 1)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta
