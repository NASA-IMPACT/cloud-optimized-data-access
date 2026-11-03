"""Correctness gate: every file arm must equal the virtual arm exactly.

An observation where both sides are entirely NaN proves nothing and is
recorded as ``vacuous``; it never counts as a pass.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd

from benchmarks.tempo_no2.backends import Backend, SelectionResult, quality_mask
from benchmarks.tempo_no2.cases import Case, SpatialCases

GATE_ALL_OBSERVATIONS_1D = {("point", 1), ("aoi", 500)}
GATE_SAMPLE_LONG_WINDOWS = 4


def assert_equivalent(baseline: SelectionResult, virtual: SelectionResult) -> None:
    assert np.array_equal(baseline.times, virtual.times), "Observation times differ"
    assert baseline.no2.shape == virtual.no2.shape
    assert baseline.quality.shape == virtual.quality.shape
    assert baseline.no2.dtype == virtual.no2.dtype
    assert baseline.quality.dtype == virtual.quality.dtype
    assert np.array_equal(baseline.quality, virtual.quality, equal_nan=True)
    assert np.array_equal(np.isnan(baseline.no2), np.isnan(virtual.no2))
    np.testing.assert_allclose(baseline.no2, virtual.no2, rtol=0, atol=0, equal_nan=True)
    np.testing.assert_allclose(quality_mask(baseline), quality_mask(virtual), rtol=0, atol=0, equal_nan=True)


def gate_observation(baseline: SelectionResult, virtual: SelectionResult) -> Literal["pass", "vacuous"]:
    assert_equivalent(baseline, virtual)
    if np.all(np.isnan(baseline.no2)) and np.all(np.isnan(virtual.no2)):
        return "vacuous"
    return "pass"


def observations_for_gate(case: Case, n_obs: int, rng: np.random.Generator) -> list[int]:
    if case.window == "1d":
        if (case.workload, case.case_size) in GATE_ALL_OBSERVATIONS_1D:
            return list(range(n_obs))
        return [0]
    middle = [i for i in range(1, n_obs - 1)]
    sample = rng.choice(middle, size=min(GATE_SAMPLE_LONG_WINDOWS, len(middle)), replace=False).tolist()
    return sorted({0, n_obs - 1, *sample})


def run_gate(
    baselines: list[Backend],
    virtual: Backend,
    cases: list[Case],
    spatial: SpatialCases,
    n_obs: int,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    import sys
    import time

    rng = rng or np.random.default_rng(0)
    rows: list[dict[str, Any]] = []
    for case in cases:
        for observation in observations_for_gate(case, n_obs, rng):
            for arm in baselines:
                t0 = time.perf_counter()
                try:
                    if case.workload == "point":
                        rows_ = spatial.points.iloc[: case.case_size]
                        status = gate_observation(arm.point([observation], rows_), virtual.point([observation], rows_))
                    else:
                        window = spatial.windows[case.case_size]
                        status = gate_observation(arm.aoi([observation], window), virtual.aoi([observation], window))
                except Exception as error:  # a mismatch or a read failure is a row, not a crash
                    status = f"fail: {type(error).__name__}: {str(error)[:300]}"
                rows.append({"case": str(case), "arm": arm.name, "observation": observation, "status": status})
                print(f"gate {case} {arm.name} observation {observation}: {status} ({time.perf_counter() - t0:.1f} s)", file=sys.stderr, flush=True)
    return pd.DataFrame(rows, columns=["case", "arm", "observation", "status"])


def gate_passed(df: pd.DataFrame) -> bool:
    if df.empty or df["status"].str.startswith("fail").any():
        return False
    return bool((df.groupby("case")["status"].apply(lambda s: (s == "pass").any())).all())
