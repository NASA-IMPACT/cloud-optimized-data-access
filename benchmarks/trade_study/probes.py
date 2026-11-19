"""Network probes: what the link to the data can do, measured before a session.

The floor every row carries (:func:`benchmarks.trade_study.measure.floor_seconds`)
is built from these two numbers: a median round-trip time and a bandwidth.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


def probe_network(
    store: Any, path: str, *, heads: int = 20, range_reads: int = 5, range_bytes: int = 16 * 2**20
) -> dict[str, float | None]:
    """Median round-trip time (ms) from HEADs, and single-stream bandwidth (MB/s)
    from ``range_reads`` consecutive reads of ``range_bytes``. ``path`` must be an
    object at least ``range_reads × range_bytes`` long. Network."""
    import obstore

    round_trips = []
    for _ in range(heads):
        start = time.perf_counter()
        obstore.head(store, path)
        round_trips.append(time.perf_counter() - start)
    round_trips.sort()
    rtt_ms = 1000 * round_trips[len(round_trips) // 2]
    if range_reads == 0:
        return {"rtt_ms_median": rtt_ms, "bandwidth_mb_s": None}
    total_bytes, total_seconds = 0, 0.0
    for i in range(range_reads):
        start = time.perf_counter()
        buffer = obstore.get_range(store, path, start=i * range_bytes, end=(i + 1) * range_bytes)
        total_seconds += time.perf_counter() - start
        total_bytes += len(memoryview(buffer))
    return {"rtt_ms_median": rtt_ms, "bandwidth_mb_s": total_bytes / total_seconds / 1e6}


def concurrent_rate(store: Any, path: str, part_bytes: int = 4 * 2**20, parts: int = 16) -> float:
    """MB/s over the first ``parts`` × ``part_bytes`` of an object (at most 64 MiB
    by default, however large the granule), all ranges in flight at once. Network."""

    async def fetch() -> int:
        size = (await store.head_async(path))["size"]
        starts = range(0, min(size, parts * part_bytes), part_bytes)
        buffers = await asyncio.gather(
            *(store.get_range_async(path, start=s, end=min(s + part_bytes, size)) for s in starts)
        )
        return sum(len(memoryview(b)) for b in buffers)

    start = time.perf_counter()
    return asyncio.run(fetch()) / (time.perf_counter() - start) / 1e6
