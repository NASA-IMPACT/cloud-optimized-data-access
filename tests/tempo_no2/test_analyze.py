import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from benchmarks.tempo_no2.analyze import load, publish, summarize

FIXTURE = Path(__file__).parent.parent / "fixtures" / "results-small-run"


@pytest.fixture
def results_small():
    return load(FIXTURE)


@pytest.fixture
def results_small_dir(tmp_path):
    run_dir = tmp_path / "tempo_no2" / "small"
    shutil.copytree(FIXTURE, run_dir)
    return run_dir


def cell(s, arm, size, window="1d"):
    return next(x for x in s["cells"] if x["arm"] == arm and x["case_size"] == size and x["window"] == window and not x.get("arm_settings"))


def test_summary_cells_and_speedups(results_small):
    s = summarize(results_small)
    c = cell(s, "virtual_icechunk", 10)
    assert c["n"] == 10
    assert set(c["phases"]) == {"catalog", "open", "retrieval", "processing"}
    sp = next(x for x in s["speedups"] if x["baseline_arm"] == "netcdf_user_stack" and x["case_size"] == 10)
    assert sp["ci_low"] <= sp["speedup_median"] <= sp["ci_high"]
    assert sp["speedup_median"] > 3 and "session_spread" in sp and isinstance(sp["detectable"], bool)
    assert any(x["label"] == "concurrency_only" for x in s["speedups"])


def test_summary_limit_checks_and_cov(results_small):
    c = summarize(results_small)["cells"][0]
    assert 0 <= c["cpu_fraction"] and 0 <= c["bandwidth_fraction"] <= 1.5 and c["cov"] >= 0
    assert c["likely_limiter"] in {"cpu-one-core", "cpu-all-cores", "network-bandwidth", "latency", "unexplained"}


def test_summary_subtracts_noop_from_net_rx(results_small):
    c = cell(summarize(results_small), "virtual_icechunk", 10)
    assert c["net_rx_bytes"] == 4000


def test_summary_ignores_failed_rows(results_small):
    s = summarize(results_small)
    assert all(c["n"] <= 10 for c in s["cells"])
    assert cell(s, "virtual_icechunk", 100)["n"] == 10  # the failed row is not counted as an eleventh
    assert cell(s, "netcdf_tuned", 10)["max_s"] < 50  # warm-up row excluded


def test_summary_errors_on_arm_without_ok_rows(results_small):
    df = results_small.copy()
    mask = (df.arm == "netcdf_tuned") & (df.case_size == 100) & (df.repetition >= 0)
    df.loc[mask, "status"] = "subprocess:timeout"
    with pytest.raises(ValueError, match="netcdf_tuned"):
        summarize(df)


def test_publish_writes_deck_copy_and_pointer(tmp_path, results_small_dir):
    deck = tmp_path / "deck"
    publish(results_small_dir, deck)
    copy = json.loads((deck / "public" / "results" / "summary.json").read_text())
    pointer = json.loads((results_small_dir.parent / "latest.json").read_text())
    assert copy["run_id"] == pointer["run_id"] == "small"
    assert {p.name for p in (deck / "public" / "figures").iterdir()} == {"phases.svg", "requests.svg", "floor_ratio.svg", "speedup.svg"}
    assert (results_small_dir / "summary.json").exists()


def test_catalog_rows_feed_file_arm_phases_and_never_become_cells(results_small):
    s = summarize(results_small)
    assert not any(c["arm"] == "catalog" for c in s["cells"])
    assert cell(s, "netcdf_user_stack", 10)["phases"]["catalog"] == pytest.approx(1.6)
    assert cell(s, "virtual_icechunk", 10)["phases"]["catalog"] == 0.0


def test_failed_checker_arm_does_not_abort_summary(results_small):
    s = summarize(results_small)  # the fixture has a failed virtual_manifeststore row and no ok one
    assert all(c["arm"] != "virtual_manifeststore" for c in s["cells"])


def test_cells_carry_request_count_kind(results_small):
    s = summarize(results_small)
    assert cell(s, "virtual_icechunk", 10)["request_count_kind"] == "static-minimum"
    assert cell(s, "netcdf_tuned", 10)["request_count_kind"] == "client-ranges"
    assert cell(s, "netcdf_user_stack", 10)["request_count_kind"] == "http-calls"


def test_cells_record_access_transport(results_small):
    s = summarize(results_small)
    assert all(c["access"] == "s3" for c in s["cells"])  # fixture rows predate the field; s3 is the default
