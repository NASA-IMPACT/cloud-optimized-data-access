import numpy as np

from benchmarks.tempo_no2.cases import (
    Case,
    aoi_bounds,
    matrix,
    nearest_indices,
    prepare_spatial_cases,
)


def test_aoi_bounds_500km_is_about_4_5_degrees_lat():
    lo, hi, _, _ = aoi_bounds(500)
    assert abs((hi - lo) - 500 / 110.574) < 1e-9


def test_nearest_indices_descending_axis():
    assert list(nearest_indices(np.array([5.0, 4.0, 3.0, 2.0]), np.array([4.1, 2.4]))) == [1, 3]


def test_prepare_spatial_cases_is_deterministic_and_nested(lat, lon):
    a = prepare_spatial_cases(lat, lon)
    b = prepare_spatial_cases(lat, lon)
    assert a.points.equals(b.points)
    assert len(a.points) == 250
    assert set(a.windows) == {50, 100, 250, 500}


def test_matrix_headline_has_five_one_day_cells_including_a_full_scan():
    m = matrix(headline_only=True)
    assert len(m) == 5 and all(c.window == "1d" for c in m)
    assert Case("scan", 1, "1d") in m  # the file reader's best case: one whole scan, one observation


def test_full_window_covers_the_grid(lat, lon):
    s = prepare_spatial_cases(lat, lon)
    assert s.full_window == (slice(0, 2950), slice(0, 7750))


def test_matrix_full_has_week_and_month_only_for_fixed_cells():
    m = matrix(headline_only=False)
    assert {(c.workload, c.case_size) for c in m if c.window != "1d"} == {("point", 10), ("aoi", 100)}
    assert Case("point", 10, "1m") in m


def test_grid_spec_roundtrips_a_regular_grid(lat, lon):
    from benchmarks.tempo_no2.cases import grid_arrays, grid_spec

    spec = grid_spec(lat, lon)
    assert set(spec) == {"latitude", "longitude"} and spec["latitude"]["size"] == 2950
    lat2, lon2 = grid_arrays(spec)
    np.testing.assert_allclose(lat2, lat, atol=1e-9)
    np.testing.assert_allclose(lon2, lon, atol=1e-9)


def test_grid_spec_rejects_an_irregular_axis(lat, lon):
    import pytest

    from benchmarks.tempo_no2.cases import grid_spec

    bent = lat.copy()
    bent[1000] += 0.05
    with pytest.raises(ValueError, match="latitude"):
        grid_spec(bent, lon)
