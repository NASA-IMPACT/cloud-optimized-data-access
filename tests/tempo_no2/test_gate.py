import numpy as np
import pandas as pd
import pytest

from benchmarks.tempo_no2.backends import SelectionResult
from benchmarks.tempo_no2.cases import Case
from benchmarks.tempo_no2.gate import gate_observation, gate_passed, observations_for_gate

T = np.array(["2025-03-14T12:00"], dtype="datetime64[ns]")


def test_gate_all_nan_is_vacuous():
    r = SelectionResult(times=T, no2=np.full((1, 2, 2), np.nan), quality=np.zeros((1, 2, 2)))
    assert gate_observation(r, r) == "vacuous"


def test_gate_detects_value_mismatch():
    a = SelectionResult(T, np.ones((1, 2, 2)), np.zeros((1, 2, 2)))
    b = SelectionResult(T, np.ones((1, 2, 2)) * 2, np.zeros((1, 2, 2)))
    with pytest.raises(AssertionError):
        gate_observation(a, b)


def test_gate_passes_on_identical_finite_values():
    a = SelectionResult(T, np.ones((1, 2, 2)), np.zeros((1, 2, 2)))
    assert gate_observation(a, SelectionResult(T, np.ones((1, 2, 2)), np.zeros((1, 2, 2)))) == "pass"


def test_observations_for_gate_scopes():
    rng = np.random.default_rng(0)
    assert observations_for_gate(Case("aoi", 500, "1d"), 12, rng) == list(range(12))
    assert observations_for_gate(Case("point", 1, "1d"), 12, rng) == list(range(12))
    assert observations_for_gate(Case("point", 50, "1d"), 12, rng) == [0]
    s = observations_for_gate(Case("point", 10, "1m"), 400, rng)
    assert len(s) == 6 and {0, 399} <= set(s)


def test_gate_passed_requires_a_real_pass():
    df = pd.DataFrame([{"case": "point:1:1d", "arm": "netcdf_tuned", "observation": 0, "status": "vacuous"}])
    assert gate_passed(df) is False
    ok = pd.DataFrame([{"case": "point:1:1d", "arm": "netcdf_tuned", "observation": 0, "status": "pass"}])
    assert gate_passed(ok) is True
    bad = pd.concat([ok, pd.DataFrame([{"case": "point:1:1d", "arm": "netcdf_tuned", "observation": 1, "status": "fail: x"}])])
    assert gate_passed(bad) is False


def test_run_gate_reports_progress_per_comparison(capsys):
    from benchmarks.tempo_no2.gate import run_gate

    class Arm:
        def __init__(self, name):
            self.name = name
            self.settings = {}

        def point(self, observations, point_rows):
            return SelectionResult(T, np.ones((1, len(point_rows))), np.zeros((1, len(point_rows))))

        def aoi(self, observations, window):
            return SelectionResult(T, np.ones((1, 2, 2)), np.zeros((1, 2, 2)))

        def counts(self):
            return None

    points = pd.DataFrame({"lat_index": [0, 1], "lon_index": [0, 1]})
    spatial = type("S", (), {"points": points, "windows": {50: (slice(0, 2), slice(0, 2))}})()
    table = run_gate([Arm("netcdf_tuned")], Arm("virtual_icechunk"), [Case("point", 1, "1d")], spatial, n_obs=2)
    assert list(table["status"]) == ["pass", "pass"]
    err = capsys.readouterr().err
    assert "point:1:1d" in err and "observation 0" in err and "pass" in err and "observation 1" in err


def test_gate_scans_observation_zero_with_the_full_window():
    assert observations_for_gate(Case("scan", 1, "1d"), 12, np.random.default_rng(0)) == [0]
