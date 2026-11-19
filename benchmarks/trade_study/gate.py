"""The correctness gate: every arm must return the reference arm's values
before its timings count.

Arms are compared on the first, middle and last observation of each case,
read together in one question, so an arm is checked on the code a trial
times: worker processes, a multi-file open, a selection of several time
steps. An arm variant (``arm=JSON`` settings) is gated as an arm of its own.
Fill masks and times must be equal, and values must agree within a relative
1e-6 (h5py decodes packed integers in float64, xarray in float32). ``exact``
and ``close`` are recorded apart. A comparison where every value is fill on
both sides is ``vacuous``: it proves nothing, so it is never a pass.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import numpy as np

from benchmarks.trade_study.arms import ARM_CLASSES, Arm, Result
from benchmarks.trade_study.datasets import Dataset
from benchmarks.trade_study.questions import Case
from benchmarks.trade_study.scope import ARM_INFO

GATE_RTOL = 1e-6
PASSING = ("pass", "reference")


def make_arm(name: str, dataset: Dataset, payload: dict[str, Any], settings: dict[str, Any] | None = None) -> Arm:
    """The arm called ``name``; exits with what is missing when the scope lists it but it is not built."""
    if name not in ARM_CLASSES:
        info = ARM_INFO.get(name)
        if info is None:
            raise SystemExit(f"unknown arm {name!r}; one of {sorted(ARM_INFO)}")
        raise SystemExit(f"arm {name!r} is {info.status}, not built. Needs: {info.needs or 'see the README'}")
    return ARM_CLASSES[name](dataset, payload, settings)


def gate_key(arm: str, settings: dict[str, Any] | None = None) -> str:
    """The name a verdict is filed under: the arm, plus its variant's settings when it has any."""
    return f"{arm} {json.dumps(settings, sort_keys=True)}" if settings else arm


def compare(reference: Result, other: Result) -> tuple[str, float]:
    """("exact" | "close" | "vacuous", largest relative difference). Raises on a mismatch."""
    if reference.times.shape != other.times.shape:
        raise AssertionError("observation times differ")
    offsets_us = np.abs((reference.times - other.times) / np.timedelta64(1, "us"))
    if offsets_us.size and offsets_us.max() > 1:
        raise AssertionError("observation times differ")
    worst, vacuous = 0.0, True
    for name, expected in reference.values.items():
        got = other.values[name]
        if expected.shape != got.shape:
            raise AssertionError(f"{name}: shape {got.shape}, expected {expected.shape}")
        if not np.array_equal(np.isnan(expected), np.isnan(got)):
            raise AssertionError(f"{name}: fill masks differ")
        valid = ~np.isnan(expected)
        if valid.any():
            vacuous = False
            scale = np.maximum(np.abs(expected[valid]), np.finfo(float).tiny)
            worst = max(worst, float((np.abs(got[valid] - expected[valid]) / scale).max()))
    if worst > GATE_RTOL:
        raise AssertionError(f"values differ: largest relative difference {worst:.3g} > {GATE_RTOL}")
    return ("vacuous" if vacuous else "exact" if worst == 0 else "close"), worst


def gate_observations(n: int) -> list[int]:
    """The first, middle and last of ``n`` observations."""
    return sorted({0, n // 2, n - 1})


def _read_or_error(arm: Arm, observations: list[int], selection: Any) -> Result | Exception:
    try:
        arm.open(observations)
        return arm.read(observations, selection)
    except Exception as error:  # noqa: BLE001 -- a read failure is a gate row, not a crash
        return error


def _one(result: Result | Exception, position: int) -> Result | Exception:
    """One observation of a result; an error stands for every observation it was read with."""
    if isinstance(result, Exception):
        return result
    values = {name: array[position : position + 1] for name, array in result.values.items()}
    return Result(result.times[position : position + 1], values)


def _verdict(reference: Result | Exception, other: Result | Exception) -> tuple[str, float]:
    try:
        for result in (reference, other):
            if isinstance(result, Exception):
                raise result
        return compare(reference, other)
    except Exception as error:  # noqa: BLE001 -- a failed comparison is a gate row, not a crash
        return f"fail: {type(error).__name__}: {str(error)[:300]}", float("nan")


def run_gate(
    dataset: Dataset,
    payload: dict[str, Any],
    arm_names: tuple[str, ...],
    cases: list[Case],
    variants: list[tuple[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Every arm, and every variant, against the first arm. One row per
    (case, arm, observation); a variant's rows carry its :func:`gate_key`. Network."""
    arms: list[tuple[str, Arm]] = []
    for name, settings in [*((name, None) for name in arm_names), *(variants or [])]:
        arm = make_arm(name, dataset, payload, settings)
        arm.authenticate()
        arms.append((gate_key(name, settings), arm))
    reference, rows = arms[0][0], []
    try:
        for case in cases:
            selection = dataset.selection(case, payload["grid"])
            n_observations = len(dataset.observations(case, len(payload["granules"])))
            observations = gate_observations(n_observations)
            results = [_read_or_error(arm, observations, selection) for _, arm in arms]
            for position, observation in enumerate(observations):
                for (key, _), result in zip(arms[1:], results[1:], strict=True):
                    status, worst = _verdict(_one(results[0], position), _one(result, position))
                    rows.append(
                        {
                            "case": str(case),
                            "arm": key,
                            "reference": reference,
                            "observation": observation,
                            "status": status,
                            "max_rel_diff": worst,
                        }
                    )
                    message = f"gate {case} {key} vs {reference} observation {observation}: {status}"
                    print(message, file=sys.stderr, flush=True)
    finally:
        for _, arm in arms:
            arm.close()
    return rows


def gate_verdicts(rows: list[dict[str, Any]], reference: str) -> dict[str, str]:
    """Per arm: "pass" when no comparison failed and at least one per case was not vacuous."""
    verdicts = {reference: "reference"}
    for arm in sorted({row["arm"] for row in rows}):
        mine = [row for row in rows if row["arm"] == arm]
        failed = any(row["status"].startswith("fail") for row in mine)
        proven = all(
            any(row["status"] in ("exact", "close") for row in mine if row["case"] == case)
            for case in {row["case"] for row in mine}
        )
        verdicts[arm] = "fail" if failed else "pass" if proven else "vacuous"
    return verdicts
