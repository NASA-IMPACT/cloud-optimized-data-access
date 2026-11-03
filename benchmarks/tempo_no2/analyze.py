"""Aggregate a run's results.jsonl into summary.json and SVG figures, and
copy both into the deck.

Rows with ``status != "ok"`` or ``repetition == -1`` (warm-ups) are
excluded first. Cells are keyed by (arm, arm_settings, workload, case_size,
window) for cold trials. Speedups are paired by (session, repetition)
between each baseline arm and ``virtual_icechunk``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from benchmarks.tempo_no2.measure import FIELDS

PHASES = ("catalog", "open", "retrieval", "processing")
BASELINES = ("netcdf_user_stack", "netcdf_tuned")
VIRTUAL = "virtual_icechunk"
HEADLINE_ARMS = (*BASELINES, VIRTUAL)
SPECIAL_ARMS = ("noop", "catalog")
FIGURES = ("phases.svg", "requests.svg", "floor_ratio.svg", "speedup.svg")


def load(run_dir: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in (Path(run_dir) / "results.jsonl").read_text().splitlines() if line.strip()]
    df = pd.DataFrame(rows, columns=list(FIELDS))
    for col in ("arm_settings", "arm_variant"):
        df[col] = df[col].apply(lambda s: s if isinstance(s, dict) else {})
    return df


def _settings_key(settings: dict[str, Any]) -> str:
    return json.dumps(settings, sort_keys=True) if settings else ""


def _t_interval(values: np.ndarray) -> tuple[float, float]:
    from scipy import stats

    n = len(values)
    if n < 2:
        return float("nan"), float("nan")
    if n >= 10:
        m, se = values.mean(), values.std(ddof=1) / math.sqrt(n)
        h = stats.t.ppf(0.975, n - 1) * se
        return float(m - h), float(m + h)
    res = stats.bootstrap((values,), np.median, confidence_level=0.95, n_resamples=2000, random_state=0)
    return float(res.confidence_interval.low), float(res.confidence_interval.high)


def _limiter(cpu_fraction: float, nproc: float, max_core: float | None, bandwidth_fraction: float, floor_ratio: float) -> str:
    if max_core is not None and max_core >= 85:
        return "cpu-one-core"
    if nproc and cpu_fraction >= 0.8 * nproc:
        return "cpu-all-cores"
    if bandwidth_fraction >= 0.7:
        return "network-bandwidth"
    if cpu_fraction >= 0.9 and bandwidth_fraction < 0.5:
        return "cpu-decode"  # one core busy throughout while the network idles: decompression
    if floor_ratio is not None and not math.isnan(floor_ratio) and floor_ratio <= 2:
        return "latency"
    return "unexplained"


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    df = df.copy()
    df["settings_key"] = df["arm_variant"].apply(_settings_key)
    df["access"] = df["access"].astype(object).where(df["access"].notna(), "s3") if "access" in df else "s3"
    ok = df[(df["status"] == "ok") & (df["repetition"].fillna(0) >= 0)]
    noop = ok[ok["arm"] == "noop"]
    noop_rx = noop.groupby("session_id")["net_rx_bytes"].median().to_dict() if not noop.empty else {}
    # CMR search time per window, from the unpinned catalog trials; it is the file
    # arms' first phase. The virtual arm's coordinate lookup is inside its open.
    catalog = ok[ok["arm"] == "catalog"].groupby("window")["catalog_seconds"].median().to_dict()
    trials = ok[(~ok["arm"].isin(SPECIAL_ARMS)) & (ok["cache_state"] == "cold")]
    keys = ["arm", "settings_key", "workload", "case_size", "window", "access"]

    # Review focus 5: a headline arm with no ok rows in a cell where others have some.
    attempted = df[(df["arm"].isin(HEADLINE_ARMS)) & (df["cache_state"] == "cold") & (df["repetition"].fillna(0) >= 0)]
    for (workload, size, window), group in attempted.groupby(["workload", "case_size", "window"]):
        arms_ok = set(trials[(trials.workload == workload) & (trials.case_size == size) & (trials.window == window)]["arm"])
        for arm in sorted(set(group["arm"]) - arms_ok):
            if arms_ok:
                raise ValueError(f"no ok rows for arm {arm} in {workload}/{size}/{window}")

    cells: list[dict[str, Any]] = []
    for key, g in trials.groupby(keys, dropna=False):
        arm, settings_key, workload, size, window, access = key
        e2e = g["end_to_end_seconds"].astype(float)
        cpu = (g["cpu_user_s"].fillna(0) + g["cpu_sys_s"].fillna(0)) / e2e
        bw_probe = g["bandwidth_mb_s"].astype(float).median()
        bw_frac = (g["bytes_requested"].astype(float) / e2e / 1e6).median() / bw_probe if bw_probe and not math.isnan(bw_probe) else float("nan")
        floor_ratio = (e2e / g["floor_seconds"].astype(float)).median()
        # subtract each row's own session's noop baseline before taking the median
        rx = (g["net_rx_bytes"].astype(float) - g["session_id"].map(noop_rx).fillna(0.0)).median()
        nproc = g["nproc"].astype(float).median()
        max_core = g["cpu_busy_pct_max_core"].astype(float).median()
        cells.append({
            "arm": arm,
            "arm_variant": json.loads(settings_key) if settings_key else {},
            "arm_settings": next((v for v in g["arm_settings"] if v), {}),
            "workload": workload, "case_size": int(size), "window": window, "cache_state": "cold", "access": access,
            "n": len(g),
            "median_s": float(e2e.median()), "p90_s": float(e2e.quantile(0.9)),
            "min_s": float(e2e.min()), "max_s": float(e2e.max()),
            "cov": float(e2e.std(ddof=1) / e2e.mean()) if len(g) > 1 else 0.0,
            "phases": {
                "catalog": 0.0 if arm.startswith("virtual") else _nan_to_none(catalog.get(window)),
                **{p: _nan_to_none(g[f"{p}_seconds"].astype(float).median()) for p in PHASES if p != "catalog"},
            },
            "retrieval_split": _split_medians(g["retrieval_split"]),
            "request_count": _nan_to_none(g["request_count"].astype(float).median()),
            "request_count_kind": _count_kind(g["request_count_reason"]),
            "bytes_requested": _nan_to_none(g["bytes_requested"].astype(float).median()),
            "net_rx_bytes": _nan_to_none(rx),
            "retries": _nan_to_none(g["retries"].astype(float).sum()) if g["retries"].notna().any() else None,
            "floor_ratio": _nan_to_none(floor_ratio),
            "cpu_fraction": float(cpu.median()),
            "bandwidth_fraction": _nan_to_none(bw_frac),
            "cpu_busy_pct_max_core": _nan_to_none(max_core),
            "likely_limiter": _limiter(float(cpu.median()), nproc, None if math.isnan(max_core) else max_core,
                                       0.0 if math.isnan(bw_frac) else bw_frac, floor_ratio),
            "host": ", ".join(sorted(set(g["host_kind"].dropna().astype(str)))),
        })

    speedups: list[dict[str, Any]] = []
    plain = trials[trials["settings_key"] == ""]
    pair_keys = ["workload", "case_size", "window", "access", "session_id", "repetition"]
    for baseline in BASELINES:
        for (workload, size, window, access), g in plain.groupby(["workload", "case_size", "window", "access"]):
            merged = _paired(g, baseline, VIRTUAL, pair_keys)
            if merged.empty:
                continue
            row = _speedup_row(merged, workload, int(size), window, baseline, "headline" if baseline == "netcdf_user_stack" else "tuned")
            row["access"] = access
            speedups.append(row)
    one = trials[trials["settings_key"] == _settings_key({"max_workers": 1})]
    for (workload, size, window), g in one.groupby(["workload", "case_size", "window"]):
        eight = plain[(plain.arm == "netcdf_tuned") & (plain.workload == workload) & (plain.case_size == size) & (plain.window == window)]
        merged = g.merge(eight, on=["session_id", "repetition"], suffixes=("_b", "_v"))
        if merged.empty:
            continue
        speedups.append(_speedup_row(merged, workload, int(size), window, "netcdf_tuned[1 worker]", "concurrency_only"))

    first = trials.iloc[0] if not trials.empty else None
    return {
        "run_id": str(first["run_id"]) if first is not None else None,
        "cells": cells,
        "speedups": speedups,
        "provenance": {
            "snapshot_tag": first["snapshot_tag"] if first is not None else None,
            "snapshot_id": first["snapshot_id"] if first is not None else None,
            "git_sha": first["git_sha"] if first is not None else None,
            "hosts": sorted(set(trials["host_kind"].dropna().astype(str))),
            "instance_types": sorted(set(trials["instance_type"].dropna().astype(str))),
            "sessions": sorted(set(trials["session_id"].dropna().astype(str))),
        },
    }


def _paired(g: pd.DataFrame, baseline: str, virtual: str, keys: list[str]) -> pd.DataFrame:
    b = g[g.arm == baseline][keys + ["end_to_end_seconds"]]
    v = g[g.arm == virtual][keys + ["end_to_end_seconds"]]
    return b.merge(v, on=keys, suffixes=("_b", "_v"))


def _speedup_row(merged: pd.DataFrame, workload: str, size: int, window: str, baseline: str, label: str) -> dict[str, Any]:
    ratio = merged["end_to_end_seconds_b"].astype(float) / merged["end_to_end_seconds_v"].astype(float)
    logs = np.log(ratio.to_numpy())
    lo, hi = _t_interval(logs)
    per_session = ratio.groupby(merged["session_id"]).median()
    spread = float(per_session.max() - per_session.min()) if len(per_session) > 1 else 0.0
    median = float(ratio.median())
    return {
        "workload": workload, "case_size": size, "window": window,
        "baseline_arm": baseline, "label": label,
        "speedup_median": median,
        "ci_low": float(math.exp(lo)) if not math.isnan(lo) else None,
        "ci_high": float(math.exp(hi)) if not math.isnan(hi) else None,
        "n": len(merged),
        "session_spread": spread,
        "detectable": abs(math.log(median)) > math.log(1 + spread / median) if median > 0 else False,
    }


def _count_kind(reasons: pd.Series) -> str:
    """static-minimum (chunk grid), http-calls (botocore), or client-ranges (obspec tracer:
    Python calls into obstore, not HTTP requests; see developmentseed/obstore#105)."""
    r = reasons.fillna("")
    if r.str.startswith("static").any():
        return "static-minimum"
    if (r == "http-calls").any():
        return "http-calls"
    return "client-ranges"


def _split_medians(splits: pd.Series) -> dict[str, float]:
    """Median of each retrieval sub-phase over the rows that timed it (rows before the field existed have none)."""
    frame = pd.DataFrame([s for s in splits if isinstance(s, dict) and s])
    return {k: float(frame[k].median()) for k in frame.columns}


def _nan_to_none(x: Any) -> float | None:
    try:
        return None if x is None or math.isnan(float(x)) else float(x)
    except (TypeError, ValueError):
        return None


def figures(summary: dict[str, Any], out_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    # headline arms only: no orchestrator override, and not the once-per-session checker
    cells = [c for c in summary["cells"] if not c["arm_variant"] and c["arm"] != "virtual_manifeststore"]
    one_day = [c for c in cells if c["window"] == "1d"]
    labels = [f"{c['workload']}/{c['case_size']} {c['arm']}" for c in one_day]
    paths = []

    fig, ax = plt.subplots(figsize=(9, 0.4 * max(1, len(one_day)) + 1.5))
    left = np.zeros(len(one_day))
    for phase in PHASES:
        vals = np.array([c["phases"].get(phase) or 0.0 for c in one_day])
        ax.barh(labels, vals, left=left, label=phase)
        left += vals
    ax.set_xlabel("seconds (median)")
    ax.legend(loc="lower right")
    ax.set_title("Where the seconds go, one event day")
    fig.tight_layout()
    paths.append(_save(fig, out_dir / "phases.svg"))

    fig, ax = plt.subplots(figsize=(9, 4))
    for arm in sorted({c["arm"] for c in cells}):
        pts = [(f"{c['workload']}/{c['case_size']}/{c['window']}", c["request_count"] or 0.5) for c in cells if c["arm"] == arm]
        if pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=arm)
    ax.set_yscale("log")
    ax.set_ylabel("requests (median)")
    ax.legend()
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    paths.append(_save(fig, out_dir / "requests.svg"))

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.bar(labels, [c["floor_ratio"] or 0.0 for c in one_day])
    ax.axhline(1.0, color="k", linewidth=0.8)
    ax.set_ylabel("end-to-end / floor")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    paths.append(_save(fig, out_dir / "floor_ratio.svg"))

    fig, ax = plt.subplots(figsize=(9, 4))
    sp = summary["speedups"]
    xs = [f"{s['workload']}/{s['case_size']}/{s['window']}\n{s['label']}" for s in sp]
    ys = [s["speedup_median"] for s in sp]
    err = [[max(0.0, y - (s["ci_low"] or y)) for s, y in zip(sp, ys)], [max(0.0, (s["ci_high"] or y) - y) for s, y in zip(sp, ys)]]
    ax.errorbar(xs, ys, yerr=err, fmt="o", capsize=4)
    ax.axhline(1.0, color="k", linewidth=0.8)
    ax.set_ylabel("speedup (paired median, 95% CI)")
    ax.tick_params(axis="x", labelsize=7)
    fig.tight_layout()
    paths.append(_save(fig, out_dir / "speedup.svg"))
    for name, size, bars in SCAN_FIGURES:
        path = scan_figure(summary, out_dir, name, size, bars)
        if path:
            paths.append(path)
    return paths


# Whole-scan figures for the deck: (file name, granules, bars as (label, arm, arm_variant) top to bottom).
SCAN_FIGURES = (
    ("scan_1.svg", 1, (
        ("Default stack", "netcdf_user_stack", {}),
        ("Tuned h5py", "netcdf_tuned", {"max_workers": 1}),
        ("Virtual Zarr", "virtual_icechunk", {}),
        ("Download + h5py", "netcdf_download", {}),
    )),
    ("scan_15.svg", 15, (
        ("Tuned h5py, 1 process", "netcdf_tuned", {"max_workers": 1}),
        ("Tuned h5py, 8 processes", "netcdf_tuned", {}),
        ("Virtual Zarr", "virtual_icechunk", {}),
        ("Download + h5py", "netcdf_download", {}),
    )),
)


def scan_figure(summary: dict[str, Any], out_dir: Path, name: str, size: int, bars: tuple) -> Path | None:
    """End to end for a whole scan of `size` granules, one bar per library, split into setup + open
    and read + decode. Bars the run lacks are left out; None when fewer than two remain."""
    import matplotlib.pyplot as plt

    cells = {}
    for label, arm, variant in bars:
        match = [c for c in summary["cells"] if c["arm"] == arm and c["arm_variant"] == variant
                 and (c["workload"], c["case_size"], c["window"]) == ("scan", size, "1d")]
        if match:
            cells[label] = match[0]
    if len(cells) < 2:
        return None

    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # Inter is the deck's font, often not installed where this runs
    out_dir.mkdir(parents=True, exist_ok=True)
    # svg.fonttype none: text stays text, so the deck renders it in Inter
    style = {"font.family": ["Inter", "DejaVu Sans", "sans-serif"], "font.size": 13, "svg.fonttype": "none"}
    with plt.rc_context(style):
        return _draw_scan(cells, size, out_dir / name)


def _draw_scan(cells: dict[str, dict[str, Any]], size: int, path: Path) -> Path:
    import matplotlib.pyplot as plt

    setup_c, read_c, ink, muted = "#d97e6a", "#952f7c", "#3f3f48", "#6b6b76"  # deck palette; validated as a pair
    fig, ax = plt.subplots(figsize=(11, 0.75 * len(cells) + 1.2))
    labels = list(cells)[::-1]
    opens = np.array([cells[label]["phases"]["open"] or 0.0 for label in labels])
    reads = np.array([(cells[label]["phases"]["retrieval"] or 0.0) + (cells[label]["phases"]["processing"] or 0.0) for label in labels])
    gap = 0.04 * max(opens + reads) / 30  # a thin surface gap between the two segments
    ax.barh(labels, opens - gap, color=setup_c, height=0.62, label="Setup + open")
    ax.barh(labels, reads, left=opens, color=read_c, height=0.62, label="Read + decode")
    top = max(c["median_s"] for c in cells.values())
    for y, label in enumerate(labels):
        ax.text(cells[label]["median_s"] + top * 0.012, y, f"{cells[label]['median_s']:.1f} s", va="center", color=ink)
    granules = "one granule" if size == 1 else f"{size} granules"
    ax.set_xlabel(f"seconds, whole scan of {granules} (median of cold runs)", color=muted)
    ax.set_xlim(0, top * 1.12)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(muted)
    ax.tick_params(axis="y", length=0, colors=ink)
    ax.tick_params(axis="x", colors=muted)
    # above the plot: the longest bar runs into any corner inside it
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, labelcolor=ink)
    fig.tight_layout()
    return _save(fig, path)


