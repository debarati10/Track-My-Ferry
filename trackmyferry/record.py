"""Recorder and replay.

Live feeds keep no history, so the first thing to run is the recorder: it appends every message to
`<root>/<topic>/<YYYY-MM-DD>.jsonl.gz` (one file per topic per local day). Training and evaluation
replay those files through the exact same processing code that runs live.
"""
from __future__ import annotations

import gzip
import heapq
import json
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import IO

from .gtfs.static import TZ
from .messages import Message, decode, message_ts


class Recorder:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._files: dict[tuple[str, str], IO] = {}
        self.count = 0

    def write(self, msg: Message) -> None:
        day = datetime.fromtimestamp(message_ts(msg), TZ).strftime("%Y-%m-%d")
        key = (msg.topic, day)
        fh = self._files.get(key)
        if fh is None:
            path = self.root / msg.topic / f"{day}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = gzip.open(path, "at", compresslevel=5)
            self._files[key] = fh
            if len(self._files) > 32:  # keep the number of open handles bounded
                old_key = next(iter(self._files))
                if old_key != key:
                    self._files.pop(old_key).close()
        fh.write(json.dumps(msg.to_dict(), separators=(",", ":")) + "\n")
        self.count += 1

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()
        self._files.clear()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def recorded_days(root: Path | str, topic: str) -> list[date]:
    folder = Path(root) / topic
    return sorted(datetime.strptime(p.name[:10], "%Y-%m-%d").date() for p in folder.glob("*.jsonl.gz"))


def _iter_file(path: Path, topic: str) -> Iterator[tuple[float, str, Message]]:
    with gzip.open(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            msg = decode(topic, json.loads(line))
            yield message_ts(msg), topic, msg


def replay(root: Path | str, topics: Iterable[str], days: Iterable[date] | None = None) -> Iterator[Message]:
    """Merge recorded topics into one time-ordered stream."""
    root = Path(root)
    topics = list(topics)
    if days is None:
        days = sorted({d for t in topics for d in recorded_days(root, t)})
    for d in days:
        streams = []
        for t in topics:
            path = root / t / f"{d:%Y-%m-%d}.jsonl.gz"
            if path.exists():
                streams.append(_iter_file(path, t))
        for _, _, msg in heapq.merge(*streams, key=lambda x: x[0]):
            yield msg
