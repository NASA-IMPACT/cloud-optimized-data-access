"""The building blocks: stores, request counters, probes, resource measurement and the host description."""

from __future__ import annotations

import asyncio
import multiprocessing
import resource
import types

import h5py
import numpy as np
import obstore
import pytest

from benchmarks.trade_study import h5, host, measure
from benchmarks.trade_study.counters import S3fsCounter, add_counts, empty_counts, trace_counts, traced_store
from benchmarks.trade_study.measure import floor_seconds, resource_usage, static_minimum_requests
from benchmarks.trade_study.probes import concurrent_rate, probe_network
from benchmarks.trade_study.stores import build_store, fetch_object, icechunk_config, store_path

# --- stores -----------------------------------------------------------------------


def test_store_path_per_kind_of_store():
    assert store_path({"kind": "local"}, "file:///tmp/a.nc") == "/tmp/a.nc"
    s3 = {"kind": "s3", "bucket": "b", "prefix": "", "region": "us-west-2"}
    assert store_path(s3, "s3://b/dir/a.nc") == "dir/a.nc"
    assert store_path({**s3, "prefix": "dir"}, "s3://b/dir/a.nc") == "a.nc"
    http = {"kind": "http", "base_url": "https://example.org"}
    assert store_path(http, "https://example.org/b/dir/a.nc") == "b/dir/a.nc"
    assert store_path(http, "s3://b/dir/a.nc") == "b/dir/a.nc"  # the endpoint serves an S3 key as bucket/key


def test_fetch_object_reads_every_byte_in_parts():
    memory = obstore.store.MemoryStore()
    obstore.put(memory, "g.nc", bytes(range(250)) * 4)
    store, trace = traced_store(memory)
    data = asyncio.run(fetch_object(store, "g.nc", part_bytes=300, concurrency=2))  # 4 parts, the last one short
    assert bytes(data) == bytes(range(250)) * 4
    assert trace_counts(trace)["requests_by_method"] == {"head": 1, "get_range": 4}


def test_icechunk_config_applies_the_tuning_factors(world):
    repo = world[0].open_repo({})
    config = icechunk_config(repo, {"icechunk_concurrency": 32, "icechunk_decodes": 8})
    assert config.get_partial_values_concurrency == 32 and config.max_concurrent_requests == 32
    assert config.max_concurrent_decodes == 8


# --- counters ---------------------------------------------------------------------


def test_trace_counts_from_a_memory_store():
    memory = obstore.store.MemoryStore()
    obstore.put(memory, "a", b"x" * 4096)
    store, trace = traced_store(memory)
    store.get_range("a", start=0, end=1024)
    store.head("a")
    counts = trace_counts(trace)
    assert counts["request_count"] == 2 and counts["bytes_requested"] == 1024
    assert counts["requests_by_method"]["head"] == 1
    assert counts["kind"] == "client-ranges"  # Python calls into obstore, one per byte range; not HTTP requests


def test_add_counts_sums_per_method():
    total = empty_counts()
    for _ in range(2):
        add_counts(total, {"request_count": 3, "bytes_requested": 10, "requests_by_method": {"head": 1, "get": 2}})
    assert total["request_count"] == 6 and total["bytes_requested"] == 20
    assert total["requests_by_method"] == {"head": 2, "get": 4}


def test_s3fs_counter_counts_bytes_for_gets_only_and_retries_from_metadata():
    class Events:  # minimal fake of botocore's event emitter
        def __init__(self):
            self.handlers = []

        def register(self, name, handler):
            self.handlers.append((name, handler))

    filesystem = types.SimpleNamespace(s3=types.SimpleNamespace(meta=types.SimpleNamespace(events=Events())))
    counter = S3fsCounter()
    counter.attach(filesystem)
    handlers = dict(filesystem.s3.meta.events.handlers)
    assert set(handlers) == {"after-call"}  # needs-retry fires on successes too; it is not used
    get, head = types.SimpleNamespace(name="GetObject"), types.SimpleNamespace(name="HeadObject")
    handlers["after-call"](parsed={"ContentLength": 10, "ResponseMetadata": {"RetryAttempts": 0}}, model=get)
    handlers["after-call"](parsed={"ContentLength": 50_000_000, "ResponseMetadata": {"RetryAttempts": 2}}, model=head)
    counts = counter.counts()
    assert counts["request_count"] == 2
    assert counts["bytes_requested"] == 10  # a HEAD's ContentLength is the object size, not bytes moved
    assert counts["retries"] == 2
    assert counts["requests_by_method"] == {"GetObject": 1, "HeadObject": 1}
    assert counts["kind"] == "http-calls"  # one botocore after-call per completed S3 operation


# --- probes -----------------------------------------------------------------------