def _save(fig: Any, path: Path) -> Path:
    """Write the figure as SVG (for the deck) and PNG (for anything else) side by side."""
    import matplotlib.pyplot as plt

    fig.savefig(path, format="svg")
    fig.savefig(path.with_suffix(".png"), format="png", dpi=160)
    plt.close(fig)
    return path


def publish(run_dir: Path, deck_dir: Path) -> None:
    run_dir, deck_dir = Path(run_dir), Path(deck_dir)
    summary = summarize(load(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    fig_paths = figures(summary, run_dir / "figures")
    (deck_dir / "public" / "results").mkdir(parents=True, exist_ok=True)
    (deck_dir / "public" / "figures").mkdir(parents=True, exist_ok=True)
    shutil.copy(run_dir / "summary.json", deck_dir / "public" / "results" / "summary.json")
    for p in fig_paths:
        for out in (p, p.with_suffix(".png")):
            shutil.copy(out, deck_dir / "public" / "figures" / out.name)
    (run_dir.parent / "latest.json").write_text(json.dumps({"run_id": summary["run_id"]}) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.tempo_no2.analyze", description=__doc__)
    p.add_argument("run_dir")
    p.add_argument("--deck", default="decks/coda-presentation")
    args = p.parse_args(argv)
    publish(Path(args.run_dir), Path(args.deck))
    print(f"published {args.run_dir} → {args.deck}/public and {Path(args.run_dir).parent / 'latest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
