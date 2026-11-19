"""CLI: status | discover | build-store | probe | gate | trial | session | report | superchunks.

Every network subcommand takes ``--transport``: ``s3`` (direct, from the data's
region) or ``https`` (the DAAC's distribution endpoint with an Earthdata
token, from anywhere). Results are filed per transport and never pooled.

``trial`` runs one trial in this process and prints one JSON row as the last
line of stdout; ``session`` spawns one fresh process per trial, in shuffled
order. The GOES-19 dataset needs no credentials, so every step can be rerun
by anyone.

This file only parses arguments and writes files; the work is in the module
each subcommand names.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.trade_study import paths, superchunks
from benchmarks.trade_study.datasets import DATASETS, Dataset, get_dataset, timed_discovery
from benchmarks.trade_study.gate import GATE_RTOL, PASSING, gate_key, gate_verdicts, make_arm, run_gate
from benchmarks.trade_study.host import REPO_ROOT, client_region, host_info, provenance
from benchmarks.trade_study.paths import load_manifest, write_json
from benchmarks.trade_study.probes import concurrent_rate, probe_network
from benchmarks.trade_study.questions import Case, headline_cases, parse_case
from benchmarks.trade_study.scope import render_status
from benchmarks.trade_study.session import SessionOptions, plan, run_session
from benchmarks.trade_study.stores import build_store, store_path
from benchmarks.trade_study.trial import DEFAULT_INSTANCE_RATE_USD_H, MEASURES, Row, run_trial

DEFAULT_WINDOW = "1h"
DEFAULT_ARMS = ("user_stack", "tuned_h5py", "download", "virtual_icechunk")
GATE_ARMS = ("tuned_h5py", "user_stack", "download", "virtual_icechunk")  # the first is the reference
INSTANCE_RATE = float(os.environ.get("BENCH_INSTANCE_RATE_USD_H", DEFAULT_INSTANCE_RATE_USD_H))


def now() -> str:
    return datetime.now(UTC).isoformat()


def is_in_region(dataset: Dataset, region: str | None) -> bool:
    """A client is in region when it sits in the data's AWS region and reads S3 directly."""
    return region == dataset.region and dataset.transport == "s3"


def parse_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_variant(text: str) -> tuple[str, dict[str, Any]]:
    """``arm=JSON`` → (arm, its settings)."""
    arm, separator, settings = text.partition("=")
    try:
        parsed = json.loads(settings) if separator else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict) or not parsed:
        raise argparse.ArgumentTypeError(
            f"bad variant {text!r}: expected arm=JSON, e.g. 'tuned_h5py={{\"max_workers\": 1}}'"
        )
    return arm.strip(), parsed


def resolve_window(args: argparse.Namespace) -> str:
    """The one window a gate or a session is about: ``--window``, or the window
    its ``--case`` values name. They must agree, because the manifest and the
    gate file are per window."""
    named = sorted({case.window for case in args.case or []})
    if len(named) > 1:
        raise SystemExit(f"the cases name more than one window ({', '.join(named)}); run one window at a time")
    if named and args.window and args.window != named[0]:
        raise SystemExit(f"--window {args.window} but the cases are over {named[0]}; drop one or make them agree")
    return named[0] if named else args.window or DEFAULT_WINDOW


# --- pinning what a run reads -----------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    print(render_status())
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    dataset = get_dataset(args.dataset, args.transport)
    granules, seconds = timed_discovery(dataset, args.window)
    grid = dataset.describe_grid(granules)
    payload = {
        "created_at": now(),
        "dataset": dataset.name,
        "window": args.window,
        "discovery_kind": dataset.discovery_kind,
        "discovery_seconds": seconds,
        "environment": {"python": sys.version.split()[0], **provenance()},
        "snapshot_id": grid.pop("snapshot_id", None),
        "index_location": dataset.index_location,
        "grid": grid,
        "granules": granules,
    }
    path = dataset.manifest_path(args.window)
    write_json(path, payload)
    print(f"wrote {path}: {len(granules)} granules, discovery ({dataset.discovery_kind}) {seconds:.2f} s")
    return 0


def cmd_build_store(args: argparse.Namespace) -> int:
    dataset = get_dataset(args.dataset, args.transport)
    payload = load_manifest(dataset, args.window)
    start = time.perf_counter()
    try:
        snapshot, where = dataset.build_virtual_store(payload, args.source)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    seconds = time.perf_counter() - start
    if args.source == "dmrpp":  # one DMR++ store per transport, beside the HDF5-built one
        payload.setdefault("dmrpp_snapshot_ids", {})[dataset.transport] = snapshot
        payload.setdefault("dmrpp_store_build_seconds", {})[dataset.transport] = seconds
    else:
        payload["snapshot_id"], payload["store_build_seconds"] = snapshot, seconds
    write_json(dataset.manifest_path(args.window), payload)
    print(f"built {where} from {args.source} in {seconds:.0f} s; snapshot {snapshot} pinned in the manifest")
    return 0


