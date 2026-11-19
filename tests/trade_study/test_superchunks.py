"""The superchunk arithmetic: what grouping chunks into larger reads can buy."""

from __future__ import annotations

import h5py
import pytest

from benchmarks.trade_study import superchunks
from benchmarks.trade_study.questions import Case


def test_group_merges_within_the_gap_and_the_size_cap():
    ranges = [(0, 10), (10, 10), (25, 10), (100, 10)]
    assert superchunks.group(ranges, 0) == [(0, 20), (25, 10), (100, 10)]
    assert superchunks.group(ranges, 5) == [(0, 35), (100, 10)]
    assert superchunks.group(ranges, 1000) == [(0, 110)]
    assert superchunks.group(ranges, 1000, max_bytes=40) == [(0, 35), (100, 10)]
    assert superchunks.group([(50, 10), (0, 10)], 0) == [(0, 10), (50, 10)]


def test_analyse_counts_requests_bytes_and_over_read_per_granule():
    groupings = superchunks.analyse([(0, 10), (10, 10), (25, 10)], n_granules=4, gaps=(0, 5))
    assert [(g.max_gap, g.requests, g.bytes_read) for g in groupings] == [(-1, 12, 120), (0, 8, 120), (5, 4, 140)]
    assert groupings[0].over_read == 0 and groupings[2].over_read == pytest.approx(140 / 120 - 1)
    # grouping cannot go below one request per file
    assert superchunks.analyse([(0, 10), (10, 10)], n_granules=7, gaps=(10**9,))[1].requests == 7
    floor = superchunks.floor(groupings[2], rtt_s=0.1, bandwidth_bps=140.0, concurrency=2)
    assert floor == pytest.approx(2 * 0.1 + 1.0)


def test_needed_ranges_come_from_the_files_own_chunk_index(world):
    dataset, payload = world
    whole_map = dataset.selection(Case("pancake", 1, "1d"), payload["grid"])
    with h5py.File(payload["granules"][0]["source_url"].removeprefix("file://"), "r") as handle:
        everything = superchunks.needed_ranges(handle, dataset.variables, whole_map)
        one = superchunks.needed_ranges(handle, dataset.variables, whole_map.first())
        chunks = sum(handle[variable].id.get_num_chunks() for variable in dataset.variables)
    assert len(everything) == chunks == 12 and len(one) == 2
    assert all(length > 0 for _, length in everything)


def test_analyse_dataset_tabulates_every_headline_case(world):
    dataset, payload = world
    probes = {"rtt_ms_median": 20.0, "bandwidth_mb_s": 100.0}
    rows = superchunks.analyse_dataset(dataset, payload, "1d", probes, max_bytes=2**20)
    assert {row["case"] for row in rows} == {
        "pancake:1:1d",
        "pancake:15:1d",
        "churro:10:1d",
        "churro:250:1d",
        "box:100:1d",
        "batch:0:1d",
    }
    whole_map = [row for row in rows if row["case"] == "pancake:1:1d"]
    assert whole_map[0]["max_gap_bytes"] == -1 and whole_map[0]["requests"] == 12
    assert whole_map[-1]["requests"] == 1 and all(row["floor_seconds"] > 0 for row in rows)
    table = superchunks.render_table(whole_map)
    assert "| `pancake:1:1d` | 1 | one request per chunk | 12 |" in table and "gap ≤ 64 KiB" in table
