import json
import shutil
from pathlib import Path

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
    return next(x for x in s["cells"] if x["arm"] == arm and x["case_size"] == size and x["window"] == window and not x.get("arm_variant"))


def test_backend_settings_do_not_make_a_cell_a_variant(results_small):
    # every arm records its full settings; only an orchestrator override (arm_variant) makes a variant
    s = summarize(results_small)
    plain = [c for c in s["cells"] if not c["arm_variant"]]
    assert {c["arm"] for c in plain} == {"netcdf_user_stack", "netcdf_tuned", "virtual_icechunk"}
    assert all(c["arm_settings"] for c in plain if c["arm"] == "netcdf_tuned")
    variants = [c for c in s["cells"] if c["arm_variant"]]
    assert len(variants) == 1 and variants[0]["arm_variant"] == {"max_workers": 1}


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
    names = {p.name for p in (deck / "public" / "figures").iterdir()}
    assert names == {f"{n}.{ext}" for n in ("phases", "requests", "floor_ratio", "speedup") for ext in ("svg", "png")}
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


def test_figures_leave_out_the_checker_arm(results_small, tmp_path):
    from benchmarks.tempo_no2.analyze import figures

    s = summarize(results_small)
    s["cells"].append({**cell(s, "virtual_icechunk", 10), "arm": "virtual_manifeststore", "request_count_kind": "client-ranges"})
    figures(s, tmp_path)
    assert "virtual_manifeststore" not in (tmp_path / "phases.svg").read_text()
    assert "virtual_manifeststore" not in (tmp_path / "requests.svg").read_text()


def test_limiter_names_decode_when_a_core_is_busy_and_the_network_is_not():
    from benchmarks.tempo_no2.analyze import _limiter

    assert _limiter(cpu_fraction=1.1, nproc=4, max_core=None, bandwidth_fraction=0.45, floor_ratio=3.4) == "cpu-decode"
    assert _limiter(cpu_fraction=0.3, nproc=4, max_core=None, bandwidth_fraction=0.45, floor_ratio=1.5) == "latency"


def _one_granule_cells():
    def c(arm, variant, open_s, read_s, rx, **extra):
        return {"arm": arm, "arm_variant": variant, "workload": "scan", "case_size": 1, "window": "1d",
                "phases": {"open": open_s, "retrieval": read_s, "processing": 0.08}, "median_s": open_s + read_s + 0.08,
                "net_rx_bytes": rx, "bytes_requested": extra.pop("bytes_requested", None), "retrieval_split": extra.pop("split", {})}

    return [
        c("netcdf_user_stack", {}, 24.9, 1.93, 1.9e9),
        c("netcdf_tuned", {"max_workers": 1}, 2.52, 2.01, 85e6),
        c("virtual_icechunk", {}, 1.49, 3.01, 32e6),
        c("netcdf_download", {}, 2.48, 2.82, 515e6, bytes_requested=515e6, split={"download_seconds": 1.15, "decode_seconds": 1.52}),
    ]


def test_floor_is_decode_plus_the_smallest_fetch_at_the_download_rate(tmp_path):
    from benchmarks.tempo_no2.analyze import (
        FLOOR_BARS,
        floor_figure,
        floor_seconds_for_one_granule,
    )

    cells = _one_granule_cells()
    by_label = {label: next(c for c in cells if c["arm"] == arm) for label, arm, _ in FLOOR_BARS}
    assert floor_seconds_for_one_granule(by_label) == pytest.approx(1.52 + 32e6 / (515e6 / 1.15))
    assert floor_figure({"cells": cells}, tmp_path).name == "floor.svg"
    assert floor_figure({"cells": cells[:3]}, tmp_path) is None  # no download arm: no figure, no crash


def test_cells_carry_the_retrieval_split_median():
    import pandas as pd

    from benchmarks.tempo_no2.analyze import _split_medians

    rows = pd.Series([{"download_seconds": 1.0, "decode_seconds": 2.0}, {"download_seconds": 3.0, "decode_seconds": 4.0}, float("nan"), {}])
    assert _split_medians(rows) == {"download_seconds": 2.0, "decode_seconds": 3.0}
    assert _split_medians(pd.Series([float("nan")])) == {}
