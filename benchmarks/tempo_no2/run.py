"""CLI: discover | probe | gate | trial | catalog | smoke.

``trial`` runs one trial in this process and prints one JSON row as the
last line of stdout; the orchestrator spawns one of these per trial.
Everything that touches the network lives behind a subcommand; the
in-process pieces (:func:`run_trial`, the expected-count helpers) are
unit-tested on fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from benchmarks.tempo_no2 import backends, cases as cases_mod
from benchmarks.tempo_no2.backends import Backend, CredentialCache, SelectionResult
from benchmarks.tempo_no2.cases import CONFIG, Case, SpatialCases
from benchmarks.tempo_no2.counters import RetryLogCounter
from benchmarks.tempo_no2.measure import (
    METADATA_OBJECTS,
    TrialRow,
    cost_usd,
    cpu_steal_seconds,
    floor_seconds,
    host_info,
    resource_usage,
    virtual_minimum,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PATH = Path(__file__).parent / "expected" / "requests.json"
ARMS = ("netcdf_user_stack", "netcdf_tuned", "virtual_icechunk", "virtual_manifeststore", "noop")
SMOKE_CASES = (Case("point", 1, "1d"), Case("aoi", 50, "1d"))
PACKAGES = (
    "earthaccess", "earthaccess-auth", "icechunk", "xarray", "zarr", "h5py", "h5netcdf",
    "s3fs", "obstore", "obspec-utils", "virtualizarr", "numpy", "dask",
)
DEFAULT_INSTANCE_RATE = float(os.environ.get("BENCH_INSTANCE_RATE_USD_H", "0.192"))  # m6i.xlarge, 2026-11-02

_HOST: dict[str, Any] | None = None


def host() -> dict[str, Any]:
    global _HOST
    if _HOST is None:
        _HOST = host_info()
    return _HOST


# --- small helpers ---------------------------------------------------------------


def parse_case(s: str) -> Case:
    workload, size, window = s.split(":")
    if workload not in ("point", "aoi") or window not in cases_mod.WINDOWS:
        raise argparse.ArgumentTypeError(f"bad case {s!r}; want workload:size:window")
    return Case(workload, int(size), window)  # type: ignore[arg-type]


def write_expected(path: Path, counts: dict[tuple[str, str], int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({f"{arm}|{case}": n for (arm, case), n in sorted(counts.items())}, indent=2) + "\n")


def check_expected(path: Path, counts: dict[tuple[str, str], int]) -> list[tuple[str, str, int, int]]:
    expected = json.loads(path.read_text())
    diffs = []
    for (arm, case), observed in sorted(counts.items()):
        want = expected.get(f"{arm}|{case}")
        if want is not None and want != observed:
            diffs.append((arm, case, want, observed))
    return diffs


def provenance() -> dict[str, Any]:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        sha = None
    lock = REPO_ROOT / "uv.lock"
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return {
        "git_sha": sha,
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "packages": packages,
    }


class _Noop:
    name = "noop"
    settings: dict[str, Any] = {}

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult:
        return SelectionResult(np.array([], dtype="datetime64[ns]"), np.zeros((0, 0)), np.zeros((0, 0)))

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult:
        return self.point(observations, pd.DataFrame())

    def counts(self) -> dict[str, Any] | None:
        return {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}}


def noop_factory() -> Backend:
    return _Noop()


def smoke_budget_s(access: str) -> int:
    """Smoke must stay quick in region; over HTTPS every request costs a redirect
    through the distribution service, so the budget is wider."""
    return 900 if access == "https" else 120


def serial_phases(arm: str, request_count: int, n_obs: int) -> int:
    """Dependent round trips on the critical path, for the floor model.

    Virtual: the metadata objects, then one round of concurrent chunk reads.
    File arms: granules are read concurrently, so the chain is one granule's
    requests (mean requests per granule). Documented in the protocol doc.
    """
    if arm.startswith("virtual"):
        return METADATA_OBJECTS + 1
    return max(1, round(request_count / max(1, n_obs)))


# --- the trial ------------------------------------------------------------------


def run_trial(
    backend_factory: Callable[[], Backend],
    case: Case,
    observations: list[int],
    spatial: SpatialCases,
    rep: int,
    cache_state: str,
    *,
    probes: dict[str, Any] | None = None,
    instance_rate_usd_h: float = DEFAULT_INSTANCE_RATE,
    backend: Backend | None = None,
    arm: str | None = None,
) -> TrialRow:
    """One timed trial. ``backend`` reuses an object (warm_process); else the factory builds one.
    ``arm`` names the row even when the factory raises before a backend exists."""
    row = TrialRow(
        arm=arm, workload=case.workload, case_size=case.case_size, window=case.window,
        observation_count=len(observations), repetition=rep, cache_state=cache_state,
        cache_drop="not attempted",
    )
    h = host()
    row.host_kind, row.instance_type, row.instance_type_reason = h["host_kind"], h["instance_type"], h["instance_type_reason"]
    row.availability_zone, row.cpu_model, row.nproc, row.mem_total_bytes = h["availability_zone"], h["cpu_model"], h["nproc"], h["mem_total_bytes"]
    load_before, steal_before = os.getloadavg()[0], cpu_steal_seconds()
    net_before = _net_rx() if h["host_kind"] in ("ec2", "jupyterhub") else None
    retry_log = RetryLogCounter()
    retry_log.attach()
    total_start = time.perf_counter()
    try:
        with resource_usage() as usage:
            open_start = time.perf_counter()
            b = backend if backend is not None else backend_factory()
            row.open_seconds = time.perf_counter() - open_start
            row.arm, row.arm_settings = b.name, dict(b.settings)
            retrieval_start = time.perf_counter()
            if case.workload == "point":
                point_rows = spatial.points.iloc[: case.case_size]
                selection = b.point(observations, point_rows)
                lat_sel, lon_sel = point_rows["lat_index"].to_numpy(), point_rows["lon_index"].to_numpy()
            else:
                window = spatial.windows[case.case_size]
                selection = b.aoi(observations, window)
                lat_sel, lon_sel = window
            row.retrieval_seconds = time.perf_counter() - retrieval_start
            processing_start = time.perf_counter()
            _ = backends.quality_mask(selection)
            row.processing_seconds = time.perf_counter() - processing_start
        row.end_to_end_seconds = time.perf_counter() - total_start
        row.discovery_plus_open_seconds = row.open_seconds
        row.rendering_seconds = 0.0
        row.cpu_user_s, row.cpu_sys_s, row.peak_rss_bytes = usage.cpu_user_s, usage.cpu_sys_s, usage.peak_rss_bytes
        counts = b.counts()
        if b.name == "noop":
            row.request_count, row.bytes_requested, row.request_count_reason = 0, 0, "noop"
        elif counts is None and hasattr(b, "static_minimum"):
            minimum = b.static_minimum(observations, lat_sel, lon_sel)
            row.request_count, row.bytes_requested = minimum["request_count"], minimum["bytes_requested"]
            row.request_count_reason = "static minimum; not instrumented"
        elif counts is None and b.name.startswith("virtual"):
            row.request_count, row.bytes_requested = virtual_minimum(observations, lat_sel, lon_sel)
            row.request_count_reason = "static minimum (pinned grid); not instrumented"
        elif counts is None:
            row.request_count_reason = f"not instrumented ({b.name} over this transport)"
        else:
            row.request_count, row.bytes_requested = counts["request_count"], counts["bytes_requested"]
            row.requests_by_method = counts.get("requests_by_method", {})
            row.request_count_reason = counts.get("kind", "client-ranges")
            if "retries" in counts:
                row.retries = counts["retries"]
        if row.retries is None:
            if retry_log.seen_any:
                row.retries = retry_log.count
            else:
                row.retries_reason = "library does not log retries"
        if probes and probes.get("rtt_ms_median") and probes.get("bandwidth_mb_s"):
            row.rtt_ms_median, row.bandwidth_mb_s = probes["rtt_ms_median"], probes["bandwidth_mb_s"]
            row.floor_seconds = floor_seconds(
                serial_phases(b.name, row.request_count or 0, len(observations)),
                probes["rtt_ms_median"] / 1000, row.bytes_requested or 0, probes["bandwidth_mb_s"] * 1e6,
            )
        row.cost_usd_estimate = cost_usd(row.request_count or 0, row.end_to_end_seconds, instance_rate_usd_h)
        row.status = "ok"
    except Exception as error:
        row.end_to_end_seconds = time.perf_counter() - total_start
        row.status = f"{type(error).__name__}: {error}"
        row.error = row.status[:2000]
    finally:
        retry_log.detach()
    row.load_avg_1m = (load_before + os.getloadavg()[0]) / 2
    steal_after = cpu_steal_seconds()
    if steal_before is None or steal_after is None:
        row.cpu_steal_s_reason = "no /proc/stat on this platform"
    else:
        row.cpu_steal_s = steal_after - steal_before
    if net_before is not None:
        row.net_rx_bytes = _net_rx() - net_before
    else:
        row.net_rx_bytes_reason = "shared host"
    return row


def prepare_backend(
    backend_factory: Callable[[], Backend], case: Case, observations: list[int], spatial: SpatialCases, cache_state: str
) -> Backend | None:
    """For ``warm_process``: build the backend and run one discarded selection so the
    timed trial sees warm caches, pools and connections. ``cold`` gets nothing."""
    if cache_state != "warm_process":
        return None
    backend = backend_factory()
    if case.workload == "point":
        backend.point(observations, spatial.points.iloc[: case.case_size])
    else:
        backend.aoi(observations, spatial.windows[case.case_size])
    return backend


def _net_rx() -> int:
    import psutil

    return psutil.net_io_counters().bytes_recv


# --- building arms from a manifest ----------------------------------------------


def load_manifest(window: str) -> dict[str, Any]:
    path = cases_mod.manifest_path(window)
    if not path.exists():
        raise SystemExit(f"{path} missing: run `run.py discover --window {window}` in us-west-2 first")
    return json.loads(path.read_text())


def spatial_from_dataset(dataset: Any) -> SpatialCases:
    return cases_mod.prepare_spatial_cases(np.asarray(dataset["latitude"].values), np.asarray(dataset["longitude"].values))


class ArmFactory:
    """Builds backends for the arms from a manifest. ``access`` is "s3" (in
    us-west-2) or "https" (NASA's distribution endpoint, from anywhere)."""

    def __init__(self, payload: dict[str, Any], settings: dict[str, Any] | None = None, access: str = "s3") -> None:
        self.payload = payload
        self.manifest = pd.DataFrame(payload["granules"])
        self.settings = settings or {}
        self.access = access
        self.credentials = CredentialCache() if access == "s3" else None
        self._client_options: dict[str, Any] | None = None
        self._repo: Any = None

    def client_options(self) -> dict[str, Any] | None:
        if self.access == "https" and self._client_options is None:
            self._client_options = backends._https_client_options()
        return self._client_options

    def source_urls(self) -> list[str]:
        urls = self.manifest["source_url"].tolist()
        return [backends.https_url(u) for u in urls] if self.access == "https" else urls

    def store_spec(self) -> dict[str, Any]:
        if self.access == "https":
            return backends.source_store_spec(None, "https", self.client_options())
        return backends.source_store_spec(self.credentials.get())

    @property
    def repo(self) -> Any:
        if self._repo is None:
            self._repo = backends.open_mirror_repo(self.access)
        return self._repo

    def session(self) -> Any:
        return backends.pinned_session(self.repo, self.payload["snapshot_id"])

    def factory(self, arm: str) -> Callable[[], Backend]:
        """Every factory opens its own store and repository inside the timed region,
        so no arm starts a 'cold' trial on a repository this process already warmed."""
        urls = self.source_urls()
        snapshot = self.payload["snapshot_id"]
        access = self.access
        if arm == "noop":
            return noop_factory
        if arm == "netcdf_user_stack":
            return lambda: backends.NetcdfUserStack(urls, backends._earthaccess_filesystem(access))
        if arm == "netcdf_tuned":
            return lambda: backends.NetcdfTuned(urls, self.store_spec(), **self.settings)
        if arm == "virtual_icechunk":
            return lambda: backends.VirtualIcechunk(backends.pinned_session(backends.open_mirror_repo(access), snapshot).store, self.manifest)
        if arm == "virtual_manifeststore":

            def manifeststore() -> Backend:
                repo = backends.open_mirror_repo(access)
                stores = backends.container_stores(repo, self.credentials.get() if self.credentials else None, self.client_options())
                stores[f"s3://{CONFIG.mirror_bucket}"] = backends.build_store({"kind": "s3", "bucket": CONFIG.mirror_bucket})
                return backends.VirtualManifestStore(
                    backends.pinned_session(repo, snapshot), stores,
                    f"s3://{CONFIG.mirror_bucket}/{CONFIG.mirror_prefix}/chunks", self.manifest,
                )

            return manifeststore
        raise SystemExit(f"unknown arm {arm!r}; one of {ARMS}")

    def spatial(self) -> SpatialCases:
        """Point and window indices from the grid recorded at discover time, so
        trials need no store access before timing starts."""
        if "grid" not in self.payload:
            raise SystemExit("manifest has no grid: re-run `run.py discover`")
        latitudes, longitudes = cases_mod.grid_arrays(self.payload["grid"])
        return cases_mod.prepare_spatial_cases(latitudes, longitudes)


# --- subcommands --------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    frame = cases_mod.discover(args.window)
    repo = backends.open_mirror_repo()
    snapshot_id, tag = backends.resolve_pin(repo)
    dataset = backends.VirtualIcechunk(backends.pinned_session(repo, snapshot_id).store, frame).dataset
    grid = cases_mod.grid_spec(np.asarray(dataset["latitude"].values), np.asarray(dataset["longitude"].values))
    if tag is None:
        print(f"warning: tag {CONFIG.snapshot_tag} not found on mirror; pinning main tip {snapshot_id}", file=sys.stderr)
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "window": args.window,
        "config": CONFIG.__dict__,
        "environment": {"python": sys.version.split()[0], **provenance()},
        "snapshot_id": snapshot_id,
        "snapshot_tag": tag,
        "icechunk_uri": f"s3://{CONFIG.mirror_bucket}/{CONFIG.mirror_prefix}",
        "grid": grid,
        "granules": frame.to_dict(orient="records"),
    }
    path = cases_mod.manifest_path(args.window)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"wrote {path} ({len(frame)} granules, snapshot {snapshot_id}, tag {tag})")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    import h5py
    from obspec_utils.readers import BlockStoreReader
    from obstore.store import S3Store

    from benchmarks.tempo_no2.counters import probe_network, probe_paging

    payload = load_manifest(args.window)
    arms = ArmFactory(payload, access=args.access)
    store = backends.build_store(arms.store_spec())
    key = payload["granules"][0]["source_url"].split(f"{backends.SOURCE_PREFIX}/", 1)[1]
    path = f"{backends.SOURCE_BUCKET}/{key}" if args.access == "https" else key
    source = probe_network(store, path)
    mirror_store = S3Store(CONFIG.mirror_bucket, region=CONFIG.region, skip_signature=True)
    # RTT only, against whatever object the repository prefix lists first: the
    # Icechunk layout under the prefix is not something to guess.
    mirror = probe_network(mirror_store, None, range_reads=0, prefix=CONFIG.mirror_prefix)
    with BlockStoreReader(store, path) as reader, h5py.File(reader, "r") as f:
        paged = probe_paging(f)
    out = {
        "created_at": datetime.now(UTC).isoformat(),
        "rtt_ms_median": source["rtt_ms_median"],
        "bandwidth_mb_s": source["bandwidth_mb_s"],
        "mirror_rtt_ms_median": mirror["rtt_ms_median"],
        "source_paged": paged,
        "access": args.access,
        "host": host(),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    from benchmarks.tempo_no2.gate import gate_passed, run_gate

    payload = load_manifest(args.window)
    arms = ArmFactory(payload, access=args.access)
    spatial = arms.spatial()
    virtual = arms.factory("virtual_icechunk")()
    baselines = [arms.factory(a)() for a in ("netcdf_user_stack", "netcdf_tuned")]
    cases = [c for c in cases_mod.matrix(headline_only=False) if c.window == args.window]
    table = run_gate(baselines, virtual, cases, spatial, len(payload["granules"]))
    print(table.to_string(index=False))
    print(f"max time offset: {virtual.assert_one_to_one():.1f} s")
    ok = gate_passed(table)
    print("GATE PASSED" if ok else "GATE FAILED")
    return 0 if ok else 1


def cmd_trial(args: argparse.Namespace) -> int:
    payload = load_manifest(args.case.window)
    arms = ArmFactory(payload, json.loads(args.settings) if args.settings else None, access=args.access)
    spatial = arms.spatial()
    probes = json.loads(Path(args.probes).read_text()) if args.probes else None
    observations = list(range(len(payload["granules"])))
    factory = arms.factory(args.arm)
    warmed = prepare_backend(factory, args.case, observations, spatial, args.cache)
    row = run_trial(factory, args.case, observations, spatial, args.rep, args.cache, probes=probes, backend=warmed, arm=args.arm)
    row.access = args.access
    _stamp(row, payload, args.run_id, args.session)
    print(row.to_json())
    return 0 if row.status == "ok" else 1


def _stamp(row: TrialRow, payload: dict[str, Any], run_id: str, session: str) -> None:
    prov = provenance()
    row.run_id, row.session_id = run_id, session
    row.git_sha, row.uv_lock_sha256, row.packages = prov["git_sha"], prov["uv_lock_sha256"], prov["packages"]
    row.manifest_path = str(cases_mod.manifest_path(payload["window"]).relative_to(REPO_ROOT))
    row.snapshot_id, row.snapshot_tag = payload.get("snapshot_id"), payload.get("snapshot_tag")


def cmd_catalog(args: argparse.Namespace) -> int:
    import earthaccess

    earthaccess.login(strategy="all", persist=True)
    payload = load_manifest(args.window) if cases_mod.manifest_path(args.window).exists() else {"window": args.window}
    for rep in range(args.rep):
        t0 = time.perf_counter()
        results = earthaccess.search_data(concept_id=CONFIG.concept_id, temporal=cases_mod.WINDOWS[args.window], count=-1)
        row = TrialRow(arm="catalog", workload="point", case_size=1, window=args.window, repetition=rep, cache_state="cold",
                       observation_count=len(results), catalog_seconds=time.perf_counter() - t0, status="ok")
        _stamp(row, payload, args.run_id, args.session)
        print(row.to_json())
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    payload = load_manifest("1d")
    arms = ArmFactory(payload, access=args.access)
    spatial = arms.spatial()
    counts: dict[tuple[str, str], int] = {}
    rows: list[TrialRow] = []
    t0 = time.perf_counter()
    for case in SMOKE_CASES:
        for arm in ARMS:
            for rep in range(2):
                row = run_trial(arms.factory(arm), case, [0], spatial, rep, "cold", arm=arm)
                row.access = args.access
                _stamp(row, payload, args.run_id, "smoke")
                rows.append(row)
                print(row.to_json())
                if row.status == "ok" and row.request_count is not None and arm != "noop":
                    counts[(arm, str(case))] = row.request_count
    elapsed = time.perf_counter() - t0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.writelines(r.to_json() + "\n" for r in rows)
    failed = [r for r in rows if r.status != "ok"]
    print(f"smoke: {len(rows)} trials in {elapsed:.0f} s, {len(failed)} failed, rows appended to {out}")
    if failed:
        for r in failed:
            print(f"  {r.arm} {r.workload}:{r.case_size}: {r.status}")
        return 1
    budget = smoke_budget_s(args.access)
    if elapsed > budget:
        print(f"smoke: over the {budget} s budget for --access {args.access}")
        return 1
    expected = EXPECTED_PATH if args.access == "s3" else EXPECTED_PATH.with_name("requests-https.json")
    if not expected.exists():
        write_expected(expected, counts)
        print(f"recorded expected request counts in {expected}")
        return 0
    diffs = check_expected(expected, counts)
    for arm, case, want, got in diffs:
        print(f"request count changed: {arm} {case}: expected {want}, observed {got}")
    return 1 if diffs else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.tempo_no2.run", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("discover", help="CMR discovery → committed manifest (network)")
    s.add_argument("--window", choices=list(cases_mod.WINDOWS), default="1d")
    s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("probe", help="RTT, bandwidth and HDF5 paging probes for this session (network)")
    s.add_argument("--window", choices=list(cases_mod.WINDOWS), default="1d")
    s.add_argument("--out", default="results/tempo_no2/probes.json")
    s.add_argument("--access", choices=("s3", "https"), default="s3", help="s3: in us-west-2; https: NASA distribution endpoint, from anywhere")
    s.set_defaults(fn=cmd_probe)
    s = sub.add_parser("gate", help="exact-equality gate between file arms and the virtual arm (network)")
    s.add_argument("--window", choices=list(cases_mod.WINDOWS), default="1d")
    s.add_argument("--access", choices=("s3", "https"), default="s3", help="s3: in us-west-2; https: NASA distribution endpoint, from anywhere")
    s.set_defaults(fn=cmd_gate)
    s = sub.add_parser("trial", help="one trial in this process; prints one JSON row (network)")
    s.add_argument("--arm", choices=ARMS, required=True)
    s.add_argument("--case", type=parse_case, required=True, help="workload:size:window, e.g. point:10:1d")
    s.add_argument("--rep", type=int, default=0)
    s.add_argument("--cache", choices=("cold", "warm_process"), default="cold")
    s.add_argument("--run-id", required=True)
    s.add_argument("--session", default="1")
    s.add_argument("--probes", help="probes.json from `probe`")
    s.add_argument("--settings", help='JSON of arm settings, e.g. \'{"max_workers": 1}\'')
    s.add_argument("--access", choices=("s3", "https"), default="s3", help="s3: in us-west-2; https: NASA distribution endpoint, from anywhere")
    s.set_defaults(fn=cmd_trial)
    s = sub.add_parser("catalog", help="time the CMR search for a window (network)")
    s.add_argument("--window", choices=list(cases_mod.WINDOWS), default="1d")
    s.add_argument("--rep", type=int, default=5)
    s.add_argument("--run-id", required=True)
    s.add_argument("--session", default="1")
    s.set_defaults(fn=cmd_catalog)
    s = sub.add_parser("smoke", help="1 observation × smallest cases × all arms × n=2; request-count tripwire (network)")
    s.add_argument("--run-id", default="smoke")
    s.add_argument("--out", default="results/tempo_no2/smoke/results.jsonl")
    s.add_argument("--access", choices=("s3", "https"), default="s3", help="s3: in us-west-2; https: NASA distribution endpoint, from anywhere")
    s.set_defaults(fn=cmd_smoke)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
