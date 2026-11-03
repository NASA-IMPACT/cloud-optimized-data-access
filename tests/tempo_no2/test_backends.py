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


def test_user_stack_opens_each_granule_twice_at_most(granules, monkeypatch):
    import fsspec

    fs = fsspec.filesystem("file")
    opened = []
    original = fs.open

    def counting_open(path, *a, **k):
        opened.append(path)
        return original(path, *a, **k)

    monkeypatch.setattr(fs, "open", counting_open)
    NetcdfUserStack([str(p) for p in granules], filesystem=fs)
    # one open for the product group and one for the root time axis; never a third
    assert len(opened) == 2 * len(granules)


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


@pytest.fixture(scope="module")
def http_granules(granules):
    from tests.tempo_no2.http_server import serve

    server, base_url = serve(str(granules[0].parent))
    yield base_url, [f"{base_url}/{p.name}" for p in granules]
    server.shutdown()


def test_tuned_arm_reads_over_http(granules, http_granules, cases):
    base_url, urls = http_granules
    # plain http only for the local test server; the real endpoint is https
    t = NetcdfTuned(urls, {"kind": "http", "base_url": base_url, "client_options": {"allow_http": True}}, max_workers=2)
    u = local_user_stack(granules)
    np.testing.assert_array_equal(t.aoi([0, 1], cases.windows[1]).no2, u.aoi([0, 1], cases.windows[1]).no2)
    assert t.counts()["request_count"] > 0


def test_https_url_maps_s3_keys_to_the_distribution_endpoint():
    from benchmarks.tempo_no2.backends import https_url

    assert https_url("s3://asdc-prod-protected/TEMPO/a.nc") == "https://data.asdc.earthdata.nasa.gov/asdc-prod-protected/TEMPO/a.nc"


def test_container_stores_builds_http_stores_for_https_containers():
    import types

    from benchmarks.tempo_no2.backends import container_stores
    from obstore.store import HTTPStore

    container = types.SimpleNamespace(name="asdc", url_prefix="https://data.asdc.earthdata.nasa.gov/asdc-prod-protected/", store=None)
    repo = types.SimpleNamespace(config=types.SimpleNamespace(virtual_chunk_containers={"asdc": container}))
    stores = container_stores(repo, credentials=None, client_options={"default_headers": {"Authorization": "Bearer x"}})
    assert isinstance(stores["vcc://asdc"], HTTPStore)
    assert stores["https://data.asdc.earthdata.nasa.gov/asdc-prod-protected"] is stores["vcc://asdc"]


def test_tuned_arm_reuses_one_worker_pool(granules, cases):
    t = NetcdfTuned([f"file://{p}" for p in granules], {"kind": "local"}, max_workers=2)
    first = t.aoi([0, 1], cases.windows[1])
    pool = t._pool
    second = t.aoi([0, 1], cases.windows[1])
    assert pool is not None and t._pool is pool
    np.testing.assert_array_equal(first.no2, second.no2)
    t.close()
    assert t._pool is None


def test_virtual_arm_records_and_applies_zarr_concurrency(virtual_repo, manifest):
    import zarr

    repo, snap, _ = virtual_repo
    v = VirtualIcechunk(pinned_session(repo, snap).store, manifest, zarr_concurrency=4)
    assert v.settings["zarr_concurrency"] == 4 and zarr.config.get("async.concurrency") == 4
    VirtualIcechunk(pinned_session(repo, snap).store, manifest)  # default leaves the setting alone
    assert zarr.config.get("async.concurrency") == 4


def test_icechunk_config_overrides(virtual_repo):
    from benchmarks.tempo_no2.backends import icechunk_config

    repo, _, _ = virtual_repo
    cfg = icechunk_config(repo, {"icechunk_concurrency": 32, "icechunk_decodes": 8})
    assert cfg.get_partial_values_concurrency == 32 and cfg.max_concurrent_requests == 32 and cfg.max_concurrent_decodes == 8


def test_virtual_arm_can_use_the_zarrs_codec_pipeline(virtual_repo, manifest, cases):
    import zarr

    repo, snap, _ = virtual_repo
    default = VirtualIcechunk(pinned_session(repo, snap).store, manifest).aoi([0, 1], cases.windows[1])
    try:
        v = VirtualIcechunk(pinned_session(repo, snap).store, manifest, zarrs_pipeline=True)
        assert v.settings["codec_pipeline"] == "zarrs"
        np.testing.assert_array_equal(v.aoi([0, 1], cases.windows[1]).no2, default.no2)
        assert zarr.config.get("codec_pipeline.batch_size") is not None  # zarr's own keys survive
    finally:
        zarr.config.set({"codec_pipeline.path": "zarr.core.codec_pipeline.BatchedCodecPipeline"})