def test_probe_network_times_heads_and_range_reads():
    store = obstore.store.MemoryStore()
    obstore.put(store, "a", b"x" * 1000)
    probe = probe_network(store, "a", heads=3, range_reads=2, range_bytes=100)
    assert probe["rtt_ms_median"] >= 0 and probe["bandwidth_mb_s"] > 0
    assert probe_network(store, "a", heads=3, range_reads=0)["bandwidth_mb_s"] is None


def test_concurrent_rate_reads_a_bounded_prefix(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 1000)
    store, trace = traced_store(build_store({"kind": "local"}))
    assert concurrent_rate(store, str(big), part_bytes=100, parts=3) > 0
    assert int(trace.to_dataframe()["length"].sum()) == 300  # never the whole object


# --- HDF5 -------------------------------------------------------------------------


def test_a_plain_hdf5_file_is_not_paged(tmp_path):
    with h5py.File(tmp_path / "plain.h5", "w") as f:
        f["x"] = [1]
    with h5py.File(tmp_path / "plain.h5") as f:
        assert h5.is_paged(f) is False


def test_decode_cf_masks_fill_then_scales(tmp_path):
    with h5py.File(tmp_path / "packed.h5", "w") as f:
        packed = f.create_dataset("v", data=np.array([1, 2, -9], dtype="int16"))
        packed.attrs["_FillValue"] = np.array([-9], dtype="int16")
        packed.attrs["scale_factor"] = np.array([0.5])
        packed.attrs["add_offset"] = np.array([10.0])
        np.testing.assert_array_equal(h5.decode_cf(packed, packed[:]), [10.5, 11.0, np.nan])


# --- requests a selection cannot avoid, and the floor ------------------------------


def test_static_minimum_counts_straddled_chunks():
    # rows 700..800 cross the chunk edge at 738; columns 1900..2000 cross 1938 → 2 × 2 chunks
    shape, chunks = (1, 2950, 7750), (1, 738, 1938)
    n, nbytes = static_minimum_requests(shape, chunks, [0], slice(700, 800), slice(1900, 2000), itemsize=8)
    assert n == 4 and nbytes == 4 * 738 * 1938 * 8


def test_static_minimum_points_count_distinct_chunks():
    shape, chunks = (1, 2950, 7750), (1, 738, 1938)
    n, _ = static_minimum_requests(shape, chunks, [0, 1], np.array([10, 20, 800]), np.array([5, 6, 5]), itemsize=8)
    assert n == 4  # two chunks per time step × two time steps


def test_floor_seconds():
    assert floor_seconds(3, 0.05, 16 * 2**20, 100e6) == pytest.approx(0.15 + 16 * 2**20 / 100e6)


# --- CPU and memory ---------------------------------------------------------------


def test_resource_usage_scales_maxrss_only_on_linux(monkeypatch):
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    monkeypatch.setattr(measure.sys, "platform", "darwin")
    with resource_usage() as usage:
        pass
    assert usage.peak_rss_bytes == pytest.approx(raw, rel=0.05)  # macOS reports bytes already
    monkeypatch.setattr(measure.sys, "platform", "linux")
    with resource_usage() as usage:
        pass
    assert usage.peak_rss_bytes == pytest.approx(raw * 1024, rel=0.05)


def _burn():
    total = 0
    for i in range(3_000_000):
        total += i * i


def test_resource_usage_includes_child_processes():
    with resource_usage() as usage:
        child = multiprocessing.get_context("spawn").Process(target=_burn)
        child.start()
        child.join()
    assert usage.cpu_user_s > 0.05  # the child's CPU time counts once it has been waited on


# --- the host ---------------------------------------------------------------------


def test_host_info_fills_cpu_and_memory_without_proc(monkeypatch):
    monkeypatch.setattr(host, "_proc_value", lambda *_: None)  # macOS: no /proc
    info = host.host_info(imds=False)
    assert info["mem_total_bytes"] > 0 and info["cpu_model"]


def test_host_info_marks_jupyterhub_sessions(monkeypatch):
    metadata = {"instance-type": "r5.xlarge", "placement/availability-zone": "us-west-2a"}
    monkeypatch.setenv("JUPYTERHUB_USER", "someone")
    monkeypatch.setattr(host, "_imds", lambda path, timeout=0.5: metadata[path])
    info = host.host_info()
    assert info["host_kind"] == "jupyterhub" and info["instance_type"] == "r5.xlarge"
    assert host.client_region(info) == "us-west-2"


def test_client_region_can_be_set_by_hand(monkeypatch):
    assert host.client_region({"availability_zone": None}) is None
    monkeypatch.setenv("BENCH_CLIENT_REGION", "eu-west-1")
    assert host.client_region({"availability_zone": "us-west-2a"}) == "eu-west-1"
