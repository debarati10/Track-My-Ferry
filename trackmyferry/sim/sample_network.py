"""A small, approximate NYC Ferry network used by the simulator and the tests.

Three routes (East River, Astoria, South Brooklyn) with real landing names. Landings sit on the
water's edge, and the waypoints between them were traced through the water on the OpenStreetMap
basemap: shortest paths that keep ~90 m off shore and ~250 m clear of landings a route doesn't
serve, straightened into a few clean legs. The stretches north of Roosevelt Island and south of
Red Hook are still hand-placed. Geometry is approximate (tens of metres) and the timetable is
synthetic; in live mode the real NYC Ferry GTFS feed replaces it.

Regenerate the GTFS files with:  python -m trackmyferry.sim.sample_network
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..geo import KNOT_MS, path_length_m
from ..gtfs.static import format_gtfs_time

HERE = Path(__file__).parent
SAMPLE_GTFS_DIR = HERE / "sample_gtfs"

STOPS: dict[str, tuple[str, float, float]] = {
    "PIER11": ("Wall St/Pier 11", 40.7034, -74.0044),
    "DUMBO": ("Dumbo/Fulton Ferry", 40.7040, -73.9961),
    "SWBG": ("South Williamsburg", 40.7099, -73.9704),
    "NWBG": ("North Williamsburg", 40.7215, -73.9640),
    "GRPT": ("Greenpoint", 40.7306, -73.9627),
    "HPS": ("Hunters Point South", 40.7421, -73.9623),
    "E34": ("East 34th Street", 40.7436, -73.9708),
    "BNY": ("Brooklyn Navy Yard", 40.7022, -73.9748),
    "LIC": ("Long Island City", 40.7458, -73.9598),
    "RI": ("Roosevelt Island", 40.7580, -73.9560),
    "AST": ("Astoria", 40.7720, -73.9346),
    "E90": ("East 90th Street", 40.7777, -73.9420),
    "PIER6": ("Atlantic Ave/BBP Pier 6", 40.6919, -74.0022),
    "REDHOOK": ("Red Hook/Atlantic Basin", 40.6840, -74.0122),
    "BAT": ("Sunset Park/BAT", 40.6457, -74.0283),
    "BAYRIDGE": ("Bay Ridge", 40.6354, -74.0377),
}

# Outbound (direction 0) paths: stop ids interleaved with (lat, lon) waypoints on the water.
# Generated from the basemap (see the module docstring); edit by hand only north of RI / south of Red Hook.
PATHS: dict[str, list] = {
    "ER": [
        "PIER11", "DUMBO", (40.70552, -73.99379), (40.70700, -73.98736), "SWBG", (40.71541, -73.96950),
        (40.71723, -73.96892), (40.71868, -73.96812), (40.71976, -73.96710), "NWBG", "GRPT",
        (40.73485, -73.96362), (40.73776, -73.96386), (40.73897, -73.96373), (40.74083, -73.96316),
        (40.74178, -73.96272), "HPS", "E34",
    ],
    "AS": [
        "PIER11", (40.70625, -73.99690), (40.70671, -73.97807), "BNY", (40.73196, -73.96956), "E34", "LIC",
        (40.75404, -73.96044), "RI", (40.76131, -73.95339), (40.77120, -73.94490), (40.77420, -73.93950),
        "AST", (40.77620, -73.93720), "E90",
    ],
    "SB": [
        "PIER11", "DUMBO", (40.70274, -73.99989), (40.69130, -74.00707), (40.69006, -74.00453),
        (40.69051, -74.00363), "PIER6", (40.69040, -74.00378), (40.69028, -74.00423), (40.68496, -74.01230),
        "REDHOOK", (40.68428, -74.01588), (40.67374, -74.02605), (40.67000, -74.02900), (40.65800, -74.03000),
        "BAT", (40.64050, -74.03450), "BAYRIDGE",
    ],
}

ROUTES = {
    # route_id: (short, long, color, direction names, headsigns)
    "ER": ("ER", "East River", "3FA9F5", ("NB", "SB"), ("East 34th Street", "Wall St/Pier 11")),
    "AS": ("AST", "Astoria", "F5A623", ("NB", "SB"), ("East 90th Street", "Wall St/Pier 11")),
    "SB": ("SB", "South Brooklyn", "B37FEB", ("SB", "NB"), ("Bay Ridge", "Wall St/Pier 11")),
}

# (first departure, last departure, headway minutes) for weekday "WK" and weekend "WE" service.
SERVICE = {
    "ER": {"WK": ("06:30", "21:30", 20), "WE": ("08:00", "21:30", 25)},
    "AS": {"WK": ("06:15", "21:15", 25), "WE": ("08:00", "21:00", 30)},
    "SB": {"WK": ("06:30", "21:00", 30), "WE": ("08:00", "21:00", 40)},
}

SCHEDULE_SPEED_KN = 19.5
DWELL_S = 60
ACCEL_PENALTY_S = 35


def path_coords(route_id: str, direction_id: int) -> tuple[list[tuple[float, float]], list[str]]:
    seq = PATHS[route_id] if direction_id == 0 else list(reversed(PATHS[route_id]))
    coords, stops = [], []
    for item in seq:
        if isinstance(item, str):
            _, lat, lon = STOPS[item]
            coords.append((lat, lon))
            stops.append(item)
        else:
            coords.append(item)
    return coords, stops


def leg_lengths(route_id: str, direction_id: int) -> list[float]:
    """Path length between consecutive stops (metres)."""
    seq = PATHS[route_id] if direction_id == 0 else list(reversed(PATHS[route_id]))
    legs, cur = [], []
    for item in seq:
        pt = (STOPS[item][1], STOPS[item][2]) if isinstance(item, str) else item
        cur.append(pt)
        if isinstance(item, str) and len(cur) > 1:
            legs.append(path_length_m(cur))
            cur = [pt]
    return legs


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def generate(out_dir: Path = SAMPLE_GTFS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    def write(name: str, header: list[str], rows: list[list]) -> None:
        with open(out_dir / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write("agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone"],
          [["SIM", "Track My Ferry sample network (synthetic)", "https://example.org", "America/New_York"]])
    write("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
          [[sid, name, lat, lon] for sid, (name, lat, lon) in STOPS.items()])
    write("routes.txt", ["route_id", "route_short_name", "route_long_name", "route_type", "route_color", "route_text_color"],
          [[rid, short, long_name, 4, color, "000000"] for rid, (short, long_name, color, _, _) in ROUTES.items()])
    write("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                           "sunday", "start_date", "end_date"],
          [["WK", 1, 1, 1, 1, 1, 0, 0, "20260101", "20271231"], ["WE", 0, 0, 0, 0, 0, 1, 1, "20260101", "20271231"]])

    shapes, trips, stop_times = [], [], []
    for rid in ROUTES:
        for direction in (0, 1):
            coords, stops = path_coords(rid, direction)
            shape_id = f"{rid}_{direction}"
            shapes += [[shape_id, lat, lon, i] for i, (lat, lon) in enumerate(coords)]
            legs = leg_lengths(rid, direction)
            run_s = [round(d / (SCHEDULE_SPEED_KN * KNOT_MS) + ACCEL_PENALTY_S) for d in legs]
            for service_id, (first, last, headway) in SERVICE[rid].items():
                t = _minutes(first)
                while t <= _minutes(last):
                    trip_id = f"{rid}_{direction}_{service_id}_{t // 60:02d}{t % 60:02d}"
                    trips.append([rid, service_id, trip_id, ROUTES[rid][4][direction], direction, shape_id])
                    clock = t * 60
                    for i, sid in enumerate(stops):
                        if i == 0:
                            arr = dep = clock
                        else:
                            clock += run_s[i - 1]
                            arr = clock - clock % 60 + (60 if clock % 60 >= 30 else 0)  # round to minute
                            dep = arr if i == len(stops) - 1 else arr + DWELL_S
                            clock = dep
                        stop_times.append([trip_id, format_gtfs_time(arr), format_gtfs_time(dep), sid, i + 1])
                    t += headway

    write("shapes.txt", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], shapes)
    write("trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id", "shape_id"], trips)
    write("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], stop_times)
    return out_dir


if __name__ == "__main__":
    print(f"wrote sample GTFS to {generate()}")
