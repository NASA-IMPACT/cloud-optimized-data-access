"""Request counting for each access path, plus the session probes.

- obspec paths (tuned netCDF arm, ManifestStore arm): obspec-utils'
  ``TracingReadableStore`` records every get/get_range/get_ranges/head.
- fsspec path (user-stack arm): botocore event hooks on the s3fs client.
- library retries: a logging handler on the ``obstore`` and ``icechunk``
  loggers; null with a reason when a library does not log them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from obspec_utils.wrappers import RequestTrace, TracingReadableStore


def traced_store(store: Any) -> tuple[TracingReadableStore, RequestTrace]:
    trace = RequestTrace()
    return TracingReadableStore(store, trace), trace


# What a count means. The obspec tracer sees Python calls into obstore, one
# entry per byte range; obstore's Rust side may coalesce, split or retry, so
# the HTTP request count can differ (developmentseed/obstore#105). The
# botocore hook sees completed S3 operations, which are HTTP requests.
CLIENT_RANGES = "client-ranges"
HTTP_CALLS = "http-calls"


def trace_counts(trace: RequestTrace) -> dict[str, Any]:
    df = trace.to_dataframe()
    if df.empty:
        return {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}, "kind": CLIENT_RANGES}
    by_method = df["method"].value_counts().to_dict()
    return {
        "request_count": len(df),
        "bytes_requested": int(df["length"].sum()),
        "requests_by_method": {str(k): int(v) for k, v in by_method.items()},
        "kind": CLIENT_RANGES,
    }


class S3fsCounter:
    """Counts botocore calls on an s3fs filesystem's client.

    Bytes are counted for GetObject only: a HeadObject reply carries the
    whole object's size, not bytes moved. Retries come from each reply's
    ``ResponseMetadata.RetryAttempts``; botocore's ``needs-retry`` event
    fires on successes too, so it is not used.
    """

    def __init__(self) -> None:
        self.request_count = 0
        self.bytes_requested = 0
        self.by_method: dict[str, int] = {}
        self.retries = 0

    def attach(self, fs: Any) -> None:
        fs.s3.meta.events.register("after-call", self._after_call)

    def _after_call(self, parsed: Any = None, model: Any = None, **_: Any) -> None:
        self.request_count += 1
        name = getattr(model, "name", "unknown")
        self.by_method[name] = self.by_method.get(name, 0) + 1
        if isinstance(parsed, dict):
            if name == "GetObject" and isinstance(parsed.get("ContentLength"), int):
                self.bytes_requested += parsed["ContentLength"]
            attempts = parsed.get("ResponseMetadata", {}).get("RetryAttempts")
            if isinstance(attempts, int):
                self.retries += attempts

    def counts(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "bytes_requested": self.bytes_requested,
            "requests_by_method": dict(self.by_method),
            "retries": self.retries,
            "kind": HTTP_CALLS,
        }


class RetryLogCounter(logging.Handler):
    """Counts DEBUG log records mentioning a retry from obstore and icechunk."""

    LOGGERS = ("obstore", "icechunk")

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.count = 0
        self.seen_any = False
        self._previous_levels: dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        self.seen_any = True
        if "retry" in record.getMessage().lower():
            self.count += 1

    def attach(self) -> None:
        for name in self.LOGGERS:
            logger = logging.getLogger(name)
            self._previous_levels[name] = logger.level
            logger.setLevel(logging.DEBUG)
            logger.addHandler(self)

    def detach(self) -> None:
        for name in self.LOGGERS:
            logger = logging.getLogger(name)
            logger.removeHandler(self)
            logger.setLevel(self._previous_levels.get(name, logging.NOTSET))


def probe_paging(h5file: Any) -> bool:
    """True when the HDF5 file uses the paged file-space strategy."""
    import h5py

    strategy = h5file.id.get_create_plist().get_file_space_strategy()[0]
    return strategy == h5py.h5f.FSPACE_STRATEGY_PAGE


def first_object(store: Any, prefix: str | None = None) -> str:
    """The first object a listing under ``prefix`` returns; raises if there is none."""
    import obstore

    for page in obstore.list(store, prefix=prefix, chunk_size=1):
        for meta in page:
            return str(meta["path"])
    raise FileNotFoundError(f"no objects under {prefix!r}")


def probe_network(
    store: Any, path: str | None, *, heads: int = 20, range_reads: int = 5, range_bytes: int = 16 * 2**20, prefix: str | None = None
) -> dict[str, float | None]:
    """Median RTT (ms) from HEADs and bandwidth (MB/s) from large range reads.

    ``store`` is an obstore store; ``path`` an object of at least
    ``range_bytes`` bytes, or ``None`` to probe whatever object a listing
    under ``prefix`` returns first (RTT only makes sense then). Network.
    """
    import obstore

    if path is None:
        path = first_object(store, prefix)
    rtts = []
    for _ in range(heads):
        t0 = time.perf_counter()
        obstore.head(store, path)
        rtts.append(time.perf_counter() - t0)
    rtts.sort()
    rtt_ms = 1000 * rtts[len(rtts) // 2]
    total_bytes, total_s = 0, 0.0
    if range_reads == 0:
        return {"rtt_ms_median": rtt_ms, "bandwidth_mb_s": None}
    for i in range(range_reads):
        t0 = time.perf_counter()
        buf = obstore.get_range(store, path, start=i * range_bytes, end=(i + 1) * range_bytes)
        total_s += time.perf_counter() - t0
        total_bytes += len(memoryview(buf))
    return {"rtt_ms_median": rtt_ms, "bandwidth_mb_s": total_bytes / total_s / 1e6}


async def fetch_object(store: Any, path: str, *, part_bytes: int = 16 * 2**20, concurrency: int = 16) -> bytearray:
    """The whole object in memory: one HEAD, then ``part_bytes`` ranges with
    ``concurrency`` in flight. Uses the store's own methods, so a tracing
    wrapper counts every request. Network."""
    import asyncio

    size = (await store.head_async(path))["size"]
    data = bytearray(size)
    gate = asyncio.Semaphore(concurrency)

    async def part(start: int) -> None:
        async with gate:
            buf = await store.get_range_async(path, start=start, end=min(start + part_bytes, size))
            data[start : start + len(memoryview(buf))] = memoryview(buf)

    await asyncio.gather(*(part(s) for s in range(0, size, part_bytes)))
    return data


def probe_download(store: Any, path: str, *, part_bytes: int = 16 * 2**20, concurrency: int = 16) -> dict[str, float | int]:
    """Seconds to bring the whole object into memory: the "download it all, then
    parse" bound. Times the transfer and one copy, not the parse. Network."""
    import asyncio

    t0 = time.perf_counter()
    size = len(asyncio.run(fetch_object(store, path, part_bytes=part_bytes, concurrency=concurrency)))
    seconds = time.perf_counter() - t0
    return {"size_bytes": size, "part_bytes": part_bytes, "concurrency": concurrency, "seconds": seconds, "mb_s": size / seconds / 1e6}
