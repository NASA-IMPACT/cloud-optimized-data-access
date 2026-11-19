"""The report: medians, paired speedups, figures, and results in the first benchmark's format."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from benchmarks.trade_study import figures, legacy, report
from benchmarks.trade_study.arms import ARM_CLASSES
from benchmarks.trade_study.questions import Case
from benchmarks.trade_study.trial import Row, run_trial

FIRST_BENCHMARK_RUN = Path(__file__).parents[1] / "fixtures" / "first-benchmark-run"
PROBES = {"rtt_ms_median": 1.0, "bandwidth_mb_s": 100.0}


def write_rows(run_dir: Path, rows: list[Row]) -> None:
    (run_dir / "results.jsonl").write_text("".join(row.to_json() + "\n" for row in rows))


def timed(arm: str, seconds: float, rep: int, session: str = "1", variant: dict | None = None, **fields) -> Row:
    """A whole-question row for the 100 km box, as if the trial had taken ``seconds``."""
    row = Row(
        run_id="t", session_id=session, dataset="tempo_no2", arm=arm, arm_variant=variant or {}, workload="box",
        case_size=100, window="1d", measure="full", observation_count=15, repetition=rep, in_region=True,
        auth_seconds=0.1, manifest_seconds=seconds / 4, byte_seconds=seconds / 2, end_to_end_seconds=seconds,
        gate_status="pass", status="ok",
    )  # fmt: skip
    for name, value in fields.items():
        setattr(row, name, value)
    return row


# --- medians ----------------------------------------------------------------------


def test_report_summarises_medians_and_keeps_failures(world, tmp_path):
    dataset, payload = world
    rows = []
    for name in ("tuned_h5py", "virtual_icechunk"):
        for measure in ("first_byte", "full"):
            for rep in range(2):
                arm = ARM_CLASSES[name](dataset, payload)
                row = run_trial(arm, dataset, Case("box", 500, "1d"), measure, rep=rep, probes=PROBES, warm=False)
                row.run_id, row.gate_status = "t", "pass" if name == "tuned_h5py" else "not gated"
                rows.append(row)
    timeout = Row(run_id="t", dataset="tempo_no2", arm="download", workload="box", case_size=500, window="1d",
                  measure="full", status="subprocess: timeout")  # fmt: skip
    write_rows(tmp_path, [*rows, timeout])
    text = report.write_report(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["rows"] == 9 and summary["failed"] == 1 and len(summary["cells"]) == 4
    cell = next(c for c in summary["cells"] if c["arm"] == "tuned_h5py" and c["measure"] == "first_byte")
    assert cell["n"] == 2 and cell["first_byte_seconds"] > 0 and cell["chunk_id_seconds"] is not None
    assert "time to first byte, in parts" in text and "`virtual_icechunk` †" in text and "`tuned_h5py` |" in text
    assert "Failed trials" in text and "≥ " in text
    assert (tmp_path / "summary.md").read_text() == text


def test_in_region_and_out_of_region_rows_never_share_a_cell(tmp_path):
    inside = [timed("tuned_h5py", 2.0, rep) for rep in range(2)]
    outside = [timed("tuned_h5py", 20.0, rep) for rep in range(2)]
    for row in outside:
        row.in_region = False
    write_rows(tmp_path, inside + outside)
    cells = report.summarize(report.load(tmp_path))["cells"]
    assert sorted((cell["in_region"], cell["end_to_end_seconds"]) for cell in cells) == [(False, 20.0), (True, 2.0)]


# --- paired speedups --------------------------------------------------------------


def speedup(summary, from_arm, to_arm, from_variant=""):
    matches = [
        s for s in summary["speedups"]
        if (s["from_arm"], s["from_variant"], s["to_arm"]) == (from_arm, from_variant, to_arm)
    ]  # fmt: skip
    assert len(matches) == 1
    return matches[0]


def test_speedups_pair_trials_by_session_and_repetition(tmp_path):
    rows = []
    for rep in range(10):
        noise = 1 + rep / 100
        rows.append(timed("virtual_icechunk", 2.0 * noise, rep))
        rows.append(timed("tuned_h5py", 6.0 * noise, rep))
        rows.append(timed("tuned_h5py", 18.0 * noise, rep, variant={"max_workers": 1}))
    rows.append(timed("tuned_h5py", 600.0, rep=10))  # no virtual trial to pair with: left out
    write_rows(tmp_path, rows)
    summary = report.summarize(report.load(tmp_path))
    against_virtual = speedup(summary, "tuned_h5py", "virtual_icechunk")
    assert against_virtual["speedup_median"] == pytest.approx(3.0) and against_virtual["n"] == 10
    assert against_virtual["ci_low"] == pytest.approx(3.0) and against_virtual["ci_high"] == pytest.approx(3.0)
    # one session: there is no spread between sessions to judge the speedup against
    assert against_virtual["sessions"] == 1
    assert against_virtual["detectable"] is None and against_virtual["session_spread"] is None
    # a variant is also compared with its own arm's defaults: what the tuning factor alone is worth
    one_worker = '{"max_workers": 1}'
    assert speedup(summary, "tuned_h5py", "tuned_h5py", one_worker)["speedup_median"] == pytest.approx(3.0)
    assert speedup(summary, "tuned_h5py", "virtual_icechunk", one_worker)["speedup_median"] == pytest.approx(9.0)
    text = report.render(summary)
    assert "## tempo_no2: paired speedups" in text
    assert '| `tuned_h5py` {"max_workers": 1} | `tuned_h5py` | **3.00×** |' in text
    assert report.ONE_SESSION in text and "inside the spread between sessions" not in text


def test_the_interval_is_on_the_median_it_is_printed_beside(tmp_path):
    rows = []
    for rep, slow in enumerate([2.0] * 11 + [200.0]):  # one outlier: the median stays 1, the mean does not
        rows += [timed("virtual_icechunk", 2.0, rep), timed("tuned_h5py", slow, rep)]
    write_rows(tmp_path, rows)
    result = speedup(report.summarize(report.load(tmp_path)), "tuned_h5py", "virtual_icechunk")
    assert result["speedup_median"] == pytest.approx(1.0)
    assert result["ci_low"] <= result["speedup_median"] <= result["ci_high"] < 2


def test_a_trial_written_twice_counts_once(tmp_path):
    """A session started again under the same name appends to the same file."""
    rows = []
    for rep in range(3):
        rows += [timed("virtual_icechunk", 2.0, rep), timed("tuned_h5py", 6.0, rep)]
    again = [timed("virtual_icechunk", 1.0, rep) for rep in range(3)]  # the later rows are the ones kept
    write_rows(tmp_path, rows + again)
    summary = report.summarize(report.load(tmp_path))
    assert summary["rows"] == 9 and summary["repeated"] == 3
    result = speedup(summary, "tuned_h5py", "virtual_icechunk")
    assert result["n"] == 3 and result["speedup_median"] == pytest.approx(6.0)
    cell = next(c for c in summary["cells"] if c["arm"] == "virtual_icechunk")
    assert cell["n"] == 3 and cell["end_to_end_seconds"] == 1.0
    assert "3 rows repeat an earlier trial" in report.render(summary)


def test_a_speedup_inside_the_spread_between_sessions_is_not_detectable(tmp_path):
    rows = []
    for session, slow in (("1", 2.2), ("2", 1.8)):  # the baseline wins one session and loses the other
        for rep in range(5):
            rows += [timed("virtual_icechunk", 2.0, rep, session), timed("tuned_h5py", slow, rep, session)]
    write_rows(tmp_path, rows)
    summary = report.summarize(report.load(tmp_path))
    result = speedup(summary, "tuned_h5py", "virtual_icechunk")
    assert result["n"] == 10 and not result["detectable"]
    assert "inside the spread between sessions" in report.render(summary)


# --- figures ----------------------------------------------------------------------


def test_report_draws_one_figure_per_case_with_two_or_more_arms(tmp_path):
    rows = [timed(arm, seconds, 0) for arm, seconds in (("virtual_icechunk", 4.7), ("tuned_h5py", 8.1))]
    rows.append(timed("tuned_h5py", 26.4, 0, variant={"max_workers": 1}))
    alone = timed("download", 60.0, 0)
    alone.workload, alone.case_size = "pancake", 15
    write_rows(tmp_path, [*rows, alone])
    report.write_report(tmp_path)
    assert sorted(path.name for path in (tmp_path / "figures").iterdir()) == [
        "tempo_no2_box_100_1d.png",
        "tempo_no2_box_100_1d.svg",
    ]
    svg = (tmp_path / "figures" / "tempo_no2_box_100_1d.svg").read_text()
    # text stays text, so the deck can set it in its own font
    for label in ("Tuned h5py, 1 process", "Tuned h5py", "Virtual store", "26.4 s", "100 km box over 15 granules"):
        assert label in svg, label


def test_in_region_and_out_of_region_cells_never_share_a_figure(tmp_path):
    rows = []
    for in_region, scale in ((True, 1.0), (False, 10.0)):
        for arm, seconds in (("virtual_icechunk", 4.0), ("tuned_h5py", 8.0)):
            rows.append(timed(arm, seconds * scale, 0, in_region=in_region))
    write_rows(tmp_path, rows)
    report.write_report(tmp_path)
    names = sorted(path.name for path in (tmp_path / "figures").glob("*.svg"))
    assert names == ["tempo_no2_box_100_1d_in-region.svg", "tempo_no2_box_100_1d_out-of-region.svg"]
    inside = (tmp_path / "figures" / names[0]).read_text()
    assert "8.0 s" in inside and "80.0 s" not in inside


def test_no_figures_when_asked(tmp_path):
    write_rows(tmp_path, [timed("virtual_icechunk", 4.7, 0), timed("tuned_h5py", 8.1, 0)])
    report.write_report(tmp_path, figures=False)
    assert not (tmp_path / "figures").exists()


def test_figure_labels_and_names():
    assert figures.bar_label("download", "") == "Whole-file download"
    assert figures.bar_label("tuned_h5py", '{"max_workers": 8}') == "Tuned h5py, 8 processes"
    assert figures.bar_label("virtual_icechunk", '{"zarr_concurrency": 4}') == "Virtual store, zarr_concurrency=4"
    cell = {"dataset": "tempo_no2", "transport": "https", "workload": "churro", "case_size": 10, "window": "1d"}
    assert figures.figure_name(cell) == "tempo_no2-https_churro_10_1d.svg"
    assert figures.setup_and_read({"auth_seconds": None, "manifest_seconds": 1.5, "end_to_end_seconds": 4.0}) == (
        1.5,
        2.5,
    )


# --- results from the first TEMPO benchmark ---------------------------------------


@pytest.fixture
def first_benchmark_run(tmp_path):
    shutil.copy(FIRST_BENCHMARK_RUN / "results.jsonl", tmp_path / "results.jsonl")
    return tmp_path


def test_first_benchmark_rows_are_upgraded_on_load(first_benchmark_run):
    frame = report.load(first_benchmark_run)
    assert set(frame["arm"]) == {"user_stack", "tuned_h5py", "virtual_icechunk"}  # no noop, catalog or checker rows
    assert set(frame["workload"]) == {"churro", "box"} and set(frame["measure"]) == {"full"}
    assert (frame["repetition"] >= 0).all() and len(frame) == 71  # the two warm-up rows are gone
    assert frame["in_region"].all() and set(frame["dataset"]) == {"tempo_no2"}
    assert frame["discovery_seconds"].nunique() == 1 and frame["discovery_seconds"].iloc[0] > 0
    old = [json.loads(line) for line in (first_benchmark_run / "results.jsonl").read_text().splitlines()]
    tuned = next(row for row in old if row["arm"] == "netcdf_tuned")
    upgraded = frame[frame["arm"] == "tuned_h5py"].iloc[0]
    assert upgraded["manifest_seconds"] == tuned["open_seconds"]
    assert upgraded["byte_seconds"] == tuned["retrieval_seconds"]
    assert upgraded["last_byte_seconds"] == pytest.approx(tuned["open_seconds"] + tuned["retrieval_seconds"])
    assert pd.isna(upgraded["auth_seconds"]) and "did not time it apart" in upgraded["auth_reason"]


def test_first_benchmark_run_reports_like_any_other(first_benchmark_run):
    text = report.write_report(first_benchmark_run)
    summary = json.loads((first_benchmark_run / "summary.json").read_text())
    assert summary["failed"] == 1 and "timeout" in summary["failures"][0]["status"]
    kinds = {(cell["arm"], cell["request_count_kind"]) for cell in summary["cells"]}
    assert {("virtual_icechunk", "static-minimum"), ("tuned_h5py", "client-ranges")} <= kinds
    assert {s["from_arm"] for s in summary["speedups"]} == {"user_stack", "tuned_h5py"}
    assert "## tempo_no2: paired speedups" in text and "Client: in region" in text
    assert (first_benchmark_run / "figures" / "tempo_no2_box_100_1d.svg").exists()


def test_rows_this_harness_writes_are_not_taken_for_old_ones():
    assert not legacy.is_legacy(json.loads(Row(arm="download", measure="full").to_json()))


# --- the committed runs -----------------------------------------------------------

COMMITTED_RUNS = sorted(
    path.parent for path in (Path(__file__).parents[2] / "results" / "trade_study").glob("*/results.jsonl")
)


@pytest.mark.parametrize("run_dir", COMMITTED_RUNS, ids=lambda path: path.name)
def test_a_committed_summary_is_what_report_writes_today(run_dir):
    """``summary.md`` is committed so the tables can be read without running
    anything. If this fails, run ``run.py report <run dir> --no-figures`` and
    commit the result."""
    assert COMMITTED_RUNS
    assert (run_dir / "summary.md").read_text() == report.render(report.summarize(report.load(run_dir)))
