"""The arms on synthetic TEMPO-shaped granules and a local virtual store, and one trial through each."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from benchmarks.trade_study.arms import ARM_CLASSES, Result
from benchmarks.trade_study.gate import compare
from benchmarks.trade_study.questions import Case
from benchmarks.trade_study.trial import Row, answer, price, run_trial, serial_round_trips
from tests.trade_study import fixtures
from tests.trade_study.http_server import serve

ARMS = ("tuned_h5py", "user_stack", "download", "virtual_icechunk")
NO2 = "vertical_column_troposphere"
PROBES = {"rtt_ms_median": 1.0, "bandwidth_mb_s": 100.0}


def opened(name, dataset, payload, case, settings=None):
    """The arm, authenticated and opened for the case, with the case's observations and selection."""
    arm = ARM_CLASSES[name](dataset, payload, settings)
    observations = dataset.observations(case, len(payload["granules"]))
    arm.authenticate()
    arm.open(observations)
    return arm, observations, dataset.selection(case, payload["grid"])


def read(name, dataset, payload, case, settings=None):
    arm, observations, selection = opened(name, dataset, payload, case, settings)
    try:
        return arm.read(observations, selection)
    finally:
        arm.close()


# --- every arm returns the same values --------------------------------------------


@pytest.mark.parametrize(
    "case", [Case("churro", 10, "1d"), Case("box", 500, "1d"), Case("pancake", 1, "1d"), Case("batch", 0, "1d")]
)
def test_every_arm_returns_the_reference_values(world, case):
    dataset, payload = world
    reference = read("tuned_h5py", dataset, payload, case, {"max_workers": 1})
    n_observations = len(dataset.observations(case, 3))
    assert reference.times.shape == (n_observations,)
    assert np.isfinite(reference.values[NO2]).any()
    for name in ARMS:
        status, _ = compare(reference, read(name, dataset, payload, case))
        assert status == "exact", name


def test_reference_values_are_the_fixture_values(world):
    dataset, payload = world
    got = read("tuned_h5py", dataset, payload, Case("pancake", 1, "1d"))
    expected = fixtures.expected_no2(0, 60, 80).astype(float)
    expected[expected == fixtures.NO2_FILL] = np.nan
    assert np.array_equal(got.values[NO2], expected, equal_nan=True)


# --- one arm at a time ------------------------------------------------------------


def test_user_stack_opens_each_granule_twice_and_never_a_third_time(world, monkeypatch):
    import fsspec

    dataset, payload = world
    filesystem = fsspec.filesystem("file")
    paths = []
    real_open = filesystem.open

    def counting_open(path, *args, **kwargs):
        paths.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(filesystem, "open", counting_open)
    monkeypatch.setattr(dataset, "filesystem", lambda credentials: filesystem)
    arm, *_ = opened("user_stack", dataset, payload, Case("box", 500, "1d"))
    # one open for the product group and one for the root group's time axis
    assert len(paths) == 2 * len(payload["granules"])
    assert arm.settings["files_opened"] == 3


def test_tuned_arm_reuses_one_worker_pool_and_counts_its_workers_requests(world):
    dataset, payload = world
    arm, observations, selection = opened("tuned_h5py", dataset, payload, Case("box", 500, "1d"), {"max_workers": 2})
    try:
        assert arm.settings["workers"] == "processes"
        opens_here = len(arm._handles)
        first = arm.read(observations, selection)
        assert len(arm._handles) == opens_here == 1  # the granule opened here is read here, not again in a worker
        assert arm._worker_counts["request_count"] > 0  # the other two, in the workers
        pool, requests_after_one = arm._pool, arm.counts()["request_count"]
        second = arm.read(observations, selection)
        assert pool is not None and arm._pool is pool
        assert arm.counts()["request_count"] > requests_after_one > 0
        np.testing.assert_array_equal(first.values[NO2], second.values[NO2])
    finally:
        arm.close()
    assert arm._pool is None


