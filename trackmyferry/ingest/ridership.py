"""Download NYC Ferry hourly ridership from NYC Open Data (Socrata API, dataset t5n6-gx8c).

Columns: date, hour, route, direction, stop, boardings, typeday. An app token is optional but
raises rate limits: set SOCRATA_APP_TOKEN.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

import requests

BASE = "https://data.cityofnewyork.us/resource/{dataset}.json"
FIELDS = ["date", "hour", "route", "direction", "stop", "boardings", "typeday"]


def download(out_csv: Path | str, since: str = "2024-01-01", dataset: str = "t5n6-gx8c",
             page_size: int = 50_000, max_rows: int | None = None) -> int:
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    headers = {}
    if os.environ.get("SOCRATA_APP_TOKEN"):
        headers["X-App-Token"] = os.environ["SOCRATA_APP_TOKEN"]
    session = requests.Session()
    n = 0
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        offset = 0
        while True:
            params = {"$where": f"date >= '{since}T00:00:00'", "$order": "date, hour, route, stop",
                      "$limit": page_size, "$offset": offset}
            resp = session.get(BASE.format(dataset=dataset), params=params, headers=headers, timeout=60)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for r in rows:
                w.writerow({k: r.get(k, "") for k in FIELDS})
            n += len(rows)
            offset += page_size
            if max_rows and n >= max_rows:
                break
    return n
