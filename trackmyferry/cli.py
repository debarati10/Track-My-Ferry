"""Command-line entry point: `trackmyferry <command>` (or `python -m trackmyferry <command>`).

  run          all-in-one: data sources + processing + API + live map (sim or live)
  record       save raw feeds to disk (start this on day one: live feeds keep no history)
  simulate     generate a multi-week simulated dataset with ground truth
  train        build features from recorded data, train models, write the evaluation report
  ridership    download NYC Ferry hourly ridership from NYC Open Data
  fleet        list vessels discovered as NYC Ferry boats in recorded data -> fleet.json
  ingest       (distributed) run one source and publish to Kafka
  process      (distributed) consume Kafka, run the pipeline, write PostgreSQL/Redis, publish results
  api          (distributed) serve the API/map from Kafka's derived topics
  gtfs-sample  regenerate the bundled sample network
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from .config import Settings

log = logging.getLogger("trackmyferry")


def _serving_threads() -> None:
    """Long-running services score small batches; OpenMP thread pools in scikit-learn only add
    contention and latency spikes there, so default to one thread (training keeps all cores)."""
    os.environ.setdefault("OMP_NUM_THREADS", "1")


def _settings(args) -> Settings:
    s = Settings.from_env()
    over = {}
    for name in ("mode", "bus"):
        if getattr(args, name, None):
            over[name] = getattr(args, name)
    if getattr(args, "speedup", None):
        over["sim_speedup"] = args.speedup
    if getattr(args, "port", None):
        over["api_port"] = args.port
    if getattr(args, "models", None):
        over["model_dir"] = Path(args.models)
    return replace(s, **over)


def _run(coro) -> None:
    """asyncio.run, but Ctrl+C stops the service quietly instead of printing a traceback."""
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        log.info("stopped")


async def _serve(rt, settings: Settings, extra_tasks=()) -> None:
    import uvicorn

    from .api.app import create_app

    app = create_app(rt)
    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port, log_level="info")
    server = uvicorn.Server(config)
    tasks = [asyncio.create_task(t) for t in extra_tasks]
    try:
        await server.serve()
    finally:
        for t in tasks:
            t.cancel()
        await rt.stop()


def cmd_run(args) -> None:
    _serving_threads()
    from .runtime import Runtime

    settings = _settings(args)

    async def main():
        rt = Runtime(settings, in_process=True)
        await rt.start_all_in_one()
        log.info("Track My Ferry %s mode on http://localhost:%d (model: %s)", settings.mode, settings.api_port, rt.model_version)
        await _serve(rt, settings)

    _run(main())


def cmd_record(args) -> None:
    from .record import Recorder
    from .runtime import Runtime

    settings = _settings(args)
    out = Path(args.out)

    async def main():
        rt = Runtime(settings, in_process=True)
        rt.pipeline = None
        rec = Recorder(out / "raw")
        await rt.bus.start()
        stop_at = time.time() + args.hours * 3600 if args.hours else None

        async def sink():
            from .messages import Topics, decode

            async for topic, payload in rt.bus.subscribe(Topics.INGEST, group="recorder"):
                payload.pop("_ingest_ts", None)
                rec.write(decode(topic, payload))
                if rec.count % 5000 == 0:
                    log.info("recorded %d messages", rec.count)
                if stop_at and time.time() > stop_at:
                    return

        sink_task = asyncio.create_task(sink())
        await asyncio.sleep(0)
        sources = [asyncio.create_task(c) for c in rt.source_coroutines()]
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, sink_task.cancel)
        try:
            await sink_task
        except asyncio.CancelledError:
            pass
        finally:
            for t in sources:
                t.cancel()
            rec.close()
            log.info("recorder stopped after %d messages", rec.count)

    _run(main())


def cmd_simulate(args) -> None:
    from .runtime import load_gtfs
    from .sim.dataset import generate

    settings = replace(Settings.from_env(), mode="sim")
    meta = generate(load_gtfs(settings), args.out, date.fromisoformat(args.start), args.days, seed=args.seed)
    print(json.dumps(meta, indent=2))


def cmd_train(args) -> None:
    from .ml.train import train_all
    from .runtime import load_gtfs

    settings = Settings.from_env()
    if args.gtfs:
        settings = replace(settings, gtfs_static_path=args.gtfs)
    elif (Path(args.data) / "truth").exists():
        settings = replace(settings, mode="sim")
    else:
        settings = replace(settings, mode="live")
    summary = train_all(args.data, load_gtfs(settings), args.models, test_days=args.test_days,
                        report_dir=args.report, capacity=settings.vessel_capacity)
    eta = summary["eta"]["overall"]
    print(f"\nETA MAE: model {eta['model']['mae_s']:.0f}s | official {eta['official']['mae_s']:.0f}s | "
          f"timetable {eta['schedule']['mae_s']:.0f}s  ->  report: {Path(args.report) / 'RESULTS.md'}")


def cmd_ridership(args) -> None:
    from .ingest.ridership import download

    n = download(args.out, since=args.since)
    print(f"wrote {n:,} rows to {args.out}")


def cmd_fleet(args) -> None:
    from .messages import Topics
    from .processing.pipeline import Pipeline
    from .record import replay
    from .runtime import load_gtfs

    settings = replace(Settings.from_env(), mode="live")
    gtfs = load_gtfs(settings)
    pipe = Pipeline(gtfs, prediction_interval_s=1e9, emit_states=False)
    for msg in replay(Path(args.data) / "raw", [Topics.AIS_POSITIONS, Topics.AIS_STATIC]):
        pipe.handle(msg)
    vessels = [{"mmsi": m, "name": pipe.names.get(m)} for m in sorted(pipe.registry.discovered)]
    Path(args.out).write_text(json.dumps({"vessels": vessels}, indent=2))
    print(f"discovered {len(vessels)} NYC Ferry vessels -> {args.out}")


def cmd_ingest(args) -> None:
    _serving_threads()
    from .runtime import run_ingest_service

    settings = replace(_settings(args), bus=args.bus or "kafka")
    _run(run_ingest_service(settings, args.source))


def cmd_process(args) -> None:
    _serving_threads()
    from .runtime import run_processor_service

    settings = replace(_settings(args), bus=args.bus or "kafka")
    _run(run_processor_service(settings))


def cmd_api(args) -> None:
    _serving_threads()
    from .runtime import Runtime

    settings = replace(_settings(args), bus=args.bus or "kafka")

    async def main():
        rt = Runtime(settings, in_process=False)
        await rt.bus.start()
        await rt.start_storage()
        await _serve(rt, settings, extra_tasks=[rt.hub.run(), rt.follow_derived()])

    _run(main())


def cmd_gtfs_sample(args) -> None:
    from .sim.sample_network import generate

    print(f"wrote {generate()}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="trackmyferry", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="all-in-one: sources + processing + API + map")
    r.add_argument("--mode", choices=["sim", "live"])
    r.add_argument("--speedup", type=float, help="simulator speed multiplier (sim mode)")
    r.add_argument("--port", type=int)
    r.add_argument("--models")
    r.set_defaults(func=cmd_run)

    r = sub.add_parser("record", help="save raw feeds to disk")
    r.add_argument("--out", default="data/live")
    r.add_argument("--mode", choices=["sim", "live"], default="live")
    r.add_argument("--hours", type=float, help="stop after this many hours (default: run until stopped)")
    r.add_argument("--speedup", type=float)
    r.set_defaults(func=cmd_record)

    r = sub.add_parser("simulate", help="generate a simulated dataset with ground truth")
    r.add_argument("--out", default="data/sim")
    r.add_argument("--start", default="2026-09-07")
    r.add_argument("--days", type=int, default=21)
    r.add_argument("--seed", type=int, default=7)
    r.set_defaults(func=cmd_simulate)

    r = sub.add_parser("train", help="train models and write the evaluation report")
    r.add_argument("--data", default="data/sim")
    r.add_argument("--models", default="models")
    r.add_argument("--report", default="docs")
    r.add_argument("--test-days", type=int, default=5)
    r.add_argument("--gtfs", help="GTFS zip/folder (defaults: sample network for sim data, NYC Ferry for live)")
    r.set_defaults(func=cmd_train)

    r = sub.add_parser("ridership", help="download NYC Ferry hourly ridership (NYC Open Data)")
    r.add_argument("--out", default="data/live/ridership.csv")
    r.add_argument("--since", default="2024-01-01")
    r.set_defaults(func=cmd_ridership)

    r = sub.add_parser("fleet", help="discover NYC Ferry vessels in recorded AIS")
    r.add_argument("--data", default="data/live")
    r.add_argument("--out", default="data/live/fleet.json")
    r.set_defaults(func=cmd_fleet)

    for name, func, helptext in (("process", cmd_process, "distributed processor"), ("api", cmd_api, "distributed API")):
        r = sub.add_parser(name, help=helptext)
        r.add_argument("--mode", choices=["sim", "live"])
        r.add_argument("--bus", choices=["kafka", "memory"])
        r.add_argument("--port", type=int)
        r.add_argument("--models")
        r.set_defaults(func=func)

    r = sub.add_parser("ingest", help="distributed: run one data source")
    r.add_argument("source", choices=["ais", "trip_updates", "alerts", "env", "sim"])
    r.add_argument("--mode", choices=["sim", "live"])
    r.add_argument("--bus", choices=["kafka", "memory"])
    r.add_argument("--speedup", type=float)
    r.set_defaults(func=cmd_ingest)

    r = sub.add_parser("gtfs-sample", help="regenerate the bundled sample GTFS")
    r.set_defaults(func=cmd_gtfs_sample)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