def test_tuned_arm_with_workers_asks_for_no_more_than_without(world):
    """No granule is opened in this process and again in a worker."""
    dataset, payload = world
    counts = {}
    for workers in (1, 2):
        arm, observations, selection = opened(
            "tuned_h5py", dataset, payload, Case("box", 500, "1d"), {"max_workers": workers}
        )
        try:
            arm.read(observations, selection)
            counts[workers] = arm.counts()
        finally:
            arm.close()
    # in-process, the two later granules are opened inside the read; with workers, by the workers
    assert counts[2]["request_count"] == counts[1]["request_count"]
    assert counts[2]["bytes_requested"] == counts[1]["bytes_requested"]


def test_tuned_arm_reads_over_http(world, monkeypatch):
    dataset, payload = world
    directory = payload["granules"][0]["source_url"].removeprefix("file://").rsplit("/", 1)[0]
    server, base_url = serve(directory)
    try:
        # plain http only for the local test server; the real endpoint is https
        spec = {"kind": "http", "base_url": base_url, "client_options": {"allow_http": True}}
        served = [{**g, "source_url": f"{base_url}/{g['source_url'].rsplit('/', 1)[1]}"} for g in payload["granules"]]
        monkeypatch.setattr(dataset, "store_spec", lambda credentials: spec)
        case = Case("box", 500, "1d")
        over_http = read("tuned_h5py", dataset, {**payload, "granules": served}, case, {"max_workers": 2})
    finally:
        server.shutdown()
        monkeypatch.undo()
    assert compare(read("tuned_h5py", dataset, payload, case), over_http)[0] == "exact"


def test_download_arm_fetches_every_byte_and_times_download_apart_from_decode(world):
    dataset, payload = world
    arm, observations, selection = opened("download", dataset, payload, Case("churro", 10, "1d"), {"part_bytes": 4096})
    arm.read(observations, selection)
    sizes = [Path(g["source_url"].removeprefix("file://")).stat().st_size for g in payload["granules"]]
    counts = arm.counts()
    assert counts["bytes_requested"] == sum(sizes)
    assert counts["requests_by_method"]["head"] == 3  # one per granule, then the ranges
    assert set(arm.read_split) == {"download_seconds", "decode_seconds"}


def test_virtual_arm_records_and_applies_zarr_concurrency(world, monkeypatch):
    import zarr

    dataset, payload = world
    before = zarr.config.get("async.concurrency")
    assert before != 4
    arm, observations, selection = opened(
        "virtual_icechunk", dataset, payload, Case("box", 500, "1d"), {"zarr_concurrency": 4}
    )
    assert arm.settings["zarr_concurrency"] == 4
    in_force = []
    real_load = type(arm._data).load

    def recording_load(self, **kwargs):
        in_force.append(zarr.config.get("async.concurrency"))
        return real_load(self, **kwargs)

    monkeypatch.setattr(type(arm._data), "load", recording_load)
    arm.read(observations, selection)
    assert in_force == [4]  # in force while the arm reads
    assert zarr.config.get("async.concurrency") == before  # and only then: the next arm is not affected
    default, *_ = opened("virtual_icechunk", dataset, payload, Case("box", 500, "1d"))
    assert default.settings["zarr_concurrency"] == before


def test_virtual_arm_can_use_the_zarrs_codec_pipeline(world):
    import zarr

    dataset, payload = world
    case = Case("box", 500, "1d")
    default = read("virtual_icechunk", dataset, payload, case)
    pipeline = zarr.config.get("codec_pipeline.path")
    arm, observations, selection = opened("virtual_icechunk", dataset, payload, case, {"zarrs_pipeline": True})
    assert arm.settings["codec_pipeline"] == "zarrs"
    assert arm._zarr_config()["codec_pipeline.path"] == "zarrs.ZarrsCodecPipeline"
    np.testing.assert_array_equal(arm.read(observations, selection).values[NO2], default.values[NO2])
    assert zarr.config.get("codec_pipeline.batch_size") is not None  # zarr's own keys survive
    assert zarr.config.get("codec_pipeline.path") == pipeline  # the pipeline is this arm's only