# --- before a session: the link, and whether the arms agree -----------------------


def cmd_probe(args: argparse.Namespace) -> int:
    dataset = get_dataset(args.dataset, args.transport)
    payload = load_manifest(dataset, args.window)
    spec = dataset.store_spec(dataset.auth())
    path = store_path(spec, payload["granules"][0]["source_url"])
    range_bytes = (args.range_mib or dataset.probe_range_mib) * 2**20
    network = probe_network(build_store(spec), path, range_reads=args.range_reads, range_bytes=range_bytes)
    # A floor needs the fastest rate the link has shown, and arms read with many
    # streams: also read with 16 ranges in flight, and keep the best rate seen.
    concurrent = max(concurrent_rate(build_store(spec), path) for _ in range(3))
    single_stream = network["bandwidth_mb_s"]  # None when asked for no range reads
    host = host_info()
    region = client_region(host)
    out = {
        "created_at": now(),
        "dataset": dataset.name,
        "transport": dataset.transport,
        **network,
        "bandwidth_single_stream_mb_s": single_stream,
        "bandwidth_concurrent_mb_s": concurrent,
        "bandwidth_mb_s": concurrent if single_stream is None else max(single_stream, concurrent),
        "host": host,
        "client_region": region,
        "data_region": dataset.region,
        "in_region": is_in_region(dataset, region),
    }
    write_json(Path(args.out) if args.out else paths.probes_path(dataset), out)
    print(json.dumps(out, indent=2))
    return 0


def gate_cases(window: str) -> list[Case]:
    """The headline cases the gate reads: all but the batch and the larger
    pancakes, which read what ``pancake:1`` reads over more observations."""
    return [
        case
        for case in headline_cases(window)
        if case.workload != "batch" and not (case.workload == "pancake" and case.size != 1)
    ]


def cmd_gate(args: argparse.Namespace) -> int:
    dataset = get_dataset(args.dataset, args.transport)
    window = resolve_window(args)
    payload = load_manifest(dataset, window)
    rows = run_gate(dataset, payload, args.arms, args.case or gate_cases(window), args.variants)
    verdicts = gate_verdicts(rows, args.arms[0])
    # A gate of some arms or variants adds to the window's file: arms it did not
    # read keep the verdicts, and the rows behind them, that an earlier gate wrote.
    path = paths.gate_path(dataset, window)
    earlier = json.loads(path.read_text()) if path.exists() else {"verdicts": {}, "rows": []}
    kept_rows = [row for row in earlier["rows"] if row["arm"] not in verdicts]
    kept_verdicts = {arm: verdict for arm, verdict in earlier["verdicts"].items() if arm not in verdicts}
    write_json(
        path,
        {
            "created_at": now(),
            "rtol": GATE_RTOL,
            "verdicts": {**kept_verdicts, **verdicts},
            "rows": [*kept_rows, *rows],
        },
    )
    for arm, verdict in verdicts.items():
        print(f"{arm}: {verdict}")
    passed = all(verdict in PASSING for verdict in verdicts.values())
    print("GATE PASSED" if passed else "GATE FAILED")
    return 0 if passed else 1


def read_verdicts(dataset: Dataset, window: str) -> dict[str, str]:
    path = paths.gate_path(dataset, window)
    return json.loads(path.read_text())["verdicts"] if path.exists() else {}


# --- timing -----------------------------------------------------------------------


def stamp(row: Row, dataset: Dataset, window: str, run_id: str, session: str, payload: dict[str, Any]) -> None:
    """Fill in what the trial itself does not know: the run, the code, the host and the gate verdict."""
    code, host = provenance(), host_info()
    row.run_id, row.session_id = run_id, session
    row.git_sha, row.uv_lock_sha256, row.packages = code["git_sha"], code["uv_lock_sha256"], code["packages"]
    manifest = dataset.manifest_path(window).resolve()
    row.manifest_path = str(manifest.relative_to(REPO_ROOT) if manifest.is_relative_to(REPO_ROOT) else manifest)
    row.host_kind, row.instance_type = host["host_kind"], host["instance_type"]
    row.availability_zone, row.cpu_model, row.nproc = host["availability_zone"], host["cpu_model"], host["nproc"]
    row.client_region = client_region(host)
    row.transport = dataset.transport
    if row.arm == "virtual_dmrpp":  # its store is the locally built one, not the manifest's main snapshot
        row.snapshot_id = (payload.get("dmrpp_snapshot_ids") or {}).get(dataset.transport)
        row.index_location = "local-disk"
    # a variant is gated under its own settings, never on its arm's verdict
    row.gate_status = read_verdicts(dataset, window).get(gate_key(row.arm or "", row.arm_variant), "not gated")


