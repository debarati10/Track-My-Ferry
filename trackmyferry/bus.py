"""Message bus abstraction.

* `MemoryBus` — asyncio fan-out queues for the single-process mode and for tests.
* `KafkaBus`  — Apache Kafka (or Redpanda) via aiokafka for the distributed deployment.

Both carry JSON-encoded `Message`s. Each published message is stamped with `_ingest_ts` the first
time it enters the bus, so downstream services can measure end-to-end latency.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from .messages import Message

log = logging.getLogger(__name__)


class Bus(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None: ...
    def subscribe(self, topics: list[str] | tuple[str, ...], group: str) -> AsyncIterator[tuple[str, dict[str, Any]]]: ...


async def publish_message(bus: Bus, msg: Message, ingest_ts: float | None = None) -> None:
    payload = msg.to_dict()
    payload["_ingest_ts"] = ingest_ts if ingest_ts is not None else time.time()
    await bus.publish(msg.topic, payload, msg.key())


class MemoryBus:
    """In-process pub/sub. Slow subscribers drop their oldest messages instead of blocking producers."""

    def __init__(self, max_queue: int = 50_000):
        self.max_queue = max_queue
        self._subs: dict[str, list[asyncio.Queue]] = {}
        self.dropped = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        for queues in self._subs.values():
            for q in queues:
                q.put_nowait(None)

    async def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        for q in self._subs.get(topic, ()):
            if q.full():
                q.get_nowait()
                self.dropped += 1
            q.put_nowait((topic, dict(value)))  # each subscriber gets its own copy

    async def subscribe(self, topics, group: str = "default") -> AsyncIterator[tuple[str, dict[str, Any]]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.max_queue)
        for t in topics:
            self._subs.setdefault(t, []).append(q)
        try:
            while True:
                item = await q.get()
                if item is None:
                    return
                yield item
        finally:
            for t in topics:
                if q in self._subs.get(t, []):
                    self._subs[t].remove(q)


class KafkaBus:
    """Kafka transport. Keys are vessel MMSIs or trip ids, so all messages about one boat stay in
    order on one partition and the processor can scale out by partition."""

    def __init__(self, bootstrap: str, client_id: str = "trackmyferry"):
        self.bootstrap = bootstrap
        self.client_id = client_id
        self._producer = None
        self._consumers: list[Any] = []

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer  # pip install aiokafka

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            client_id=self.client_id,
            value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode(),
            key_serializer=lambda k: k.encode() if k else None,
            linger_ms=20,
            acks=1,
        )
        await self._producer.start()

    async def stop(self) -> None:
        for c in self._consumers:
            await c.stop()
        if self._producer is not None:
            await self._producer.stop()

    async def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaBus.start() has not been called")
        await self._producer.send(topic, value, key=key)

    async def subscribe(self, topics, group: str = "default") -> AsyncIterator[tuple[str, dict[str, Any]]]:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self.bootstrap,
            group_id=group,
            client_id=f"{self.client_id}-{group}",
            value_deserializer=lambda b: json.loads(b),
            auto_offset_reset="latest",
            enable_auto_commit=True,
        )
        await consumer.start()
        self._consumers.append(consumer)
        try:
            async for rec in consumer:
                yield rec.topic, rec.value
        finally:
            await consumer.stop()


def make_bus(kind: str, kafka_bootstrap: str = "localhost:9092", client_id: str = "trackmyferry") -> Bus:
    if kind == "kafka":
        return KafkaBus(kafka_bootstrap, client_id)
    if kind == "memory":
        return MemoryBus()
    raise ValueError(f"unknown bus kind: {kind}")
