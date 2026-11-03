"""Expand the matrix, estimate its cost, and run one subprocess per trial.

Nothing starts without an estimate and ``--yes``. Each trial is a fresh
process (``cold``); ``warm_process`` trials reuse a backend inside one
process. A host sampler in this process records a one-second timeline per
trial (per-core CPU, network, steal) without touching the trial process.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.tempo_no2.cases import Case, matrix
from benchmarks.tempo_no2.measure import GRID_SHAPE, VARIABLES, TrialRow, cost_usd, static_minimum_requests
from benchmarks.tempo_no2.run import DEFAULT_ARMS, DEFAULT_INSTANCE_RATE, parse_arms

ARMS = ("netcdf_user_stack", "netcdf_tuned", "virtual_icechunk")
CHECK_CASE = Case("point", 10, "1d")
CATALOG_REPS = 3
CONCURRENCY_FACTOR_CASE = Case("aoi", 100, "1d")
SMOKE_CASE_SIZE = {"point": 1, "aoi": 50}
# Smoke-run cases read one observation; AOI request counts scale with the
# chunks a window touches, which the static minimum gives for any size.
AOI_KM_PER_PIXEL = (0.02 * 110.574, 0.02 * 111.320 * 0.8)  # TEMPO L3 grid ≈ 0.02°, cos(37°)≈0.8


@dataclass(frozen=True)
class TrialSpec:
    arm: str
    case: Case
    rep: int
    cache_state: str
    settings: dict[str, Any] = field(default_factory=dict, compare=True, hash=False)

    def __hash__(self) -> int:
        return hash((self.arm, self.case, self.rep, self.cache_state, tuple(sorted(self.settings.items()))))


def plan(
    headline_only: bool,
    reps_headline: int = 10,
    reps_other: int = 5,
    cache_states: tuple[str, ...] = ("cold", "warm_process"),
    seed: int = 20250314,
    arms: tuple[str, ...] = DEFAULT_ARMS,
) -> list[TrialSpec]:
    """``arms`` restricts the timed arms (the noop, catalog, checker and concurrency
    specs are kept when their arm is in the list or they are arm-independent)."""
    rng = random.Random(seed)
    specs: list[TrialSpec] = [TrialSpec("noop", Case("point", 1, "1d"), 0, "cold")]
    headline = set(matrix(headline_only=True))
    for case in matrix(headline_only):
        reps = reps_headline if case in headline else reps_other
        for cache_state in cache_states:
            for rep in range(reps):
                block = [TrialSpec(arm, case, rep, cache_state) for arm in ARMS if arm in arms]
                rng.shuffle(block)
                specs += block
    if "virtual_icechunk" in arms:
        specs.append(TrialSpec("virtual_manifeststore", CHECK_CASE, 0, "cold"))
    if "netcdf_tuned" in arms:
        specs += [TrialSpec("netcdf_tuned", CONCURRENCY_FACTOR_CASE, rep, "cold", {"max_workers": 1}) for rep in range(reps_headline)]
    windows = sorted({c.window for c in matrix(headline_only)})
    specs += [TrialSpec("catalog", Case("point", 1, w), rep, "cold") for w in windows for rep in range(CATALOG_REPS)]
    return specs


@dataclass
class Estimate:
    trials: int
    requests_total: int
    requests_by_bucket: dict[str, int]
    hours: float
    usd: float


def _aoi_chunks(size_km: int) -> int:
    rows = int(size_km / AOI_KM_PER_PIXEL[0])
    cols = int(size_km / AOI_KM_PER_PIXEL[1])
    total = 0
    for chunks, itemsize in VARIABLES.values():
        n, _ = static_minimum_requests(GRID_SHAPE, chunks, [0], slice(0, rows), slice(0, cols), itemsize=itemsize)
        total += n
    return max(1, total)


def _point_chunks(spatial: Any, n_points: int) -> int:
    """Distinct chunks the first ``n_points`` touch, summed over the pinned variables."""
    rows = spatial.points.iloc[:n_points]
    lat_idx, lon_idx = rows["lat_index"].to_numpy(), rows["lon_index"].to_numpy()
    total = 0
    for chunks, itemsize in VARIABLES.values():
        n, _ = static_minimum_requests(GRID_SHAPE, chunks, [0], lat_idx, lon_idx, itemsize=itemsize)
        total += n
    return max(1, total)


def estimate(
    specs: list[TrialSpec], smoke_rows: list[TrialRow], n_obs: dict[str, int], instance_rate_usd_h: float, spatial: Any | None = None
) -> Estimate:
    """Project the matrix from the smoke run's per-trial seconds and requests.

    Open cost is constant per trial. Retrieval and processing scale with the
    observation count and with the chunks a selection touches (points are
    read by chunk, so 250 points cost a few chunks, not 250 reads). Without
    ``spatial`` the point ratio falls back to the chunk grid's worst case.
    """
    by_key = {(r.arm, r.workload): r for r in smoke_rows if r.status == "ok" and r.window == "1d"}
    seconds, requests, by_bucket = 0.0, 0, {"asdc": 0, "mirror": 0}
    for s in specs:
        base = by_key.get((s.arm, s.case.workload)) or by_key.get(("virtual_icechunk", s.case.workload))
        if base is None:
            continue
        scale = n_obs[s.case.window] / max(1, base.observation_count or 1)
        if s.case.workload == "point":
            if spatial is not None:
                scale *= _point_chunks(spatial, s.case.case_size) / _point_chunks(spatial, SMOKE_CASE_SIZE["point"])
            else:
                scale *= min(s.case.case_size, 16)  # worst case: every point in its own chunk, bounded by the grid
        else:
            scale *= _aoi_chunks(s.case.case_size) / _aoi_chunks(SMOKE_CASE_SIZE["aoi"])
        open_s = base.open_seconds or 0.0
        work_s = max(0.0, (base.end_to_end_seconds or 0.0) - open_s)
        req = math.ceil((base.request_count or 0) * scale)
        seconds += open_s + work_s * scale
        requests += req
        if s.arm.startswith("virtual"):
            by_bucket["mirror"] += 4
            by_bucket["asdc"] += max(0, req - 4)
        else:
            by_bucket["asdc"] += req
    return Estimate(len(specs), requests, by_bucket, seconds / 3600, cost_usd(requests, seconds, instance_rate_usd_h))


def format_estimate(e: Estimate) -> str:
    return (
        f"trials: {e.trials}\nrequests: {e.requests_total:,} (asdc {e.requests_by_bucket['asdc']:,}, "
        f"mirror {e.requests_by_bucket['mirror']:,})\nprojected wall-clock: {e.hours:.1f} h\n"
        f"projected cost: ${e.usd:.2f} (S3 GETs at list price + instance hours)"
    )


class HostSampler:
    """Samples the host once a second from this process while a trial runs elsewhere."""

    def __init__(self, interval_s: float = 1.0) -> None:
        self.interval_s = interval_s
        self._rows: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        import psutil

        from benchmarks.tempo_no2.measure import cpu_steal_seconds

        t0 = time.monotonic()
        psutil.cpu_times_percent(percpu=True)  # prime the interval measurement
        net_prev, steal_prev, t_prev = psutil.net_io_counters().bytes_recv, cpu_steal_seconds() or 0.0, time.monotonic()
        while not self._stop.wait(self.interval_s):
            per_core = psutil.cpu_times_percent(percpu=True)
            busy = [100.0 - c.idle for c in per_core]
            net, steal, t = psutil.net_io_counters().bytes_recv, cpu_steal_seconds() or 0.0, time.monotonic()
            dt = max(1e-6, t - t_prev)
            self._rows.append({
                "t": round(t - t0, 3),
                "cpu_busy_pct_mean": sum(busy) / len(busy),
                "cpu_busy_pct_max_core": max(busy),
                "net_rx_bytes_per_s": (net - net_prev) / dt,
                "steal_pct": 100.0 * (steal - steal_prev) / dt,
            })
            net_prev, steal_prev, t_prev = net, steal, t

    def start(self) -> None:
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()

    def timeline(self) -> list[dict[str, Any]]:
        return list(self._rows)


def default_command(spec: TrialSpec, run_id: str = "", session: str = "", probes: str | None = None, access: str = "s3") -> list[str]:
    if spec.arm == "catalog":
        return [sys.executable, "-m", "benchmarks.tempo_no2.run", "catalog", "--window", spec.case.window,
                "--rep", "1", "--run-id", run_id, "--session", session]
    cmd = [sys.executable, "-m", "benchmarks.tempo_no2.run", "trial", "--arm", spec.arm, "--case", str(spec.case),
           "--rep", str(spec.rep), "--cache", spec.cache_state, "--run-id", run_id, "--session", session, "--access", access]
    if spec.settings:
        cmd += ["--settings", json.dumps(spec.settings)]
    if probes:
        cmd += ["--probes", probes]
    return cmd


def run_matrix(
    specs: list[TrialSpec],
    run_id: str,
    session: str,
    out_dir: Path,
    trial_command: Callable[[TrialSpec], list[str]] | None = None,
    timeout_s: float = 1800,
) -> int:
    """Run every spec as a subprocess; every outcome becomes a row. Returns the failure count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "timelines").mkdir(exist_ok=True)
    command = trial_command or (lambda s: default_command(s, run_id, session))
    failed = 0
    with (out_dir / "results.jsonl").open("a") as results:
        for index, spec in enumerate(specs):
            sampler = HostSampler()
            sampler.start()
            row: TrialRow | None = None
            reason, stderr_tail = None, ""
            try:
                proc = subprocess.run(command(spec), capture_output=True, text=True, timeout=timeout_s)
                stderr_tail = proc.stderr[-2000:]
                last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
                try:
                    row = TrialRow.from_json(last)
                except Exception:  # not JSON, or JSON that is not an object
                    reason = f"exit {proc.returncode}, unparseable output" if proc.returncode else "unparseable output"
                if row is not None and proc.returncode and row.status == "ok":
                    row.status = f"subprocess:exit {proc.returncode}"
            except subprocess.TimeoutExpired as error:
                reason = f"timeout after {timeout_s:.0f} s"
                stderr_tail = (error.stderr or b"")[-2000:].decode(errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")[-2000:]
            except Exception as error:  # e.g. OSError spawning the interpreter
                reason = f"{type(error).__name__}: {error}"
            finally:
                sampler.stop()
            if row is None:
                row = TrialRow(arm=spec.arm, workload=spec.case.workload, case_size=spec.case.case_size, window=spec.case.window,
                               repetition=spec.rep, cache_state=spec.cache_state, arm_settings=dict(spec.settings),
                               status=f"subprocess:{reason}", error=stderr_tail)
            row.run_id, row.session_id = run_id, session
            row.arm = row.arm or spec.arm
            if not row.arm_settings:
                row.arm_settings = dict(spec.settings)
            if row.repetition is None:
                row.repetition = spec.rep
            timeline = sampler.timeline()
            path = out_dir / "timelines" / f"{index}.json"
            path.write_text(json.dumps(timeline))
            row.timeline_path = str(path.relative_to(out_dir))
            if timeline:
                row.cpu_busy_pct_mean = sum(r["cpu_busy_pct_mean"] for r in timeline) / len(timeline)
                row.cpu_busy_pct_max_core = sum(r["cpu_busy_pct_max_core"] for r in timeline) / len(timeline)
                row.net_rx_peak_bytes_per_s = max(r["net_rx_bytes_per_s"] for r in timeline)
            if not (row.status or "").startswith("ok"):
                failed += 1
            results.write(row.to_json() + "\n")
            results.flush()
    return failed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.tempo_no2.orchestrate", description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--session", default="1")
    p.add_argument("--headline-only", action="store_true")
    p.add_argument("--estimate", action="store_true", help="print the projection and exit")
    p.add_argument("--yes", action="store_true", help="start the matrix after the estimate")
    p.add_argument("--instance-rate-usd-h", type=float, default=DEFAULT_INSTANCE_RATE)
    p.add_argument("--smoke-results", default="results/tempo_no2/smoke/results.jsonl")
    p.add_argument("--probes", default="results/tempo_no2/probes.json", help="probes.json from `run.py probe`; without it floor_seconds is null")
    p.add_argument("--out-root", default="results/tempo_no2")
    p.add_argument("--timeout-s", type=float, default=1800)
    p.add_argument("--access", choices=("s3", "https"), default="s3", help="s3: in us-west-2; https: from anywhere via the distribution endpoint")
    p.add_argument("--arms", type=parse_arms, default=DEFAULT_ARMS, help="comma-separated timed arms (default netcdf_tuned,virtual_icechunk; add netcdf_user_stack to include the user stack)")
    args = p.parse_args(argv)

    specs = plan(args.headline_only, arms=args.arms)
    smoke_path = Path(args.smoke_results)
    if not smoke_path.exists():
        print(f"no smoke results at {smoke_path}: run `run.py smoke` first", file=sys.stderr)
        return 2
    smoke_rows = [TrialRow.from_json(line) for line in smoke_path.read_text().splitlines() if line.strip()]
    n_obs = {"1d": 12, "1w": 84, "1m": 360}
    for window in n_obs:
        from benchmarks.tempo_no2.cases import manifest_path

        mp = manifest_path(window)
        if mp.exists():
            n_obs[window] = len(json.loads(mp.read_text())["granules"])
    spatial = None
    mp_1d = manifest_path("1d")
    if mp_1d.exists():
        from benchmarks.tempo_no2.cases import grid_arrays, prepare_spatial_cases

        payload = json.loads(mp_1d.read_text())
        if "grid" in payload:
            spatial = prepare_spatial_cases(*grid_arrays(payload["grid"]))
    e = estimate(specs, smoke_rows, n_obs, args.instance_rate_usd_h, spatial=spatial)
    print(format_estimate(e))
    if args.estimate or not args.yes:
        if not args.estimate:
            print("re-run with --yes to start")
        return 0 if args.estimate else 2
    out_dir = Path(args.out_root) / args.run_id
    probes = args.probes if args.probes and Path(args.probes).exists() else None
    if probes is None:
        print(f"warning: no probes file at {args.probes}; floor_seconds will be null (run `run.py probe` first)", file=sys.stderr)
    failed = run_matrix(specs, args.run_id, args.session, out_dir,
                        trial_command=lambda s: default_command(s, args.run_id, args.session, probes, args.access),
                        timeout_s=args.timeout_s)
    (Path(args.out_root) / "latest.json").write_text(json.dumps({"run_id": args.run_id}) + "\n")
    print(f"done: {len(specs)} trials, {failed} failed, rows in {out_dir / 'results.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
