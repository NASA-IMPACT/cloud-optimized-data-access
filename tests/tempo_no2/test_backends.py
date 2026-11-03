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
    import fsspec

    return NetcdfUserStack([str(p) for p in granules], filesystem=fsspec.filesystem("file"))


def test_user_stack_matches_h5py(granules, cases):
    b = local_user_stack(granules)
    r = b.aoi([0, 1], cases.windows[1])
    assert r.no2.shape == (2, 20, 40) and r.times[0] < r.times[1]
    with h5py.File(granules[0]) as f:
        raw = f["product/vertical_column_troposphere"][0, 0:20, 0:40]
    np.testing.assert_array_equal(np.where(raw == NO2_FILL, np.nan, raw), r.no2[0])


def test_tuned_matches_user_stack_and_counts_requests(granules, cases):
    t = NetcdfTuned([f"file://{p}" for p in granules], {"kind": "local"}, max_workers=2)
    u = local_user_stack(granules)
    pr = cases.points.iloc[:10]
    np.testing.assert_array_equal(t.point([0, 1], pr).no2, u.point([0, 1], pr).no2)
    np.testing.assert_array_equal(t.aoi([0, 1], cases.windows[1]).quality, u.aoi([0, 1], cases.windows[1]).quality)
    assert t.counts()["request_count"] > 0
    assert t.settings["block_size"] == 8 * 2**20 and t.settings["paged"] is False
    assert t.settings["max_workers"] == 2 and t.settings["workers"] == "processes"


def test_user_stack_opens_each_granule_once(granules, monkeypatch):
    import fsspec

    fs = fsspec.filesystem("file")
    opened = []
    original = fs.open

    def counting_open(path, *a, **k):
        opened.append(path)
        return original(path, *a, **k)

    monkeypatch.setattr(fs, "open", counting_open)
    NetcdfUserStack([str(p) for p in granules], filesystem=fs)
    assert len(opened) == len(granules)


def test_container_stores_cover_both_reference_forms(virtual_repo):
    from benchmarks.tempo_no2.backends import container_stores

    repo, _, _ = virtual_repo
    stores = container_stores(repo, credentials=None)
    container = next(iter(repo.config.virtual_chunk_containers.values()))
    assert container.url_prefix.rstrip("/") in stores
    assert f"vcc://{container.name}" in stores


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


def test_static_minimum_from_store_metadata_matches_traced_chunk_reads(virtual_repo, cases, manifest):
    from benchmarks.tempo_no2.backends import VirtualManifestStore
    from benchmarks.tempo_no2.measure import METADATA_OBJECTS

    repo, snap, native_prefix = virtual_repo
    session = pinned_session(repo, snap)
    v = VirtualIcechunk(session.store, manifest)
    window = cases.windows[1]
    static = v.static_minimum([0, 1], window[0], window[1])
    # fixture: data chunks (1, 20, 40) on a 60×80 grid; the window is exactly one chunk per variable
    assert static["data_chunks"] == 2 * 2
    assert static["coordinate_chunks"] >= 3
    assert static["request_count"] == METADATA_OBJECTS + static["coordinate_chunks"] + static["data_chunks"]
    m = VirtualManifestStore(session, {"file://": LocalStore()}, native_chunks_prefix=native_prefix, manifest=manifest)
    m.aoi([0, 1], window)
    # The ManifestStore path reads coordinates through the parsed session, not the
    # traced registry, so the cross-check covers data chunk reads only.
    assert m.counts()["request_count"] == static["data_chunks"]
