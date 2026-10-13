import math

import numpy as np
import pytest

from trackmyferry.geo import Polyline, angle_diff_deg, bearing_deg, haversine_m


def test_haversine_known_distance():
    # Pier 11 to East 34th St landing is ~4.8 km as the crow flies
    d = haversine_m(40.7031, -74.0058, 40.7440, -73.9712)
    assert 5200 < d < 5600


def test_bearing_and_angle_diff():
    assert bearing_deg(40.70, -74.0, 40.80, -74.0) == pytest.approx(0.0, abs=1e-6)
    assert bearing_deg(40.70, -74.0, 40.70, -73.9) == pytest.approx(90.0, abs=0.1)
    assert angle_diff_deg(350, 10) == pytest.approx(20)
    assert angle_diff_deg(90, 270) == pytest.approx(180)


@pytest.fixture
def line():
    return Polyline([(40.70, -74.00), (40.71, -74.00), (40.71, -73.99)])


def test_projection_along_and_side(line):
    north_leg = haversine_m(40.70, -74.00, 40.71, -74.00)
    p = line.project(40.705, -74.001)  # west of a north-bound segment = left
    assert p.along_m == pytest.approx(north_leg / 2, rel=0.01)
    assert p.cross_m < 0 and abs(p.cross_m) == pytest.approx(84, abs=3)
    q = line.project(40.705, -73.999)
    assert q.cross_m > 0
    assert line.length == pytest.approx(north_leg + haversine_m(40.71, -74.00, 40.71, -73.99), rel=0.002)


def test_point_at_round_trip(line):
    for a in np.linspace(0, line.length, 7):
        lat, lon = line.point_at(a)
        assert line.project(lat, lon).along_m == pytest.approx(a, abs=0.5)


def test_project_many_matches_scalar(line):
    lats = np.array([40.701, 40.709, 40.7105, 40.712])
    lons = np.array([-74.0005, -73.9995, -73.995, -73.991])
    along, cross = line.project_many(lats, lons)
    for i in range(len(lats)):
        p = line.project(lats[i], lons[i])
        assert along[i] == pytest.approx(p.along_m, abs=1e-6)
        assert cross[i] == pytest.approx(abs(p.cross_m), abs=1e-6)


def test_min_along_and_window():
    # a U-shaped route: up one side of a narrow channel and back down the other (~84 m apart)
    u = Polyline([(40.70, -74.000), (40.71, -74.000), (40.71, -73.999), (40.70, -73.999)])
    lat, lon = 40.705, -73.9993  # nearer the return leg
    assert u.project(lat, lon).along_m > u.cum[2]
    # but if we know the boat is ~500 m into its trip, stay on the outbound leg
    assert u.project(lat, lon, near_along=500, window_m=300).along_m < u.cum[1]
    # nothing inside the window: fall back to the global nearest point
    assert u.project(lat, lon, near_along=5000, window_m=10).along_m > u.cum[2]
    end_lat, end_lon = u.point_at(u.length - 10)
    assert u.project(end_lat, end_lon, min_along=u.length / 2).along_m > u.length - 50


def test_alignment(line):
    north = line.alignment_with(0.0, 0, line.cum[1])
    east = line.alignment_with(0.0, line.cum[1], line.length)
    assert north == pytest.approx(1.0, abs=1e-3)
    assert east == pytest.approx(math.cos(math.radians(90)), abs=1e-3)
