"""Measurement primitives: the row schema, static request minimum, resource
usage, floor and cost models, and host description.

Nothing here opens a network connection except ``host_info`` (IMDS, with a
short timeout) and the probes in :mod:`counters`.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import numpy as np

# Pinned store layout (tempo-virtual-zarr-pipeline docs/reference/stores.md).
GRID_SHAPE = (1, 2950, 7750)
VARIABLES: dict[str, tuple[tuple[int, int, int], int]] = {
    # name: (chunks, itemsize)
    "vertical_column_troposphere": ((1, 738, 1938), 8),
    "main_data_quality_flag": ((1, 984, 2584), 2),
}
# Objects read before any chunk: repo config, snapshot, manifest, zarr.json.
# Recorded in the protocol doc; checked against virtual_manifeststore once per session.
METADATA_OBJECTS = 4

S3_GET_USD_PER_1000 = 0.0004  # us-west-2 standard, list price 2026-11-02


@dataclass
class TrialRow:
    # identity
    run_id: str | None = None
    session_id: str | None = None
    arm: str | None = None
    arm_settings: dict[str, Any] = field(default_factory=dict)  # the backend's full settings
    arm_variant: dict[str, Any] = field(default_factory=dict)  # the orchestrator's override, e.g. {"max_workers": 1}
    workload: str | None = None
    case_size: int | None = None
    window: str | None = None
    observation_count: int | None = None
    repetition: int | None = None
    cache_state: str | None = None
    cache_drop: str | None = None
    access: str | None = None  # s3 (in-region) or https (distribution endpoint, from anywhere)
    # provenance
    git_sha: str | None = None
    uv_lock_sha256: str | None = None
    manifest_path: str | None = None
    snapshot_id: str | None = None
    snapshot_tag: str | None = None
    packages: dict[str, str] = field(default_factory=dict)
    host_kind: str | None = None
    instance_type: str | None = None
    instance_type_reason: str | None = None
    availability_zone: str | None = None
    cpu_model: str | None = None
    nproc: int | None = None
    mem_total_bytes: int | None = None
    # phases
    catalog_seconds: float | None = None
    open_seconds: float | None = None
    discovery_plus_open_seconds: float | None = None
    retrieval_seconds: float | None = None
    retrieval_split: dict[str, float] = field(default_factory=dict)  # sub-phases of retrieval, for arms that can time them
    processing_seconds: float | None = None
    rendering_seconds: float | None = None
    end_to_end_seconds: float | None = None
    teardown_seconds: float | None = None  # backend shutdown (worker pools); outside end_to_end
    # resources
    cpu_user_s: float | None = None
    cpu_sys_s: float | None = None
    peak_rss_bytes: int | None = None
    load_avg_1m: float | None = None
    cpu_steal_s: float | None = None
    cpu_steal_s_reason: str | None = None
    # transport
    request_count: int | None = None
    request_count_reason: str | None = None
    requests_by_method: dict[str, int] = field(default_factory=dict)
    bytes_requested: int | None = None
    net_rx_bytes: int | None = None
    net_rx_bytes_reason: str | None = None
    retries: int | None = None
    retries_reason: str | None = None
    # floor and cost
    rtt_ms_median: float | None = None
    bandwidth_mb_s: float | None = None
    floor_seconds: float | None = None
    cost_usd_estimate: float | None = None
    # active benchmarking (filled by the orchestrator)
    timeline_path: str | None = None
    cpu_busy_pct_mean: float | None = None
    cpu_busy_pct_max_core: float | None = None
    net_rx_peak_bytes_per_s: float | None = None
    # outcome
    gate_status: str | None = None
    status: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=_json_default)

    @classmethod
    def from_json(cls, s: str) -> TrialRow:
        d = json.loads(s)
        return cls(**{k: v for k, v in d.items() if k in _FIELD_SET})


FIELDS: tuple[str, ...] = tuple(f.name for f in fields(TrialRow))
_FIELD_SET = set(FIELDS)


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serializable: {type(o)}")


def _chunk_ids(sel: slice | np.ndarray | Sequence[int], chunk: int, n: int) -> set[int]:
    if isinstance(sel, slice):
        start, stop, _ = sel.indices(n)
        return set(range(start // chunk, (stop - 1) // chunk + 1)) if stop > start else set()
    return {int(i) // chunk for i in np.asarray(sel).ravel()}


def static_minimum_requests(
    shape: tuple[int, int, int],
    chunks: tuple[int, int, int],
    time_indices: Sequence[int],
    lat: slice | np.ndarray,
    lon: slice | np.ndarray,
    *,
    itemsize: int,
) -> tuple[int, int]:
    """Chunks (and their bytes) a selection must touch, from the chunk grid alone.

    Point selections (index arrays) are paired element-wise, so only the
    chunks that actually contain a point are counted.
    """
    if isinstance(lat, slice) or isinstance(lon, slice):
        per_step = len(_chunk_ids(lat, chunks[1], shape[1])) * len(_chunk_ids(lon, chunks[2], shape[2]))
    else:
        lat_a, lon_a = np.asarray(lat).ravel(), np.asarray(lon).ravel()
        per_step = len({(int(a) // chunks[1], int(b) // chunks[2]) for a, b in zip(lat_a, lon_a, strict=True)})
    count = per_step * len({int(t) for t in time_indices})
    return count, count * chunks[0] * chunks[1] * chunks[2] * itemsize


def virtual_minimum(time_indices: Sequence[int], lat: slice | np.ndarray, lon: slice | np.ndarray) -> tuple[int, int]:
    """Static minimum for the Icechunk arm over the two pinned variables."""
    count, nbytes = METADATA_OBJECTS, 0
    for chunks, itemsize in VARIABLES.values():
        c, b = static_minimum_requests(GRID_SHAPE, chunks, time_indices, lat, lon, itemsize=itemsize)
        count += c
        nbytes += b
    return count, nbytes


@dataclass
class ResourceDelta:
    cpu_user_s: float = 0.0
    cpu_sys_s: float = 0.0
    peak_rss_bytes: int = 0


@contextmanager
def resource_usage() -> Iterator[ResourceDelta]:
    """CPU seconds and peak RSS over the block, this process plus any child
    processes that have been waited on (the tuned arm's workers)."""
    delta = ResourceDelta()
    before_self = resource.getrusage(resource.RUSAGE_SELF)
    before_kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        yield delta
    finally:
        after_self = resource.getrusage(resource.RUSAGE_SELF)
        after_kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        delta.cpu_user_s = (after_self.ru_utime - before_self.ru_utime) + (after_kids.ru_utime - before_kids.ru_utime)
        delta.cpu_sys_s = (after_self.ru_stime - before_self.ru_stime) + (after_kids.ru_stime - before_kids.ru_stime)
        # Linux reports ru_maxrss in KiB; macOS in bytes. Children's peak is reported separately by the kernel; take the larger.
        scale = 1024 if sys.platform.startswith("linux") else 1
        delta.peak_rss_bytes = max(after_self.ru_maxrss, after_kids.ru_maxrss) * scale


def floor_seconds(serial_phases: int, rtt_s: float, bytes_total: int, bandwidth_bps: float) -> float:
    return serial_phases * rtt_s + bytes_total / bandwidth_bps


def cost_usd(requests: int, compute_s: float, instance_rate_usd_h: float) -> float:
    return requests / 1000 * S3_GET_USD_PER_1000 + compute_s / 3600 * instance_rate_usd_h


def _proc_value(path: str, key: str) -> str | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def cpu_steal_seconds() -> float | None:
    """Steal time from /proc/stat's aggregate cpu line, in seconds."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if len(parts) < 9 or parts[0] != "cpu":
        return None
    return int(parts[8]) / os.sysconf("SC_CLK_TCK")


def _imds(path: str, timeout: float = 0.5) -> str | None:
    import urllib.request

    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        token = urllib.request.urlopen(req, timeout=timeout).read().decode()
        req = urllib.request.Request(
            f"http://169.254.169.254/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token},
        )
        return urllib.request.urlopen(req, timeout=timeout).read().decode()
    except Exception:  # noqa: BLE001 -- off EC2 or IMDS disabled; the host field is just null
        return None


def host_info(imds: bool = True) -> dict[str, Any]:
    import platform

    import psutil

    mem = _proc_value("/proc/meminfo", "MemTotal")
    info: dict[str, Any] = {
        # /proc on Linux; platform/psutil elsewhere (macOS has no /proc)
        "cpu_model": _proc_value("/proc/cpuinfo", "model name") or platform.processor() or platform.machine() or None,
        "nproc": os.cpu_count(),
        "mem_total_bytes": int(mem.split()[0]) * 1024 if mem else int(psutil.virtual_memory().total),
        "instance_type": None,
        "instance_type_reason": None,
        "availability_zone": None,
        "host_kind": "other",
    }
    if os.environ.get("CODEBUILD_BUILD_ID"):
        info["host_kind"] = "codebuild"
        info["instance_type_reason"] = "codebuild: no IMDS"
        return info
    itype = _imds("instance-type") if imds else None
    if itype:
        info.update(instance_type=itype, availability_zone=_imds("placement/availability-zone"), host_kind="ec2")
    else:
        info["instance_type_reason"] = "imds unreachable"
    if os.environ.get("JUPYTERHUB_USER"):
        # a shared node: load and steal are node-wide; network counters are the pod's own
        info["host_kind"] = "jupyterhub"
    return info


def now() -> float:
    return time.perf_counter()
