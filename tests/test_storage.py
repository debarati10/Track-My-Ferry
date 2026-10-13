"""Integration test against a real PostgreSQL. Runs only when TRACKMYFERRY_TEST_DATABASE_URL is set, e.g.
TRACKMYFERRY_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/trackmyferry_test pytest tests/test_storage.py"""
import asyncio
import os

import pytest

from trackmyferry.messages import Prediction, StopEvent, VesselState
from trackmyferry.processing.pipeline import Pipeline

DSN = os.environ.get("TRACKMYFERRY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TRACKMYFERRY_TEST_DATABASE_URL to run")


def test_write_and_query(gtfs, sim_run):
    from trackmyferry.storage.db import Storage

    _, msgs = sim_run

    async def main():
        st = Storage(DSN)
        await st.start()
        for t in ("vessel_positions", "vessel_states", "stop_events", "trip_updates", "predictions", "env_observations"):
            await st.conn.execute(f"TRUNCATE {t}")
        pipe = Pipeline(gtfs)
        for m in msgs[:20000]:
            st.add(m)
            for o in pipe.handle(m):
                if isinstance(o, (VesselState, StopEvent, Prediction)):
                    st.add(o)
        await st.flush()
        counts = {t: (await st.fetch(f"SELECT count(*) AS n FROM {t}"))[0]["n"]
                  for t in ("vessel_positions", "stop_events", "predictions")}
        errors = await st.fetch("SELECT count(*) AS n, avg(abs(error_s)) AS mae FROM prediction_errors")
        await st.stop()
        return counts, errors[0]

    counts, errors = asyncio.run(main())
    assert all(v > 0 for v in counts.values())
    assert errors["n"] > 0 and errors["mae"] is not None
