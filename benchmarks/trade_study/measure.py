"""What a trial measures besides wall-clock time: CPU, memory, bytes on the
wire, the fewest requests a selection can take, and the floor.

Nothing here opens a network connection.
"""

from __future__ import annotations

import os
import resource
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import numpy as np
import psutil

# Objects an Icechunk store reads before any chunk: repository config,
# snapshot, manifest, zarr.json.
METADATA_OBJECTS = 4


# --- requests a selection cannot avoid --------------------------------------------


def _chunks_touched(selection: slice | np.ndarray | Sequence[int], chunk: int, length: int) -> set[int]:
    if isinstance(selection, slice):
        start, stop, _ = selection.indices(length)
        return set(range(start // chunk, (stop - 1) // chunk + 1)) if stop > start else set()
    return {int(i) // chunk for i in np.asarray(selection).ravel()}


def static_minimum_requests(
    shape: tuple[int, int, int],
    chunks: tuple[int, int, int],
    time_indices: Sequence[int],
    y: slice | np.ndarray,
    x: slice | np.ndarray,
    *,
    itemsize: int,
) -> tuple[int, int]:
    """(chunks, uncompressed bytes) a selection must touch, from the chunk grid alone.

    Point selections (index arrays) are paired element-wise, so only the
    chunks that actually contain a point are counted.
    """
    if isinstance(y, slice) or isinstance(x, slice):
        per_step = len(_chunks_touched(y, chunks[1], shape[1])) * len(_chunks_touched(x, chunks[2], shape[2]))
    else:
        pairs = zip(np.asarray(y).ravel(), np.asarray(x).ravel(), strict=True)
        per_step = len({(int(a) // chunks[1], int(b) // chunks[2]) for a, b in pairs})
    count = per_step * len({int(t) for t in time_indices})
    return count, count * chunks[0] * chunks[1] * chunks[2] * itemsize


# --- the floor --------------------------------------------------------------------


def floor_seconds(serial_round_trips: int, rtt_s: float, bytes_total: int, bandwidth_bps: float) -> float:
    """The least time the network allows: round trips that must happen one
    after another, plus the bytes at the link's measured rate."""
    return serial_round_trips * rtt_s + bytes_total / bandwidth_bps


# --- CPU, memory and bytes on the wire --------------------------------------------


@dataclass
class ResourceDelta:
    cpu_user_s: float = 0.0
    cpu_sys_s: float = 0.0
    peak_rss_bytes: int = 0


@contextmanager
def resource_usage() -> Iterator[ResourceDelta]:
    """CPU seconds and peak memory over the block, for this process plus any
    child processes that have exited and been waited on."""
    delta = ResourceDelta()
    before_self = resource.getrusage(resource.RUSAGE_SELF)
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        yield delta
    finally:
        after_self = resource.getrusage(resource.RUSAGE_SELF)
        after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        delta.cpu_user_s = (after_self.ru_utime - before_self.ru_utime) + (
            after_children.ru_utime - before_children.ru_utime
        )
        delta.cpu_sys_s = (after_self.ru_stime - before_self.ru_stime) + (
            after_children.ru_stime - before_children.ru_stime
        )
        # Linux reports ru_maxrss in KiB, macOS in bytes. The kernel reports the
        # children's peak separately; take the larger.
        scale = 1024 if sys.platform.startswith("linux") else 1
        delta.peak_rss_bytes = max(after_self.ru_maxrss, after_children.ru_maxrss) * scale


def live_children_cpu() -> tuple[float, float]:
    """(user, system) CPU seconds of child processes still running, which
    :func:`resource_usage` cannot see until they exit."""
    user = system = 0.0
    for child in psutil.Process().children(recursive=True):
        try:
            times = child.cpu_times()
        except psutil.Error:
            continue
        user, system = user + times.user, system + times.system
    return user, system


def net_rx_bytes() -> int:
    """The host's receive counter. On a shared host it includes other traffic."""
    return psutil.net_io_counters().bytes_recv


def cpu_steal_seconds() -> float | None:
    """Steal time from /proc/stat's aggregate cpu line, in seconds; None off Linux."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if len(parts) < 9 or parts[0] != "cpu":
        return None
    return int(parts[8]) / os.sysconf("SC_CLK_TCK")


def json_default(value: Any) -> Any:
    """``json.dumps(default=...)`` for the numpy values that end up in rows."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serializable: {type(value)}")
