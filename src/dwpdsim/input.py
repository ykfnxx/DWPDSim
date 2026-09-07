"""Ordered columnar input and a bounded, single-producer replay queue."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIELDS = ("timestamp_ns", "request_id", "affinity_id", "hash_ids")


@dataclass(frozen=True, slots=True)
class InputConfig:
    batch_requests: int = 1024
    batch_hashes: int = 262_144
    max_request_hashes: int = 1_048_576
    queue_batches: int = 3
    inflight_bytes: int = 64 * 1024 * 1024
    prefetch: bool = False

    def __post_init__(self):
        if (
            min(
                self.batch_requests,
                self.batch_hashes,
                self.max_request_hashes,
                self.queue_batches,
                self.inflight_bytes,
            )
            <= 0
        ):
            raise ValueError("input limits must be positive")
        if self.inflight_bytes < self.max_batch_bytes:
            raise ValueError("inflight_bytes must fit one maximum batch")

    @property
    def max_batch_bytes(self) -> int:
        return 8 * (4 * self.batch_requests + 1 + max(self.batch_hashes, self.max_request_hashes))


DEFAULT_INPUT_CONFIG = InputConfig()


@dataclass(frozen=True, slots=True)
class RequestBatch:
    sequence: int
    timestamps_ns: Any
    request_ids: Any
    affinity_ids: Any
    offsets: Any
    hash_ids: Any

    @property
    def buffers(self) -> tuple:
        return (
            self.timestamps_ns,
            self.request_ids,
            self.affinity_ids,
            self.offsets,
            self.hash_ids,
        )

    @property
    def nbytes(self) -> int:
        return sum(buffer.nbytes for buffer in self.buffers)


def _columnar_batches(tables: Iterable, config: InputConfig) -> Iterator[RequestBatch]:
    import numpy as np
    import pyarrow as pa

    sequence = 0
    previous_timestamp = None
    for table in tables:
        columns = []
        for name in FIELDS:
            column = table[name]
            if isinstance(column, pa.ChunkedArray):
                column = column.combine_chunks()
            if column.null_count:
                raise ValueError(f"{name} cannot contain nulls")
            if name == "hash_ids":
                if not (pa.types.is_list(column.type) or pa.types.is_large_list(column.type)):
                    raise ValueError("hash_ids must be a list of integers")
                if column.values.null_count or not pa.types.is_integer(column.type.value_type):
                    raise ValueError("hash_ids elements must be non-null integers")
                columns.append(column.cast(pa.large_list(pa.uint64()), safe=True))
            else:
                if not pa.types.is_integer(column.type):
                    raise ValueError(f"{name} must be an integer")
                columns.append(column.cast(pa.uint64(), safe=True))
        if not len(table):
            continue
        timestamps = columns[0].to_numpy()
        if (previous_timestamp is not None and timestamps[0] < previous_timestamp) or np.any(
            timestamps[1:] < timestamps[:-1]
        ):
            raise ValueError("timestamp_ns must be nondecreasing across shards and batches")
        previous_timestamp = int(timestamps[-1])
        offsets = columns[3].offsets.to_numpy()
        lengths = offsets[1:] - offsets[:-1]
        if np.any(lengths > config.max_request_hashes):
            raise ValueError("request exceeds max_request_hashes")
        start = 0
        while start < len(table):
            stop = min(len(table), start + config.batch_requests)
            hash_stop = (
                int(
                    np.searchsorted(
                        offsets, int(offsets[start]) + config.batch_hashes, side="right"
                    )
                )
                - 1
            )
            stop = max(start + 1, min(stop, hash_stop))
            lo, hi = int(offsets[start]), int(offsets[stop])
            # Own the buffers: a queued slice must not retain an entire decoder batch.
            buffers = [
                np.array(col.slice(start, stop - start).to_numpy(), dtype=np.uint64, copy=True)
                for col in columns[:3]
            ]
            buffers.append(np.array(offsets[start : stop + 1] - lo, dtype=np.uint64))
            buffers.append(
                np.array(
                    columns[3].values.slice(lo, hi - lo).to_numpy(), dtype=np.uint64, copy=True
                )
            )
            for buffer in buffers:
                buffer.flags.writeable = False
            yield RequestBatch(sequence, *buffers)
            sequence += 1
            start = stop


def parquet_batches(
    paths: Sequence[str | Path], config: InputConfig = DEFAULT_INPUT_CONFIG
) -> Iterator[RequestBatch]:
    """Read explicit shard order, preserving rows and complete request paths.

    The queue budget covers owned uint64 buffers, not Arrow's decoder workspace.
    Only one decoder batch (at most batch_requests rows) is active at a time.
    """
    import pyarrow.parquet as pq

    def tables():
        for path in paths:
            with pq.ParquetFile(path) as source:
                yield from source.iter_batches(
                    batch_size=config.batch_requests, columns=list(FIELDS), use_threads=False
                )

    yield from _columnar_batches(tables(), config)


def huggingface_batches(
    dataset: Any, config: InputConfig = DEFAULT_INPUT_CONFIG
) -> Iterator[RequestBatch]:
    """Adapt an ordered HF Dataset/IterableDataset with the four required fields."""
    import pyarrow as pa

    def tables():
        for batch in dataset.iter(batch_size=config.batch_requests):
            if isinstance(batch, (pa.Table, pa.RecordBatch)):
                yield batch
            else:
                yield pa.Table.from_pydict(
                    {field: batch[field] for field in FIELDS},
                    schema=pa.schema(
                        [dataset.features.arrow_schema.field(field) for field in FIELDS]
                    )
                    if getattr(dataset, "features", None) is not None
                    else None,
                )

    yield from _columnar_batches(tables(), config)


def hub_batches(
    repo: str,
    *,
    revision: str,
    split: str = "train",
    subset: str | None = None,
    config: InputConfig = DEFAULT_INPUT_CONFIG,
) -> Iterator[RequestBatch]:
    """Load an explicitly versioned Hub source without shuffling its order."""
    from datasets import load_dataset

    if not revision:
        raise ValueError("Hub input requires an explicit revision")
    dataset = load_dataset(repo, name=subset, split=split, revision=revision, streaming=True)
    yield from huggingface_batches(dataset, config)


def replay_batches(
    simulator: Any, batches: Iterable[RequestBatch], config: InputConfig = DEFAULT_INPUT_CONFIG
) -> dict[str, int | float]:
    """Consume batches in order and finish on EOF; never retry a partially executed batch.

    Reserve the maximum buffer size before advancing the producer. The reservation
    covers a batch being constructed as well as queued and consuming batches.
    Source decoder workspace is separate. Cancellation wakes queue waits; an active
    source I/O operation must return before the producer can terminate.
    """
    metrics = {
        "build_s": 0.0,
        "producer_wait_s": 0.0,
        "consumer_wait_s": 0.0,
        "replay_s": 0.0,
        "finish_s": 0.0,
        "peak_reserved_bytes": 0,
        "peak_inflight_batches": 0,
        "batches": 0,
    }
    expected = 0

    def consume(batch):
        nonlocal expected
        if batch.sequence != expected:
            raise ValueError("batch sequence must be contiguous starting at zero")
        if batch.nbytes > config.max_batch_bytes:
            raise ValueError("batch exceeds configured buffer limit")
        started = time.perf_counter()
        simulator.process_batch(*batch.buffers)
        metrics["replay_s"] += time.perf_counter() - started
        metrics["batches"] += 1
        expected += 1

    iterator = iter(batches)
    if not config.prefetch:
        while True:
            started = time.perf_counter()
            try:
                batch = next(iterator)
            except StopIteration:
                metrics["build_s"] += time.perf_counter() - started
                break
            metrics["build_s"] += time.perf_counter() - started
            metrics["peak_reserved_bytes"] = max(metrics["peak_reserved_bytes"], batch.nbytes)
            metrics["peak_inflight_batches"] = 1
            consume(batch)
            del batch
    else:
        condition = threading.Condition()
        queue = deque()
        reserved = count = 0
        closed = cancelled = False
        failure = None

        def producer_has_space():
            return cancelled or (
                count < config.queue_batches
                and reserved + config.max_batch_bytes <= config.inflight_bytes
            )

        def produce():
            nonlocal reserved, count, closed, failure
            try:
                while True:
                    with condition:
                        started = time.perf_counter()
                        condition.wait_for(producer_has_space)
                        metrics["producer_wait_s"] += time.perf_counter() - started
                        if cancelled:
                            return
                        reserved += config.max_batch_bytes
                        count += 1
                        metrics["peak_reserved_bytes"] = max(
                            metrics["peak_reserved_bytes"], reserved
                        )
                        metrics["peak_inflight_batches"] = max(
                            metrics["peak_inflight_batches"], count
                        )
                    started = time.perf_counter()
                    try:
                        batch = next(iterator)
                    except StopIteration:
                        metrics["build_s"] += time.perf_counter() - started
                        break
                    metrics["build_s"] += time.perf_counter() - started
                    if batch.nbytes > config.max_batch_bytes:
                        raise ValueError("batch exceeds configured buffer limit")
                    with condition:
                        reserved -= config.max_batch_bytes - batch.nbytes
                        queue.append(batch)
                        del batch
                        condition.notify_all()
            except BaseException as error:  # noqa: BLE001 - propagate producer failures to consumer
                failure = error
            finally:
                with condition:
                    closed = True
                    condition.notify_all()

        producer = threading.Thread(target=produce, name="dwpdsim-input")
        producer.start()
        try:
            while True:
                with condition:
                    started = time.perf_counter()
                    condition.wait_for(lambda: queue or closed)
                    metrics["consumer_wait_s"] += time.perf_counter() - started
                    if not queue:
                        if failure is not None:
                            raise failure
                        break
                    batch = queue.popleft()
                consume(batch)
                with condition:
                    reserved -= batch.nbytes
                    count -= 1
                    del batch
                    condition.notify_all()
        finally:
            with condition:
                cancelled = True
                condition.notify_all()
            producer.join()
    started = time.perf_counter()
    simulator.finish()
    metrics["finish_s"] = time.perf_counter() - started
    return metrics
