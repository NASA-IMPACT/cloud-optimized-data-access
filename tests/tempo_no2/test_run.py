import numpy as np
import pytest

from benchmarks.tempo_no2.backends import SelectionResult
from benchmarks.tempo_no2.cases import Case, prepare_spatial_cases
from benchmarks.tempo_no2.run import (
    check_expected,
    noop_factory,
    parse_case,
    prepare_backend,
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
    calls = 0

    def _result(self, n_obs, shape):
        t = np.array(["2025-03-14T12:00"] * n_obs, dtype="datetime64[ns]")
        return SelectionResult(t, np.ones((n_obs, *shape)), np.zeros((n_obs, *shape)))

    def point(self, observations, point_rows):
        self.calls += 1
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
    assert row.load_avg_1m >= 0
    if row.cpu_steal_s is None:  # no /proc/stat (macOS): null with a reason, never a crash
        assert row.cpu_steal_s_reason
    else:
        assert row.cpu_steal_s >= 0


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


def test_run_trial_names_the_arm_when_the_factory_raises(cases):
    def boom():
        raise RuntimeError("no creds")

    row = run_trial(boom, Case("aoi", 50, "1d"), [0], cases, 0, "cold", arm="netcdf_tuned")
    assert row.status.startswith("RuntimeError") and row.arm == "netcdf_tuned"


def test_prepare_backend_warms_once_for_warm_process(cases):
    b = prepare_backend(fake_backend_factory, Case("point", 10, "1d"), [0], cases, "warm_process")
    assert isinstance(b, FakeBackend) and b.calls == 1
    assert prepare_backend(fake_backend_factory, Case("point", 10, "1d"), [0], cases, "cold") is None
    row = run_trial(fake_backend_factory, Case("point", 10, "1d"), [0], cases, 0, "warm_process", backend=b)
    assert row.status == "ok" and b.calls == 2 and row.open_seconds < 0.01


def test_uninstrumented_file_arm_reports_null_count_with_reason(cases):
    class Https(FakeBackend):
        name = "netcdf_user_stack"

    row = run_trial(lambda: Https(), Case("point", 10, "1d"), [0], cases, 0, "cold", arm="netcdf_user_stack")
    assert row.status == "ok" and row.request_count is None and "not instrumented" in row.request_count_reason


def test_run_trial_records_the_count_kind_from_the_backend(cases):
    class Counted(FakeBackend):
        name = "netcdf_tuned"

        def counts(self):
            return {"request_count": 7, "bytes_requested": 10, "requests_by_method": {}, "kind": "client-ranges"}

    row = run_trial(lambda: Counted(), Case("point", 10, "1d"), [0], cases, 0, "cold", arm="netcdf_tuned")
    assert row.request_count == 7 and row.request_count_reason == "client-ranges"


def test_smoke_budget_depends_on_transport():
    from benchmarks.tempo_no2.run import smoke_budget_s

    assert smoke_budget_s("s3") == 120 and smoke_budget_s("https") == 900


def test_arm_factory_spatial_comes_from_the_manifest_grid():
    from benchmarks.tempo_no2.run import ArmFactory

    payload = {"granules": [], "snapshot_id": "x", "grid": {
        "latitude": {"first": 20.0, "last": 50.0, "size": 60}, "longitude": {"first": -120.0, "last": -70.0, "size": 80}}}
    spatial = ArmFactory(payload).spatial()
    assert spatial.latitudes.shape == (60,) and spatial.longitudes.shape == (80,)


def test_parse_arms_validates_names():
    from benchmarks.tempo_no2.run import parse_arms

    assert parse_arms("netcdf_tuned,virtual_icechunk") == ("netcdf_tuned", "virtual_icechunk")
    with pytest.raises(Exception, match="bogus"):
        parse_arms("netcdf_tuned,bogus")


def test_scan_case_reads_the_whole_grid_for_its_first_observations_only(cases):
    seen = {}

    class Scan(FakeBackend):
        def aoi(self, observations, window):
            seen["observations"], seen["window"] = list(observations), window
            return self._result(len(observations), (20, 40))

    row = run_trial(lambda: Scan(), Case("scan", 1, "1d"), [0, 1], cases, 0, "cold", arm="virtual_icechunk")
    assert row.status == "ok" and row.observation_count == 1
    assert seen["observations"] == [0] and seen["window"] == cases.full_window
    assert parse_case("scan:1:1d") == Case("scan", 1, "1d")
