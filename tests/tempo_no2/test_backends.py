import h5py
import numpy as np
import pytest
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore

from benchmarks.tempo_no2.backends import (
    NetcdfTuned,
    NetcdfUserStack,
    VirtualIcechunk,
    pinned_session,
    resolve_pin,
)
from benchmarks.tempo_no2.cases import CONFIG, prepare_spatial_cases
from tests.tempo_no2.fixtures import NO2_FILL, make_granules, make_virtual_store, manifest_for

WINDOW = {1: (slice(0, 20), slice(0, 40))}


@pytest.fixture(scope="module")
def granules(tmp_path_factory):
    return make_granules(tmp_path_factory.mktemp("granules"))


@pytest.fixture(scope="module")
def virtual_repo(tmp_path_factory, granules):
    return make_virtual_store(tmp_path_factory.mktemp("store"), granules)


@pytest.fixture(scope="module")
def manifest(granules):
    return manifest_for(granules)


@pytest.fixture(scope="module")
def cases(granules):
    with h5py.File(granules[0]) as f:
        return prepare_spatial_cases(f["latitude"][:], f["longitude"][:], windows_override=WINDOW)


def local_user_stack(granules):
    return NetcdfUserStack([str(p) for p in granules], opener=lambda urls: urls)


def test_user_stack_matches_h5py(granules, cases):
    b = local_user_stack(granules)
    r = b.aoi([0, 1], cases.windows[1])
    assert r.no2.shape == (2, 20, 40) and r.times[0] < r.times[1]
    with h5py.File(granules[0]) as f:
        raw = f["product/vertical_column_troposphere"][0, 0:20, 0:40]
    np.testing.assert_array_equal(np.where(raw == NO2_FILL, np.nan, raw), r.no2[0])


def test_tuned_matches_user_stack_and_counts_requests(granules, cases):
    reg = ObjectStoreRegistry({"file://": LocalStore()})
    t = NetcdfTuned([f"file://{p}" for p in granules], reg)
    u = local_user_stack(granules)
    pr = cases.points.iloc[:10]
    np.testing.assert_array_equal(t.point([0, 1], pr).no2, u.point([0, 1], pr).no2)
    np.testing.assert_array_equal(t.aoi([0, 1], cases.windows[1]).quality, u.aoi([0, 1], cases.windows[1]).quality)
    assert t.counts()["request_count"] > 0
    assert t.settings["block_size"] == 8 * 2**20 and t.settings["paged"] is False


def test_virtual_icechunk_matches_user_stack(granules, virtual_repo, cases, manifest):
    repo, snap, _ = virtual_repo
    v = VirtualIcechunk(pinned_session(repo, snap).store, manifest)
    u = local_user_stack(granules)
    np.testing.assert_array_equal(v.aoi([0, 1], cases.windows[1]).no2, u.aoi([0, 1], cases.windows[1]).no2)
    pr = cases.points.iloc[:10]
    np.testing.assert_array_equal(v.point([0, 1], pr).no2, u.point([0, 1], pr).no2)
    assert v.assert_one_to_one() <= 240
    assert v.counts() is None


def test_resolve_pin_prefers_tag(virtual_repo):
    repo, snap, _ = virtual_repo
    assert resolve_pin(repo) == (snap, None)
    repo.create_tag(CONFIG.snapshot_tag, snap)
    assert resolve_pin(repo) == (snap, CONFIG.snapshot_tag)


def test_manifeststore_matches_icechunk_and_counts(virtual_repo, cases, manifest):
    from benchmarks.tempo_no2.backends import VirtualManifestStore

    repo, snap, native_prefix = virtual_repo
    session = pinned_session(repo, snap)
    m = VirtualManifestStore(session, {"file://": LocalStore()}, native_chunks_prefix=native_prefix, manifest=manifest)
    v = VirtualIcechunk(session.store, manifest)
    np.testing.assert_array_equal(m.aoi([0, 1], cases.windows[1]).no2, v.aoi([0, 1], cases.windows[1]).no2)
    assert m.counts()["request_count"] > 0
