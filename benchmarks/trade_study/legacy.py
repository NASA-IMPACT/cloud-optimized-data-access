"""Reading results written by the first TEMPO NO₂ benchmark.

That benchmark (``benchmarks/tempo_no2``, removed when the two harnesses were
merged) wrote rows in an older shape. The only in-region measurements made so
far, the "hub" run the CODA talk quotes, are in that shape, so ``report``
upgrades such rows on load instead of refusing them. Delete this module once
that run has been repeated with this harness.

What the old rows cannot say stays null: they never timed authentication on
its own (it is inside ``manifest_seconds``), never measured first byte, and
priced compute and requests only.
"""

from __future__ import annotations

from dataclasses import asdict
from statistics import median
from typing import Any

from benchmarks.trade_study.counters import CLIENT_RANGES, HTTP_CALLS, STATIC_MINIMUM
from benchmarks.trade_study.trial import Row

ARMS = {
    "netcdf_user_stack": "user_stack",
    "netcdf_tuned": "tuned_h5py",
    "netcdf_download": "download",
    "virtual_icechunk": "virtual_icechunk",
}
WORKLOADS = {"scan": "pancake", "point": "churro", "aoi": "box"}
# Copied across under the same name.
UNCHANGED = (
    "run_id", "session_id", "arm_settings", "arm_variant", "case_size", "window", "observation_count", "repetition",
    "git_sha", "uv_lock_sha256", "packages", "manifest_path", "snapshot_id", "host_kind", "instance_type",
    "availability_zone", "cpu_model", "nproc", "processing_seconds", "end_to_end_seconds", "cpu_user_s", "cpu_sys_s",
    "peak_rss_bytes", "load_avg_1m", "cpu_steal_s", "request_count", "requests_by_method", "bytes_requested",
    "net_rx_bytes", "rtt_ms_median", "bandwidth_mb_s", "floor_seconds", "gate_status", "status", "error",
)  # fmt: skip


def is_legacy(row: dict[str, Any]) -> bool:
    """The old rows have no ``measure``; every row this harness writes does."""
    return "measure" not in row


def _count_kind(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason.startswith("static"):
        return STATIC_MINIMUM
    return HTTP_CALLS if reason == HTTP_CALLS else CLIENT_RANGES


def _upgrade_row(old: dict[str, Any], discovery_seconds: float | None) -> dict[str, Any]:
    transport = old.get("access") or "s3"
    opened, retrieved = old.get("open_seconds"), old.get("retrieval_seconds")
    row = asdict(Row())  # every field this harness writes, at its default
    row.update({name: old.get(name) for name in UNCHANGED})
    row.update(
        dataset="tempo_no2",
        arm=ARMS[old["arm"]],
        workload=WORKLOADS[old["workload"]],
        measure="full",
        cache_state="cold",
        transport=transport,
        in_region=transport == "s3",  # ASDC's S3 credentials only work from us-west-2
        discovery_seconds=discovery_seconds,
        discovery_kind="cmr",
        auth_reason="inside manifest_seconds: the first benchmark did not time it apart",
        manifest_seconds=opened,
        chunk_id_reason="inside byte_seconds: the first benchmark did not time it apart",
        byte_seconds=retrieved,
        byte_split=old.get("retrieval_split") or {},
        last_byte_seconds=opened + retrieved if opened is not None and retrieved is not None else None,
        request_count_kind=_count_kind(old.get("request_count_reason")),
        cost_usd=old.get("cost_usd_estimate"),
    )
    row["arm_settings"] = row["arm_settings"] or {}
    row["arm_variant"] = row["arm_variant"] or {}
    return row


def upgrade(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Old rows in this harness's shape. Kept: cold, timed trials of the four
    arms both harnesses have. Dropped: warm-ups (repetition -1), warm-process
    trials, and the old ``noop``, ``catalog`` and ``virtual_manifeststore``
    rows, which have no counterpart. The old ``catalog`` rows' median search
    time per window becomes ``discovery_seconds``."""
    searches: dict[str, list[float]] = {}
    for row in rows:
        if row.get("arm") == "catalog" and row.get("status") == "ok" and row.get("catalog_seconds") is not None:
            searches.setdefault(row["window"], []).append(row["catalog_seconds"])
    discovery = {window: median(seconds) for window, seconds in searches.items()}
    return [
        _upgrade_row(row, discovery.get(row["window"]))
        for row in rows
        if row.get("arm") in ARMS and row.get("cache_state") == "cold" and (row.get("repetition") or 0) >= 0
    ]