def cmd_trial(args: argparse.Namespace) -> int:
    import psutil

    startup = time.time() - psutil.Process().create_time()
    dataset = get_dataset(args.dataset, args.transport)
    payload = load_manifest(dataset, args.case.window)
    row = run_trial(
        make_arm(args.arm, dataset, payload, json.loads(args.settings) if args.settings else None),
        dataset,
        args.case,
        args.measure,
        rep=args.rep,
        probes=json.loads(Path(args.probes).read_text()) if args.probes else None,
        instance_rate_usd_h=args.instance_rate_usd_h,
        warm=not args.no_warm,
        in_region=is_in_region(dataset, client_region(host_info())),
    )
    row.startup_seconds = startup
    stamp(row, dataset, args.case.window, args.run_id, args.session, payload)
    print(row.to_json())
    return 0 if row.status == "ok" else 1


def cmd_session(args: argparse.Namespace) -> int:
    dataset = get_dataset(args.dataset, args.transport)
    window = resolve_window(args)
    payload = load_manifest(dataset, window)
    for arm in args.arms:
        make_arm(arm, dataset, payload)  # refuses arms that are not built
    cases = args.case or headline_cases(window)
    variants = args.variants
    unplanned = [arm for arm, _ in variants if arm not in args.arms]
    if unplanned:
        raise SystemExit(f"variants of arms that are not in --arms: {unplanned}")
    specs = plan(args.arms, cases, args.reps, variants, measures=args.measures)
    print(
        f"{len(specs)} trials: {len(args.arms)} arms × {len(cases)} cases × {len(args.measures)} measures "
        f"× {args.reps} repetitions{' + variants' if variants else ''}, each a fresh process, "
        f"over {len(payload['granules'])} pinned granules"
    )
    verdicts = read_verdicts(dataset, window)
    planned = [*args.arms, *(gate_key(arm, settings) for arm, settings in variants)]
    ungated = [key for key in planned if verdicts.get(key) not in PASSING]
    if ungated and not args.allow_ungated:
        hint = f"run.py gate --dataset {dataset.name} --window {window}"
        if variants:
            hint += " --variants " + " --variants ".join(f"'{arm}={json.dumps(s)}'" for arm, s in variants)
        print(f"not gated: {ungated}. Run `{hint}` first, or pass --allow-ungated.", file=sys.stderr)
        return 2
    if not args.yes:
        print("re-run with --yes to start")
        return 2
    probes = args.probes or str(paths.probes_path(dataset))
    if not Path(probes).exists():
        print(f"warning: no probes at {probes}; floor_seconds will be null (run `run.py probe` first)", file=sys.stderr)
        probes = None
    options = SessionOptions(args.run_id, args.session, args.instance_rate_usd_h, args.timeout_s, probes)
    out_dir = paths.RESULTS / args.run_id
    ran, failed = run_session(dataset, specs, options, out_dir)
    skipped = f", {len(specs) - ran} already done and skipped" if ran < len(specs) else ""
    print(f"done: {ran} trials, {failed} failed{skipped}, rows in {out_dir / 'results.jsonl'}")
    return 0


# --- after a session --------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    from benchmarks.trade_study.report import write_report

    print(write_report(Path(args.run_dir), figures=not args.no_figures))
    return 0


def cmd_superchunks(args: argparse.Namespace) -> int:
    dataset = get_dataset(args.dataset, args.transport)
    payload = load_manifest(dataset, args.window)
    probes_file = paths.probes_path(dataset)
    probes = json.loads(probes_file.read_text()) if probes_file.exists() else None
    max_bytes = args.max_mib * 2**20
    rows = superchunks.analyse_dataset(dataset, payload, args.window, probes, max_bytes)
    target = paths.superchunks_path(dataset, args.window)
    write_json(
        target,
        {
            "created_at": now(),
            "dataset": dataset.name,
            "window": args.window,
            "max_bytes": max_bytes,
            "layout_from": payload["granules"][0]["granule_ur"],
            "assumption": "every granule has the first granule's chunk layout",
            "probes": {key: probes[key] for key in ("rtt_ms_median", "bandwidth_mb_s")} if probes else None,
            "rows": rows,
        },
    )
    print(superchunks.render_table(rows))
    print(f"\nwrote {target}")
    return 0


