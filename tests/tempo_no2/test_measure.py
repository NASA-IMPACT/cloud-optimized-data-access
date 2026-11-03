import json

import numpy as np
import pytest

from benchmarks.tempo_no2.measure import (
    FIELDS,
    TrialRow,
    cost_usd,
    floor_seconds,
    resource_usage,
    static_minimum_requests,
)


def test_static_minimum_counts_straddled_chunks():
    # lat 700..800 crosses the chunk edge at 738; lon 1900..2000 crosses 1938 → 2×2 chunks
    n, b = static_minimum_requests(
        (1, 2950, 7750), (1, 738, 1938), [0], slice(700, 800), slice(1900, 2000), itemsize=8
    )
    assert n == 4 and b == 4 * 738 * 1938 * 8


def test_static_minimum_points_count_distinct_chunks():
    n, _ = static_minimum_requests(
        (1, 2950, 7750), (1, 738, 1938), [0, 1], np.array([10, 20, 800]), np.array([5, 6, 5]), itemsize=8
    )
    assert n == 4  # two chunks per time step × two time steps


def test_floor_seconds():
    assert floor_seconds(3, 0.05, 16 * 2**20, 100e6) == pytest.approx(0.15 + 16 * 2**20 / 100e6)


def test_cost_usd():
    assert cost_usd(10_000, 3600, 0.192) == pytest.approx(0.004 + 0.192)


def test_trial_row_roundtrip_has_every_field():
    row = TrialRow(arm="virtual_icechunk", status="ok")
    d = json.loads(row.to_json())
    assert set(d) == set(FIELDS)
    assert TrialRow.from_json(row.to_json()) == row


def test_resource_usage_reports_nonnegative():
    with resource_usage() as r:
        sum(range(10**5))
    assert r.cpu_user_s >= 0 and r.peak_rss_bytes > 0


def test_resource_usage_scales_maxrss_only_on_linux(monkeypatch):
    import resource

    from benchmarks.tempo_no2 import measure

    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    monkeypatch.setattr(measure.sys, "platform", "darwin")
    with resource_usage() as r:
        pass
    assert r.peak_rss_bytes == pytest.approx(raw, rel=0.05)  # macOS reports bytes already
    monkeypatch.setattr(measure.sys, "platform", "linux")
    with resource_usage() as r:
        pass
    assert r.peak_rss_bytes == pytest.approx(raw * 1024, rel=0.05)


def test_host_info_fills_cpu_and_memory_without_proc(monkeypatch):
    from benchmarks.tempo_no2 import measure

    monkeypatch.setattr(measure, "_proc_value", lambda *_: None)  # macOS: no /proc
    info = measure.host_info(imds=False)
    assert info["mem_total_bytes"] and info["mem_total_bytes"] > 0
    assert info["cpu_model"]


def test_host_info_marks_jupyterhub_sessions(monkeypatch):
    from benchmarks.tempo_no2 import measure

    monkeypatch.setenv("JUPYTERHUB_USER", "someone")
    monkeypatch.setattr(measure, "_imds", lambda path, timeout=0.5: {"instance-type": "r5.xlarge", "placement/availability-zone": "us-west-2a"}[path])
    info = measure.host_info()
    assert info["host_kind"] == "jupyterhub" and info["instance_type"] == "r5.xlarge"


def test_resource_usage_includes_child_processes():
    import multiprocessing as mp

    def burn():
        x = 0
        for i in range(3_000_000):
            x += i * i

    with resource_usage() as r:
        # fork, so the nested function needs no pickling; nothing here touches obstore
        p = mp.get_context("fork").Process(target=burn)
        p.start()
        p.join()
    assert r.cpu_user_s > 0.05  # the child's CPU time counts (RUSAGE_CHILDREN after join)
