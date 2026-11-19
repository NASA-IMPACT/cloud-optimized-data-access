"""The scope tables and the question geometry."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from benchmarks.trade_study import scope
from benchmarks.trade_study.arms import ARM_CLASSES
from benchmarks.trade_study.datasets import DATASETS, get_dataset, latlon
from benchmarks.trade_study.datasets.goes19 import Goes19, goes_scan_angles
from benchmarks.trade_study.datasets.tempo_no2 import TempoNo2, match_store_times
from benchmarks.trade_study.gate import make_arm
from benchmarks.trade_study.questions import Case, Selection, box_bounds, headline_cases, parse_case, question_points

GOES_GRID = {
    "x": {"scale": 5.6e-05, "offset": -0.151844, "size": 5424},
    "y": {"scale": -5.6e-05, "offset": 0.151844, "size": 5424},
    "perspective_point_height": 35786023.0,
    "semi_major_axis": 6378137.0,
    "semi_minor_axis": 6356752.31414,
    "longitude_of_projection_origin": -75.0,
}

# --- scope ------------------------------------------------------------------------


def test_every_built_arm_has_a_class_and_every_class_is_registered():
    assert set(scope.RUNNABLE) == set(ARM_CLASSES)


def test_every_built_dataset_has_a_class_and_every_class_is_registered():
    assert {d.name for d in scope.DATASETS if d.status == "built"} == set(DATASETS)


def test_status_lists_every_arm_dataset_workload_and_metric():
    text = scope.render_status()
    for name in [a.name for a in scope.ARMS] + [d.name for d in scope.DATASETS] + [w.name for w in scope.WORKLOADS]:
        assert f"`{name}" in text
    assert "first_byte_seconds" in text


def test_a_proposed_arm_is_refused_with_what_it_needs(world):
    with pytest.raises(SystemExit, match="proposed.*HDF5 2.2.0"):
        make_arm("ros3", *world)


def test_transport_is_checked_per_dataset():
    assert get_dataset("tempo_no2", "https").transport == "https"
    s3_url = {"source_url": "s3://asdc-prod-protected/a/b.nc"}
    https_url = "https://data.asdc.earthdata.nasa.gov/asdc-prod-protected/a/b.nc"
    assert get_dataset("tempo_no2", "https").url(s3_url) == https_url
    assert get_dataset("goes19").transport == "s3"
    with pytest.raises(SystemExit, match="no 'https' transport"):
        get_dataset("goes19", "https")


# --- questions --------------------------------------------------------------------


def test_question_points_are_seeded_and_nested():
    lats, lons = question_points(250)
    # the first TEMPO benchmark's points: results from both harnesses are about the same places
    assert lats[:3].tolist() == [37.53599950736106, 36.18099985837, 34.935235463387606]
    assert lons[:3].tolist() == [-96.43360283483071, -93.84224259558374, -98.06038492863527]
    assert len(lats) == 250 and np.array_equal(question_points(10)[0], lats[:10])
    south, north, west, east = box_bounds(500)
    assert ((lats >= south) & (lats <= north) & (lons >= west) & (lons <= east)).all()


def test_a_500_km_box_is_about_4_5_degrees_of_latitude():
    south, north, _, _ = box_bounds(500)
    assert abs((north - south) - 500 / 110.574) < 1e-9


def test_first_value_of_a_selection():
    assert Selection("window", slice(5, 9), slice(2, 7)).first() == Selection("window", slice(5, 6), slice(2, 3))
    first = Selection("points", np.array([4, 5]), np.array([6, 7])).first()
    assert first.y.tolist() == [4] and first.x.tolist() == [6]


def test_cases():
    assert parse_case("churro:10:1d") == Case("churro", 10, "1d")
    with pytest.raises(ValueError):
        parse_case("point:10:1d")
    assert [str(c) for c in headline_cases("1d")] == [
        "pancake:1:1d",
        "pancake:15:1d",
        "churro:10:1d",
        "churro:250:1d",
        "box:100:1d",
        "batch:0:1d",
    ]
    assert TempoNo2.observations(Case("pancake", 15, "1d"), 12) == list(range(12))
    assert TempoNo2.observations(Case("pancake", 1, "1d"), 12) == [0]
    assert TempoNo2.observations(Case("batch", 0, "1d"), 12) == list(range(12))


# --- regular latitude/longitude grids (TEMPO) -------------------------------------


def test_nearest_indices_on_a_descending_axis():
    assert list(latlon.nearest_indices(np.array([5.0, 4.0, 3.0, 2.0]), np.array([4.1, 2.4]))) == [1, 3]


def test_grid_spec_roundtrips_a_regular_grid(lat, lon):
    spec = latlon.grid_spec(lat, lon)
    assert set(spec) == {"latitude", "longitude"} and spec["latitude"]["size"] == 2950
    lat2, lon2 = latlon.grid_arrays(spec)
    np.testing.assert_allclose(lat2, lat, atol=1e-9)
    np.testing.assert_allclose(lon2, lon, atol=1e-9)


def test_grid_spec_rejects_an_irregular_axis(lat, lon):
    bent = lat.copy()
    bent[1000] += 0.05
    with pytest.raises(ValueError, match="latitude"):
        latlon.grid_spec(bent, lon)


def test_tempo_selections_on_its_real_grid(lat, lon):
    grid = latlon.grid_spec(lat, lon)
    dataset = TempoNo2()
    assert dataset.full_window(grid) == (slice(0, 2950), slice(0, 7750))
    box = dataset.selection(Case("box", 100, "1d"), grid)
    south, north, west, east = box_bounds(100)
    assert south <= lat[box.y].min() and lat[box.y].max() <= north
    assert west <= lon[box.x].min() and lon[box.x].max() <= east
    points = dataset.selection(Case("churro", 10, "1d"), grid)
    assert np.abs(lat[points.y] - question_points(10)[0]).max() <= (lat[1] - lat[0]) / 2 + 1e-9


def test_granules_match_store_times_one_to_one():
    store_times = np.array(["2025-03-14T12:01:00", "2025-03-14T13:02:00"], dtype="datetime64[ns]")
    granules = [
        {"granule_ur": "b", "start_time": "2025-03-14T13:00:00Z"},
        {"granule_ur": "a", "start_time": "2025-03-14T12:00:00Z"},
    ]
    assert match_store_times(store_times, granules).tolist() == [1, 0]
    late = [{"granule_ur": "late", "start_time": "2025-03-14T15:00:00Z"}]
    with pytest.raises(RuntimeError, match="missing from the store snapshot.*late"):
        match_store_times(store_times, late)
    with pytest.raises(RuntimeError, match="same store time step"):
        match_store_times(store_times[:1], [granules[1], {**granules[1], "start_time": "2025-03-14T12:02:00Z"}])


# --- the GOES-R fixed grid --------------------------------------------------------


def test_goes_projection_puts_the_subsatellite_point_at_the_grid_centre():
    y, x = goes_scan_angles(GOES_GRID, np.array([0.0]), np.array([-75.0]))
    assert abs(y[0]) < 1e-12 and abs(x[0]) < 1e-12
    yi, xi = Goes19().index_points(GOES_GRID, np.array([0.0]), np.array([-75.0]))
    assert abs(yi[0] - 2711.5) <= 0.5 and abs(xi[0] - 2711.5) <= 0.5


def test_goes_north_is_up_and_east_is_right():
    yi, xi = Goes19().index_points(GOES_GRID, np.array([10.0, 0.0]), np.array([-75.0, -65.0]))
    assert yi[0] < 2711 and xi[1] > 2712


def test_goes_box_is_about_the_stated_size():
    ys, xs = Goes19().index_window(GOES_GRID, box_bounds(100))
    # 2 km pixels at nadir, stretched towards the limb: a 100 km box is a few dozen pixels
    assert 20 <= ys.stop - ys.start <= 60 and 20 <= xs.stop - xs.start <= 60


def test_goes_point_off_the_disk_is_an_error():
    with pytest.raises(ValueError, match="off the full-disk grid"):
        Goes19().index_points(GOES_GRID, np.array([0.0]), np.array([20.0]))


def test_a_dataset_says_why_it_cannot_build_a_store(world):
    with pytest.raises(ValueError, match="nothing to build"):
        TempoNo2().build_virtual_store({}, "hdf5")
    with pytest.raises(ValueError, match="no dmrpp sidecars"):
        Goes19().build_virtual_store({}, "dmrpp")


# --- what a TEMPO manifest pins ---------------------------------------------------


def test_the_pin_is_the_benchmark_tag_when_the_store_has_one(world, tmp_path):
    from benchmarks.trade_study.datasets.tempo_no2 import SNAPSHOT_TAG, resolve_pin
    from tests.trade_study import fixtures

    _, payload = world
    granules = [Path(g["source_url"].removeprefix("file://")) for g in payload["granules"]]
    repo, first = fixtures.make_virtual_store(tmp_path, granules)
    assert resolve_pin(repo) == (first, None)  # no tag yet: main's tip, and the manifest says it is untagged
    repo.create_tag(SNAPSHOT_TAG, first)
    later = repo.writable_session("main").commit("a later commit on main", allow_empty=True)
    assert later != first and resolve_pin(repo) == (first, SNAPSHOT_TAG)  # the tag, not the tip


def test_describe_grid_pins_the_snapshot_and_the_stores_own_grid(world):
    dataset, payload = world
    grid = dataset.describe_grid(payload["granules"])
    assert grid.pop("snapshot_id") == payload["snapshot_id"]
    for axis in ("latitude", "longitude"):
        assert grid[axis]["size"] == payload["grid"][axis]["size"]
        assert grid[axis]["first"] == pytest.approx(payload["grid"][axis]["first"], abs=1e-4)
        assert grid[axis]["last"] == pytest.approx(payload["grid"][axis]["last"], abs=1e-4)
    # a granule the snapshot does not hold is refused when the manifest is written, not at the first trial
    missing = [{**payload["granules"][0], "start_time": "2025-03-20T12:00:00Z"}]
    with pytest.raises(RuntimeError, match="missing from the store snapshot"):
        dataset.describe_grid(missing)
