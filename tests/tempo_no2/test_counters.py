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


def test_s3fs_counter_counts_after_call_events():
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
    handlers["after-call"](parsed={"ContentLength": 10}, model=types.SimpleNamespace(name="GetObject"))
    handlers["needs-retry"](response=(None, {"Error": {"Code": "SlowDown"}}), attempts=1)
    handlers["needs-retry"](response=None, attempts=2)
    counts = c.counts()
    assert counts["request_count"] == 1
    assert counts["bytes_requested"] == 10
    assert counts["retries"] == 1


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
