import numpy as np
import pytest

from benchmarks.tempo_no2.backends import SelectionResult
from benchmarks.tempo_no2.cases import Case, prepare_spatial_cases
from benchmarks.tempo_no2.run import (
    check_expected,
    noop_factory,
    parse_case,
    run_trial,
    write_expected,
)

WINDOW = {50: (slice(0, 20), slice(0, 40))}


@pytest.fixture(scope="module")
def cases():
    return prepare_spatial_cases(np.linspace(20, 50, 60), np.linspace(-120, -70, 80), windows_override=WINDOW)


class FakeBackend:
    name = "virtual_icechunk"
    settings = {}

    def _result(self, n_obs, shape):
        t = np.array(["2025-03-14T12:00"] * n_obs, dtype="datetime64[ns]")
        return SelectionResult(t, np.ones((n_obs, *shape)), np.zeros((n_obs, *shape)))

    def point(self, observations, point_rows):
        return self._result(len(observations), (len(point_rows),))

    def aoi(self, observations, window):
        return self._result(len(observations), (20, 40))

    def counts(self):
        return None


def fake_backend_factory():
    return FakeBackend()


def test_run_trial_fills_phases_and_static_minimum(cases):
    row = run_trial(fake_backend_factory, Case("point", 10, "1d"), [0, 1], cases, rep=0, cache_state="cold")
    assert row.status == "ok"
    assert row.end_to_end_seconds >= row.retrieval_seconds
    assert row.request_count_reason.startswith("static")
    assert row.request_count > 0 and row.observation_count == 2 and row.arm == "virtual_icechunk"


def test_run_trial_records_host_quiet_fields(cases):
    row = run_trial(fake_backend_factory, Case("point", 10, "1d"), [0], cases, 0, "cold")
    assert row.load_avg_1m >= 0 and row.cpu_steal_s >= 0


def test_noop_trial_has_zero_shape_and_ok_status(cases):
    row = run_trial(noop_factory, Case("point", 1, "1d"), [0], cases, 0, "cold")
    assert row.status == "ok" and row.arm == "noop"


def test_run_trial_captures_backend_error(cases):
    def boom():
        raise RuntimeError("no creds")

    row = run_trial(boom, Case("aoi", 50, "1d"), [0], cases, 0, "cold")
    assert row.status == "RuntimeError: no creds"


def test_case_parsing():
    assert parse_case("aoi:100:1w") == Case("aoi", 100, "1w")


def test_smoke_expected_file_roundtrip(tmp_path):
    write_expected(tmp_path / "e.json", {("netcdf_tuned", "point:1:1d"): 7})
    assert check_expected(tmp_path / "e.json", {("netcdf_tuned", "point:1:1d"): 9}) == [("netcdf_tuned", "point:1:1d", 7, 9)]
    assert check_expected(tmp_path / "e.json", {("netcdf_tuned", "point:1:1d"): 7}) == []
