"""A session: the matrix of trials, each in a fresh process, in shuffled order.

A fresh process per trial keeps one arm's caches, connections and imports
from helping the next. The order is shuffled inside each repetition so drift
in the network or the host does not line up with one arm.

Running a session again under the same run id and session resumes it: trials
that already have an ``ok`` row are skipped, so a (session, repetition) never
holds the same trial twice.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.trade_study.datasets import Dataset
from benchmarks.trade_study.questions import Case
from benchmarks.trade_study.trial import MEASURES, Row

PLAN_SEED = 20250314


@dataclass(frozen=True)
class TrialSpec:
    arm: str
    case: Case
    measure: str
    rep: int
    settings: dict[str, Any] = field(default_factory=dict)  # an arm variant, e.g. {"max_workers": 1}


def plan(
    arms: tuple[str, ...],
    cases: list[Case],
    reps: int,
    variants: list[tuple[str, dict[str, Any]]],
    seed: int = PLAN_SEED,
    measures: tuple[str, ...] = MEASURES,
) -> list[TrialSpec]:
    """Every (arm, case, measure) once per repetition, shuffled inside each
    repetition. Variants run the whole question on the box cases only."""
    rng = random.Random(seed)
    specs: list[TrialSpec] = []
    for rep in range(reps):
        block = [TrialSpec(arm, case, measure, rep) for arm in arms for case in cases for measure in measures]
        if "full" in measures:
            block += [
                TrialSpec(arm, case, "full", rep, settings)
                for arm, settings in variants
                if arm in arms
                for case in cases
                if case.workload == "box"
            ]
        rng.shuffle(block)
        specs += block
    return specs


@dataclass(frozen=True)
class SessionOptions:
    run_id: str
    session: str
    instance_rate_usd_h: float
    timeout_s: float
    probes: str | None  # path to the probes file; None leaves floor_seconds null


def trial_command(dataset: Dataset, spec: TrialSpec, options: SessionOptions) -> list[str]:
    """The ``run.py trial`` command line for one spec."""
    command = [
        sys.executable, "-m", "benchmarks.trade_study.run", "trial",
        "--dataset", dataset.name,
        "--transport", dataset.transport,
        "--arm", spec.arm,
        "--case", str(spec.case),
        "--measure", spec.measure,
        "--rep", str(spec.rep),
        "--run-id", options.run_id,
        "--session", options.session,
        "--instance-rate-usd-h", str(options.instance_rate_usd_h),
    ]  # fmt: skip
    if spec.settings:
        command += ["--settings", json.dumps(spec.settings)]
    if options.probes:
        command += ["--probes", options.probes]
    return command


def run_in_subprocess(dataset: Dataset, spec: TrialSpec, options: SessionOptions) -> Row:
    """One trial in a fresh process. A crash or a timeout comes back as a row saying so."""
    try:
        process = subprocess.run(
            trial_command(dataset, spec, options),
            capture_output=True,
            text=True,
            timeout=options.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failed_row(dataset, spec, options, f"timeout after {options.timeout_s:.0f} s", "")
    output = process.stdout.strip()
    try:
        return Row.from_json(output.splitlines()[-1] if output else "")  # the trial prints its row last
    except Exception:  # noqa: BLE001 -- whatever the process printed, it was not a row
        reason = f"exit {process.returncode}, unparseable output"
        return _failed_row(dataset, spec, options, reason, process.stderr[-2000:])


def _failed_row(dataset: Dataset, spec: TrialSpec, options: SessionOptions, reason: str, stderr_tail: str) -> Row:
    return Row(
        run_id=options.run_id,
        session_id=options.session,
        dataset=dataset.name,
        transport=dataset.transport,
        arm=spec.arm,
        arm_variant=spec.settings,
        workload=spec.case.workload,
        case_size=spec.case.size,
        window=spec.case.window,
        measure=spec.measure,
        repetition=spec.rep,
        status=f"subprocess: {reason}",
        error=stderr_tail,
    )


def _identity(row: Row) -> tuple[Any, ...]:
    """What makes two rows the same trial of the same session."""
    variant = json.dumps(row.arm_variant or {}, sort_keys=True)
    return (
        row.dataset, row.transport, row.session_id, row.arm, variant,
        row.workload, row.case_size, row.window, row.measure, row.repetition,
    )  # fmt: skip


def _spec_identity(dataset: Dataset, spec: TrialSpec, options: SessionOptions) -> tuple[Any, ...]:
    return _identity(_failed_row(dataset, spec, options, "", ""))


def completed(results_file: Path) -> set[tuple[Any, ...]]:
    """The trials ``results_file`` already holds an ``ok`` row for."""
    if not results_file.exists():
        return set()
    rows = (Row.from_json(line) for line in results_file.read_text().splitlines() if line.strip())
    return {_identity(row) for row in rows if row.status == "ok"}


def run_session(dataset: Dataset, specs: list[TrialSpec], options: SessionOptions, out_dir: Path) -> tuple[int, int]:
    """Run every spec that has no ``ok`` row yet, appending each row to
    ``out_dir/results.jsonl`` as it lands. Returns (trials run, trials failed)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    done = completed(out_dir / "results.jsonl")
    ran = failed = 0
    with (out_dir / "results.jsonl").open("a") as results:
        for index, spec in enumerate(specs, start=1):
            if _spec_identity(dataset, spec, options) in done:
                continue
            ran += 1
            row = run_in_subprocess(dataset, spec, options)
            failed += row.status != "ok"
            results.write(row.to_json() + "\n")
            results.flush()
            seconds = row.first_byte_seconds if spec.measure == "first_byte" else row.end_to_end_seconds
            outcome = f"{seconds:.2f} s" if row.status == "ok" else row.status
            progress = f"[{index}/{len(specs)}] {spec.arm} {spec.case} {spec.measure} rep {spec.rep}: {outcome}"
            print(progress, file=sys.stderr, flush=True)
    return ran, failed
