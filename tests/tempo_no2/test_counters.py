import logging
import types

import h5py
import obstore

from benchmarks.tempo_no2.counters import (
    RetryLogCounter,
    S3fsCounter,
    probe_paging,
    trace_counts,
    traced_store,
)


def test_trace_counts_from_memory_store():
    store = obstore.store.MemoryStore()
    obstore.put(store, "a", b"x" * 4096)
    t, trace = traced_store(store)
    t.get_range("a", start=0, end=1024)
    t.head("a")
    c = trace_counts(trace)
    assert c["request_count"] == 2
    assert c["bytes_requested"] == 1024
    assert c["requests_by_method"]["head"] == 1
    assert c["kind"] == "client-ranges"  # Python calls into obstore, one per byte range; not HTTP requests


def test_s3fs_counter_counts_bytes_for_gets_only_and_retries_from_metadata():
    class Events:  # minimal fake of botocore's event emitter
        def __init__(self):
            self.h = []

        def register(self, name, fn):
            self.h.append((name, fn))

    class Client:
        meta = types.SimpleNamespace(events=Events())

    fs = types.SimpleNamespace(s3=Client())
    c = S3fsCounter()
    c.attach(fs)
    handlers = dict(fs.s3.meta.events.h)
    assert set(handlers) == {"after-call"}  # needs-retry fires on successes too; it is not used
    get = types.SimpleNamespace(name="GetObject")
    head = types.SimpleNamespace(name="HeadObject")
    handlers["after-call"](parsed={"ContentLength": 10, "ResponseMetadata": {"RetryAttempts": 0}}, model=get)
    handlers["after-call"](parsed={"ContentLength": 50_000_000, "ResponseMetadata": {"RetryAttempts": 2}}, model=head)
    counts = c.counts()
    assert counts["request_count"] == 2
    assert counts["bytes_requested"] == 10  # a HEAD's ContentLength is the object size, not bytes moved
    assert counts["retries"] == 2
    assert counts["requests_by_method"] == {"GetObject": 1, "HeadObject": 1}
    assert counts["kind"] == "http-calls"  # one botocore after-call per completed S3 operation


def test_retry_log_counter_counts_retry_lines():
    r = RetryLogCounter()
    r.attach()
    logging.getLogger("obstore").debug("Retrying request after error")
    logging.getLogger("obstore").debug("got 200")
    r.detach()
    assert r.count == 1


def test_probe_paging_false_for_plain_file(tmp_path):
    with h5py.File(tmp_path / "p.h5", "w") as f:
        f["x"] = [1]
    with h5py.File(tmp_path / "p.h5") as f:
        assert probe_paging(f) is False


def test_probe_network_picks_an_object_when_no_path_is_given():
    from benchmarks.tempo_no2.counters import first_object, probe_network

    store = obstore.store.MemoryStore()
    obstore.put(store, "repo/snapshots/a", b"x" * 100)
    obstore.put(store, "repo/chunks/b", b"y" * 100)
    assert first_object(store, "repo") in {"repo/snapshots/a", "repo/chunks/b"}
    out = probe_network(store, None, heads=3, range_reads=0, prefix="repo")
    assert out["rtt_ms_median"] >= 0 and out["bandwidth_mb_s"] is None
