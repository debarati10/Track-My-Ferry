from datetime import date

import pytest

from trackmyferry.gtfs import GTFS
from trackmyferry.sim.sample_network import SAMPLE_GTFS_DIR
from trackmyferry.sim.simulator import FleetSim, SimConfig

TEST_DAY = date(2026, 10, 14)  # a Wednesday


@pytest.fixture(scope="session")
def gtfs() -> GTFS:
    return GTFS.load(SAMPLE_GTFS_DIR)


@pytest.fixture(scope="session")
def sim_run(gtfs):
    """Two hours of simulated morning service, plus the simulator for ground truth."""
    sim = FleetSim(gtfs, SimConfig(seed=21))
    sim.plan_day(TEST_DAY)
    start = sim.day_start + 2.5 * 3600
    sim.warm_start(start)
    msgs = sim.advance_to(start + 2 * 3600)
    return sim, msgs