def test_virtual_static_minimum_comes_from_the_stores_chunk_grid(world):
    dataset, payload = world
    arm, observations, selection = opened("virtual_icechunk", dataset, payload, Case("pancake", 1, "1d"))
    minimum = arm.static_minimum(observations, selection)
    # the fixture: two variables of 3 × 2 chunks per observation; 4 metadata objects; at least 3 coordinate chunks
    assert minimum["kind"] == "static-minimum" and minimum["request_count"] >= 4 + 3 + 12
    one_cell = arm.static_minimum(observations, selection.first())
    assert minimum["request_count"] - one_cell["request_count"] == 12 - 2
    # uncompressed chunk bytes: float64 and int16 chunks of 20 × 40
    assert one_cell["bytes_requested"] == 20 * 40 * 8 + 20 * 40 * 2


@pytest.mark.parametrize("case", [Case("churro", 10, "1d"), Case("box", 500, "1d"), Case("pancake", 2, "1d")])
def test_virtual_static_minimum_matches_the_chunks_hdf5_finds_in_the_files(world, case):
    """The bound printed as "≥ n requests" is checked against a second source:
    the files' own chunk index, as the tuned arm looks it up."""
    import h5py

    from benchmarks.trade_study import h5
    from benchmarks.trade_study.measure import METADATA_OBJECTS

    dataset, payload = world
    arm, observations, selection = opened("virtual_icechunk", dataset, payload, case)
    in_files = 0
    for o in observations:
        with h5py.File(payload["granules"][o]["source_url"].removeprefix("file://")) as handle:
            in_files += h5.locate_chunks(handle, dataset.variables, selection)
    minimum = arm.static_minimum(observations, selection)
    coordinate_chunks = minimum["request_count"] - METADATA_OBJECTS - in_files
    # the rest is coordinates: one time chunk per granule in the fixture store, one latitude, one longitude
    assert coordinate_chunks == len(payload["granules"]) + 2


def test_virtual_arms_fetch_no_credentials_of_their_own(world, monkeypatch):
    """The store authorizes its chunk container when it opens; a fetch before that would be timed and never used."""
    dataset, payload = world
    monkeypatch.setattr(dataset, "auth", lambda: pytest.fail("the virtual arm asked for credentials"))
    monkeypatch.setattr(type(dataset), "public", False)
    arm = ARM_CLASSES["virtual_icechunk"](dataset, payload)
    row = run_trial(arm, dataset, Case("box", 500, "1d"), "first_byte", warm=False)
    assert row.status == "ok", row.error
    assert row.auth_seconds is None and "store fetches its own credentials" in row.auth_reason
    assert row.first_byte_seconds == pytest.approx(row.manifest_seconds + row.byte_seconds, abs=1e-3)
    assert ARM_CLASSES["virtual_dmrpp"].authenticate is ARM_CLASSES["virtual_icechunk"].authenticate
    # on a public bucket the step is reported as before, with why it is zero
    monkeypatch.undo()
    public = run_trial(ARM_CLASSES["virtual_icechunk"](dataset, payload), dataset, Case("box", 500, "1d"), "full")
    assert public.auth_seconds is not None and public.auth_reason == "public bucket: no credentials"


# --- trials -----------------------------------------------------------------------


def test_first_byte_is_the_sum_of_its_parts(world):
    dataset, payload = world
    row = run_trial(ARM_CLASSES["tuned_h5py"](dataset, payload), dataset, Case("box", 500, "1d"), "first_byte")
    assert row.status == "ok", row.error
    assert row.chunk_id_seconds is not None and row.chunk_id_reason is None
    parts = row.auth_seconds + row.manifest_seconds + row.chunk_id_seconds + row.byte_seconds
    assert row.first_byte_seconds == pytest.approx(parts, abs=1e-6)
    assert row.end_to_end_seconds is None and row.last_byte_seconds is None
    assert row.arm_settings["workers"] == "in-process"  # no worker pool before the first value
    assert row.warm_seconds is not None and row.discovery_seconds == 0.5 and row.request_count > 0


