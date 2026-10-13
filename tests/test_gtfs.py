from datetime import date, datetime
from itertools import pairwise

import numpy as np

from trackmyferry.gtfs.realtime import encode_trip_updates, parse_trip_updates
from trackmyferry.gtfs.static import TZ, service_day_origin
from trackmyferry.messages import StopTimeUpdate, TripUpdate


def test_patterns_and_stop_distances(gtfs):
    assert len(gtfs.patterns) == 6
    for p in gtfs.patterns.values():
        assert p.stop_along[0] < 50
        assert all(b > a for a, b in pairwise(p.stop_along))
        assert abs(p.stop_along[-1] - p.shape.length) < 50


def test_calendar(gtfs):
    weekday = gtfs.instances_for_date(date(2026, 10, 14))
    weekend = gtfs.instances_for_date(date(2026, 10, 17))
    assert weekday and weekend
    assert all("_WK_" in i.trip_id for i in weekday)
    assert all("_WE_" in i.trip_id for i in weekend)


def test_dst_service_day_origin():
    # 2026-03-08: clocks spring forward; GTFS "noon minus 12h" is 23h before the next local noon
    origin = service_day_origin(date(2026, 3, 8))
    assert datetime.fromtimestamp(origin + 12 * 3600, TZ).hour == 12
    assert datetime.fromtimestamp(origin, TZ).hour == 23  # previous evening, because of the DST jump


def test_schedule_interpolation_is_invertible(gtfs):
    inst = gtfs.instances_for_date(date(2026, 10, 14))[10]
    p = gtfs.patterns[inst.pattern_id]
    for t in np.linspace(inst.start, inst.end, 25):
        a = inst.scheduled_along(t, p)
        t2 = inst.scheduled_time_at_along(a, p)
        assert inst.scheduled_along(t2, p) == np.float64(a) or abs(inst.scheduled_along(t2, p) - a) < 1.0


def test_gtfs_rt_round_trip():
    tu = TripUpdate(ts=1_790_000_000, trip_id="ER_0_WK_0810", route_id="ER", direction_id=0, start_date="20261014",
                    vehicle_label="SIM ER 01",
                    stop_updates=[StopTimeUpdate("DUMBO", 2, 1_790_000_300, 1_790_000_360, 60, 60),
                                  StopTimeUpdate("SWBG", 3, skipped=True)])
    feed_ts, parsed = parse_trip_updates(encode_trip_updates([tu], 1_790_000_005))
    assert feed_ts == 1_790_000_005
    (p,) = parsed
    assert (p.trip_id, p.route_id, p.direction_id, p.start_date, p.vehicle_label) == (
        "ER_0_WK_0810", "ER", 0, "20261014", "SIM ER 01")
    assert p.stop_updates[0].arrival_ts == 1_790_000_300 and p.stop_updates[0].arrival_delay_s == 60
    assert p.stop_updates[1].skipped
