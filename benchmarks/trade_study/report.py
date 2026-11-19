"""Medians per arm, case and measure, and paired speedups → ``summary.json``,
``summary.md`` and one figure per case.

Only ``status == "ok"`` rows count, and a trial counts once: when a file
holds the same (cell, session, repetition) twice, the later row is kept and
the summary says how many were set aside. A cell is (dataset, transport, arm,
variant, case, measure, in_region); in-region and out-of-region rows never
share a cell.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from benchmarks.trade_study import legacy
from benchmarks.trade_study.counters import STATIC_MINIMUM
from benchmarks.trade_study.gate import PASSING

TIME_PARTS = ("auth_seconds", "manifest_seconds", "chunk_id_seconds", "byte_seconds")
MEDIANS = (
    *TIME_PARTS,
    "first_byte_seconds", "last_byte_seconds", "processing_seconds", "end_to_end_seconds", "warm_seconds",
    "startup_seconds", "request_count", "bytes_requested", "net_rx_bytes", "floor_seconds", "peak_rss_bytes",
    "requests_usd", "transfer_usd", "compute_usd", "cost_usd",
)  # fmt: skip
# Copied from a cell's first row: the same for every row of the cell.
CONSTANTS = (
    "arm_status", "section", "gate_status", "request_count_kind", "auth_reason", "chunk_id_reason", "manifest_reason",
    "observation_count", "index_location", "discovery_seconds", "discovery_kind", "host_kind", "client_region",
)  # fmt: skip
CASE_KEYS = ["dataset", "transport", "in_region", "workload", "case_size", "window", "measure"]
KEYS = [*CASE_KEYS, "arm", "variant"]
PAIR_KEYS = ["session_id", "repetition"]  # two trials are a pair when they share both
VIRTUAL = "virtual_icechunk"  # what every other arm's speedup is measured against


def headline(measure: str) -> str:
    """The row field a measure is judged by."""
    return "first_byte_seconds" if measure == "first_byte" else "end_to_end_seconds"


def load(run_dir: Path) -> pd.DataFrame:
    """The run's rows, with ``variant`` (the arm variant as sorted JSON, "" for none)."""
    lines = (Path(run_dir) / "results.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    if rows and legacy.is_legacy(rows[0]):
        rows = legacy.upgrade(rows)
    frame = pd.DataFrame(rows)
    frame["variant"] = frame["arm_variant"].apply(lambda v: json.dumps(v, sort_keys=True) if v else "")
    frame["transport"] = frame["transport"].fillna("s3") if "transport" in frame else "s3"
    return frame


# --- medians ----------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """A pandas or numpy scalar as a JSON-ready Python value; missing becomes None."""
    if not isinstance(value, dict | list) and pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


def _cell(key: tuple, group: pd.DataFrame) -> dict[str, Any]:
    cell: dict[str, Any] = dict(zip(KEYS, key, strict=True))
    cell["case_size"], cell["in_region"] = int(cell["case_size"]), bool(cell["in_region"])
    cell["n"] = len(group)
    for name in MEDIANS:
        values = pd.to_numeric(group[name], errors="coerce").dropna() if name in group else pd.Series(dtype=float)
        cell[name] = float(values.median()) if len(values) else None
    seconds = pd.to_numeric(group[headline(cell["measure"])], errors="coerce")
    cell["min_s"], cell["max_s"] = float(seconds.min()), float(seconds.max())
    median = cell[headline(cell["measure"])]
    cell["floor_ratio"] = median / cell["floor_seconds"] if cell["floor_seconds"] else None
    first = group.iloc[0]
    for name in CONSTANTS:
        cell[name] = _plain(first.get(name))
    return cell


# --- paired speedups --------------------------------------------------------------


def _interval(log_ratios: np.ndarray) -> tuple[float, float]:
    """A 95% interval on the median log speedup, the number the report quotes:
    a percentile bootstrap over the pairs. NaN with fewer than 2."""
    from scipy import stats

    if len(log_ratios) < 2:
        return float("nan"), float("nan")
    resampled = stats.bootstrap(
        (log_ratios,), np.median, confidence_level=0.95, n_resamples=2000, method="percentile", random_state=0
    )
    return float(resampled.confidence_interval.low), float(resampled.confidence_interval.high)


def _speedup(case: dict[str, Any], slow: pd.DataFrame, fast: pd.DataFrame, seconds: str) -> dict[str, Any] | None:
    """How many times faster ``fast`` is than ``slow``, over the trials that pair up."""
    pairs = slow[[*PAIR_KEYS, seconds]].merge(fast[[*PAIR_KEYS, seconds]], on=PAIR_KEYS, suffixes=("_slow", "_fast"))
    if pairs.empty:
        return None
    ratio = pairs[f"{seconds}_slow"].astype(float) / pairs[f"{seconds}_fast"].astype(float)
    low, high = _interval(np.log(ratio.to_numpy()))
    median = float(ratio.median())
    per_session = ratio.groupby(pairs["session_id"]).median()
    # the spread between sessions needs two sessions; with one there is nothing to judge a speedup against
    spread = float(per_session.max() - per_session.min()) if len(per_session) > 1 else None
    detectable = None
    if spread is not None:
        detectable = abs(math.log(median)) > math.log(1 + spread / median) if median > 0 else False
    return {
        **case,
        "from_arm": slow["arm"].iloc[0],
        "from_variant": slow["variant"].iloc[0],
        "to_arm": fast["arm"].iloc[0],
        "to_variant": fast["variant"].iloc[0],
        "speedup_median": median,
        "ci_low": None if math.isnan(low) else math.exp(low),
        "ci_high": None if math.isnan(high) else math.exp(high),
        "n": len(pairs),
        "sessions": len(per_session),
        # sessions disagree by this much; a speedup inside it is not a detectable difference.
        # Both are null for a single session.
        "session_spread": spread,
        "detectable": detectable,
    }


def paired_speedups(ok: pd.DataFrame) -> list[dict[str, Any]]:
    """For every case: each arm against the virtual store, and each arm variant
    against the same arm's defaults (what the tuning factor alone is worth)."""
    speedups = []
    for key, group in ok.groupby(CASE_KEYS, dropna=False):
        case = dict(zip(CASE_KEYS, key, strict=True))
        case["case_size"], case["in_region"] = int(case["case_size"]), bool(case["in_region"])
        seconds = headline(case["measure"])
        arms = {arm_key: rows for arm_key, rows in group.groupby(["arm", "variant"])}
        virtual = arms.get((VIRTUAL, ""))
        for (arm, variant), rows in arms.items():
            if virtual is not None and arm != VIRTUAL:
                speedups.append(_speedup(case, rows, virtual, seconds))
            if variant and (arm, "") in arms:
                speedups.append(_speedup(case, rows, arms[(arm, "")], seconds))
    return [speedup for speedup in speedups if speedup is not None]


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    every_ok = frame[frame["status"] == "ok"]
    # one row per trial: a session run twice under one name must not pair a trial with both copies
    trial_keys = [key for key in (*KEYS, *PAIR_KEYS) if key in every_ok]
    ok = every_ok.drop_duplicates(subset=trial_keys, keep="last")
    failed = frame[frame["status"] != "ok"]
    return {
        "run_id": frame["run_id"].iloc[0],
        "rows": len(frame),
        "failed": len(failed),
        "repeated": len(every_ok) - len(ok),
        "failures": failed[["arm", "workload", "case_size", "measure", "status"]].to_dict(orient="records"),
        "git_sha": frame["git_sha"].dropna().iloc[0] if frame["git_sha"].notna().any() else None,
        "cells": [_cell(key, group) for key, group in ok.groupby(KEYS, dropna=False)],
        "speedups": paired_speedups(ok),
    }


# --- summary.md -------------------------------------------------------------------


def _s(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _mb(value: float | None) -> str:
    return "—" if value is None else f"{value / 1e6:,.1f}"


def _case(cell: dict[str, Any]) -> str:
    return f"`{cell['workload']}:{cell['case_size']}:{cell['window']}`"


def _arm(arm: str, variant: str, gate_status: str | None = "pass") -> str:
    """The arm's name, its variant if any, and † when it was not gated."""
    name = f"`{arm}`" + (f" {variant}" if variant else "")
    return name + ("" if gate_status in PASSING else " †")


def _requests(cell: dict[str, Any]) -> str:
    if cell["request_count"] is None:
        return "—"
    return ("≥ " if cell["request_count_kind"] == STATIC_MINIMUM else "") + f"{cell['request_count']:,.0f}"


def _table(header: list[str], align: str, rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", align, *("| " + " | ".join(row) + " |" for row in rows)]


def _sorted_cells(cells: list[dict[str, Any]], measure: str) -> list[dict[str, Any]]:
    """One measure's cells, by case and then fastest first."""
    mine = [cell for cell in cells if cell["measure"] == measure]
    return sorted(mine, key=lambda c: (c["workload"], c["case_size"], c["window"], c[headline(measure)]))


def _first_byte_table(cells: list[dict[str, Any]]) -> list[str]:
    header = ["Case", "Granules", "Arm", "n", "Auth", "Manifest", "Chunk id", "Bytes", "**First byte**", "min–max",
              "Warm", "Requests", "MB on wire"]  # fmt: skip
    rows = [
        [
            _case(c),
            str(c["observation_count"]),
            _arm(c["arm"], c["variant"], c["gate_status"]),
            str(c["n"]),
            _s(c["auth_seconds"]),
            _s(c["manifest_seconds"]),
            _s(c["chunk_id_seconds"]),
            _s(c["byte_seconds"]),
            f"**{_s(c['first_byte_seconds'])}**",
            f"{_s(c['min_s'])}–{_s(c['max_s'])}",
            _s(c["warm_seconds"], 3),
            _requests(c),
            _mb(c["net_rx_bytes"]),
        ]  # fmt: skip
        for c in _sorted_cells(cells, "first_byte")
    ]
    return _table(header, "|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|", rows)


def _full_table(cells: list[dict[str, Any]]) -> list[str]:
    header = ["Case", "Granules", "Arm", "n", "**End to end**", "min–max", "Last byte", "Warm", "Floor", "× floor",
              "Requests", "MB on wire", "Peak RSS MB", "$ per question"]  # fmt: skip
    rows = [
        [
            _case(c),
            str(c["observation_count"]),
            _arm(c["arm"], c["variant"], c["gate_status"]),
            str(c["n"]),
            f"**{_s(c['end_to_end_seconds'])}**",
            f"{_s(c['min_s'])}–{_s(c['max_s'])}",
            _s(c["last_byte_seconds"]),
            _s(c["warm_seconds"]),
            _s(c["floor_seconds"]),
            _s(c["floor_ratio"], 1),
            _requests(c),
            _mb(c["net_rx_bytes"]),
            _mb(c["peak_rss_bytes"]),
            _s(c["cost_usd"], 6),
        ]  # fmt: skip
        for c in _sorted_cells(cells, "full")
    ]
    return _table(header, "|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|", rows)


ONE_SESSION = (
    "Speedups measured in one session have no spread between sessions to be judged against: "
    "whether they are a detectable difference is not known until a second session is run."
)


def _speedup_table(speedups: list[dict[str, Any]]) -> list[str]:
    header = ["Case", "Measure", "From", "To", "**Speedup**", "95% interval", "Pairs", ""]
    rows = []
    for s in sorted(speedups, key=lambda s: (s["measure"], s["workload"], s["case_size"], s["window"], s["to_arm"])):
        interval = "—" if s["ci_low"] is None else f"{s['ci_low']:.2f}–{s['ci_high']:.2f}"
        # None: a single session, which the line under the table says once
        note = "inside the spread between sessions" if s["detectable"] is False else ""
        rows.append([
            _case(s), s["measure"], _arm(s["from_arm"], s["from_variant"]), _arm(s["to_arm"], s["to_variant"]),
            f"**{s['speedup_median']:.2f}×**", interval, str(s["n"]), note,
        ])  # fmt: skip
    table = _table(header, "|---|---|---|---|---:|---|---:|---|", rows)
    if any(s["detectable"] is None for s in speedups):
        table += ["", ONE_SESSION]
    return table


def _context(cells: list[dict[str, Any]]) -> str:
    """One sentence on where the client was and what it read."""
    first = cells[0]
    transports = ", ".join(sorted({cell["transport"] for cell in cells}))
    if first["in_region"]:
        place = "in region"
    else:
        region = first["client_region"] or "unknown, not AWS"
        place = f"**out of region** (client region: {region}; host: {first['host_kind']})"
    return (
        f"Transport: {transports}. Client: {place}. Virtual-store index read from: {first['index_location']}. "
        f"Discovery ({first['discovery_kind']}): {_s(first['discovery_seconds'])} s, "
        "measured when the granules were pinned; not in first byte."
    )


READING_THE_TABLES = (
    "- Requests marked ≥ are a static minimum from the chunk grid, not a count. "
    "Other counts are byte ranges the client asked for.",
    "- MB on wire is the host's receive counter over the trial, so it includes other traffic on a shared host.",
    "- Floor = dependent round trips × median round-trip time + bytes ÷ measured bandwidth, from the session's probe.",
    "- $ per question = S3 requests + transfer + compute at list price. "
    "Transfer is zero in region and internet egress otherwise.",
    "- A speedup is the median, over trials paired by session and repetition, of one arm's seconds over another's. "
    "Its interval is a bootstrap of that median.",
    "- Arms marked † are not gated: their values were not checked against the reference arm.",
)


def render(summary: dict[str, Any]) -> str:
    cells = summary["cells"]
    out = [
        f"# Trade-study run `{summary['run_id']}`",
        "",
        f"{summary['rows']} trials, {summary['failed']} failed. Medians of cold runs, one fresh process each.",
    ]
    if summary.get("repeated"):
        out += ["", f"{summary['repeated']} rows repeat an earlier trial of the same session; the later row is used."]
    if cells:
        out += ["", _context(cells)]
    for dataset in sorted({cell["dataset"] for cell in cells}):
        mine = [cell for cell in cells if cell["dataset"] == dataset]
        out += ["", f"## {dataset}: time to first byte, in parts (seconds)", "", *_first_byte_table(mine)]
        out += ["", f"## {dataset}: the whole question (seconds)", "", *_full_table(mine)]
        speedups = [s for s in summary["speedups"] if s["dataset"] == dataset]
        if speedups:
            out += ["", f"## {dataset}: paired speedups", "", *_speedup_table(speedups)]
    reasons = {(c["arm"], c[field]) for c in cells for field in ("chunk_id_reason", "manifest_reason") if c[field]}
    reasons |= {(c["arm"], c["auth_reason"]) for c in cells if c["auth_reason"] and c["auth_seconds"] is None}
    out += ["", "## Reading the tables", ""]
    out += ["- A dash under a part means the arm does not expose that step; its time is inside Bytes:"]
    out += [f"  - `{arm}`: {reason}." for arm, reason in sorted(reasons)]
    out += READING_THE_TABLES
    if summary["failures"]:
        out += ["", "## Failed trials", ""]
        out += [
            f"- {f['arm']} {f['workload']}:{f['case_size']} {f['measure']}: {f['status'][:200]}"
            for f in summary["failures"]
        ]
    return "\n".join(out) + "\n"


def write_report(run_dir: Path, figures: bool = True) -> str:
    """Write ``summary.json``, ``summary.md`` and, unless told not to, ``figures/``. Returns the Markdown."""
    run_dir = Path(run_dir)
    summary = summarize(load(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    text = render(summary)
    (run_dir / "summary.md").write_text(text)
    if figures:
        from benchmarks.trade_study.figures import write_figures

        write_figures(summary, run_dir / "figures")
    return text