# --- arguments --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.trade_study.run", description=__doc__)
    commands = parser.add_subparsers(dest="cmd", required=True)

    def add(
        name: str, run: Any, help_: str, dataset: bool = True, window: bool = True, window_from_cases: bool = False
    ) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_)
        command.set_defaults(run=run)
        if dataset:
            command.add_argument("--dataset", choices=sorted(DATASETS), default="goes19")
            command.add_argument(
                "--transport",
                choices=("s3", "https"),
                default="s3",
                help="s3: direct, from the data's region; https: the DAAC's distribution endpoint "
                "with an Earthdata token, from anywhere",
            )
        if dataset and window:
            if window_from_cases:
                help_window = (
                    f"1h (GOES-19 only), 1d, 1w or 1m; default: the --case values' window, else {DEFAULT_WINDOW}"
                )
                command.add_argument("--window", help=help_window)
            else:
                command.add_argument("--window", default=DEFAULT_WINDOW, help="1h (GOES-19 only), 1d, 1w or 1m")
        return command

    add("status", cmd_status, "the scope tables with each row's real status", dataset=False)
    add("discover", cmd_discover, "find and pin the window's granules; times the search (network)")

    c = add(
        "build-store",
        cmd_build_store,
        "write virtual references for the pinned granules to a local Icechunk store (network)",
    )
    c.add_argument(
        "--source",
        choices=("hdf5", "dmrpp"),
        default="hdf5",
        help="build from the files' HDF5 metadata, or from OPeNDAP's DMR++ sidecars",
    )

    c = add("probe", cmd_probe, "round-trip time and bandwidth to the source bucket (network)")
    c.add_argument("--out")
    c.add_argument("--range-reads", type=int, default=5)
    c.add_argument(
        "--range-mib", type=int, help="size of each bandwidth read; five must fit in one granule (default per dataset)"
    )

    c = add("gate", cmd_gate, "every arm must return the first arm's values (network)", window_from_cases=True)
    c.add_argument("--arms", type=parse_list, default=GATE_ARMS, help="comma-separated; the first is the reference")
    c.add_argument("--case", type=parse_case, action="append")
    c.add_argument(
        "--variants",
        type=parse_variant,
        action="append",
        default=[],
        help="arm=JSON, gated as an arm of its own, e.g. 'virtual_icechunk={\"zarrs_pipeline\": true}'",
    )

    c = add("trial", cmd_trial, "one trial in this process; prints one JSON row (network)", window=False)
    c.add_argument("--arm", required=True)
    c.add_argument("--case", type=parse_case, required=True, help="workload:size:window, e.g. churro:10:1h")
    c.add_argument("--measure", choices=MEASURES, default="full")
    c.add_argument("--rep", type=int, default=0)
    c.add_argument("--run-id", default="adhoc")
    c.add_argument("--session", default="1")
    c.add_argument("--probes")
    c.add_argument("--settings", help="JSON of arm settings, e.g. '{\"max_workers\": 1}'")
    c.add_argument("--no-warm", action="store_true", help="skip the warm second query")
    c.add_argument("--instance-rate-usd-h", type=float, default=INSTANCE_RATE)

    c = add(
        "session",
        cmd_session,
        "the matrix, one fresh process per trial, shuffled; re-running a run id resumes it (network)",
        window_from_cases=True,
    )
    c.add_argument("--run-id", required=True)
    c.add_argument("--session", default="1")
    c.add_argument("--arms", type=parse_list, default=DEFAULT_ARMS)
    c.add_argument(
        "--case", type=parse_case, action="append", help="repeatable; default: the headline cases for the window"
    )
    c.add_argument("--reps", type=int, default=10)
    c.add_argument(
        "--measures", type=parse_list, default=MEASURES, help="comma-separated: first_byte,full (default both)"
    )
    c.add_argument(
        "--variants",
        type=parse_variant,
        action="append",
        default=[],
        help="arm=JSON, run on the box case, e.g. 'tuned_h5py={\"max_workers\": 1}'",
    )
    c.add_argument("--probes")
    c.add_argument("--timeout-s", type=float, default=1800)
    c.add_argument("--instance-rate-usd-h", type=float, default=INSTANCE_RATE)
    c.add_argument("--allow-ungated", action="store_true")
    c.add_argument("--yes", action="store_true", help="start after printing the plan")

    c = add(
        "report", cmd_report, "medians, paired speedups and figures → summary.json, summary.md, figures/", dataset=False
    )
    c.add_argument("run_dir")
    c.add_argument("--no-figures", action="store_true", help="write the tables only")

    c = add(
        "superchunks",
        cmd_superchunks,
        "requests and bytes if chunks were grouped into larger reads (network: one granule's chunk index)",
    )
    c.add_argument("--max-mib", type=int, default=64, help="largest grouped read")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