@pytest.mark.parametrize("name", ARMS)
def test_full_trial_records_every_part_or_a_reason(world, name):
    dataset, payload = world
    row = run_trial(ARM_CLASSES[name](dataset, payload), dataset, Case("churro", 10, "1d"), "full", probes=PROBES)
    assert row.status == "ok", row.error
    assert row.first_byte_seconds is None
    assert row.end_to_end_seconds >= row.last_byte_seconds > 0
    assert (row.chunk_id_seconds is None) == (row.chunk_id_reason is not None)
    assert row.byte_seconds > 0 and row.observation_count == 3
    assert row.floor_seconds > 0 and row.cost_usd > 0
    assert row.arm_status == "built" and row.section == 1
    assert Row.from_json(row.to_json()) == row
    if name == "virtual_icechunk":
        assert row.request_count_kind == "static-minimum"
    if name == "download":
        assert row.byte_split["download_seconds"] + row.byte_split["decode_seconds"] <= row.byte_seconds


def test_a_pancake_reads_the_whole_map_for_its_first_observations_only(world):
    dataset, payload = world
    row = run_trial(ARM_CLASSES["download"](dataset, payload), dataset, Case("pancake", 1, "1d"), "full", warm=False)
    assert row.observation_count == 1 and row.requests_by_method["head"] == 1


def test_a_failing_arm_is_a_row_not_a_crash(world):
    dataset, payload = world
    missing = [{**g, "source_url": g["source_url"] + ".missing"} for g in payload["granules"]]
    arm = ARM_CLASSES["download"](dataset, {**payload, "granules": missing})
    row = run_trial(arm, dataset, Case("pancake", 1, "1d"), "full")
    assert row.status != "ok" and row.error


def test_floor_uses_wire_bytes_when_the_count_is_a_static_bound(world, monkeypatch):
    from benchmarks.trade_study import trial

    dataset, payload = world
    received = iter([1_000, 6_000])  # the host's receive counter before and after: 5 kB on the wire
    monkeypatch.setattr(trial, "net_rx_bytes", lambda: next(received))
    arm = ARM_CLASSES["virtual_icechunk"](dataset, payload)
    row = run_trial(arm, dataset, Case("churro", 10, "1d"), "full", probes=PROBES, warm=False)
    assert row.net_rx_bytes == 5_000 and row.bytes_requested > 5_000
    round_trips = serial_round_trips("virtual_icechunk", row.request_count, 3)
    assert row.floor_seconds == pytest.approx(round_trips * 0.001 + 5_000 / 100e6)


def test_serial_round_trips_per_kind_of_arm():
    assert serial_round_trips("virtual_icechunk", 500, 15) == 4 + 1  # metadata objects, then one round of chunks
    assert serial_round_trips("download", 500, 15) == 2 * 15  # per granule: a HEAD, then one round of ranges
    assert serial_round_trips("tuned_h5py", 45, 15) == 3  # one granule's chain; granules overlap
    assert serial_round_trips("tuned_h5py", 0, 15) == 1


def test_answer_is_a_nan_aware_mean_per_observation():
    values = {"v": np.array([[1.0, np.nan, 3.0], [np.nan, np.nan, np.nan]])}
    means = answer(Result(np.zeros(2, dtype="datetime64[ns]"), values))["v"]
    assert means[0] == 2.0 and np.isnan(means[1])


def test_transfer_is_free_in_region_and_egress_otherwise():
    measured = {"measure": "full", "end_to_end_seconds": 3600.0, "request_count": 1000, "net_rx_bytes": 10**9}
    inside, outside = Row(**measured, in_region=True), Row(**measured, in_region=False)
    price(inside, 0.192)
    price(outside, 0.192)
    assert inside.transfer_usd == 0 and outside.transfer_usd == pytest.approx(0.09)
    assert inside.cost_usd == pytest.approx(0.0004 + 0.192)
