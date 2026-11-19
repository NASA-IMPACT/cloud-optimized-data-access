"""The correctness gate and the session's plan and subprocess handling."""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest

from benchmarks.trade_study import run, session
from benchmarks.trade_study.arms import Result
from benchmarks.trade_study.gate import compare, gate_key, gate_observations, gate_verdicts, run_gate
from benchmarks.trade_study.questions import Case
from benchmarks.trade_study.session import SessionOptions, TrialSpec, plan, run_session, trial_command
from benchmarks.trade_study.trial import Row

ARMS = ("tuned_h5py", "user_stack", "download", "virtual_icechunk")
OPTIONS = SessionOptions(run_id="t", session="1", instance_rate_usd_h=0.192, timeout_s=30, probes=None)

# --- gate -------------------------------------------------------------------------


def _result(values):
    times = np.array(["2025-03-14T12:00:00"], dtype="datetime64[ns]")
    return Result(times, {"v": np.asarray(values, dtype=float)[None]})


def test_compare_exact_close_vacuous_and_failures():
    assert compare(_result([1.0, np.nan]), _result([1.0, np.nan])) == ("exact", 0.0)
    assert compare(_result([1.0]), _result([1.0 + 1e-8]))[0] == "close"
    assert compare(_result([np.nan]), _result([np.nan]))[0] == "vacuous"
    with pytest.raises(AssertionError, match="values differ"):
        compare(_result([1.0]), _result([1.001]))
    with pytest.raises(AssertionError, match="fill masks"):
        compare(_result([1.0, np.nan]), _result([1.0, 2.0]))
    later = Result(np.array(["2025-03-14T13:00:00"], dtype="datetime64[ns]"), {"v": np.ones((1, 1))})
    with pytest.raises(AssertionError, match="times"):
        compare(_result([1.0]), later)


def test_gate_reads_the_first_middle_and_last_observation():
    assert gate_observations(15) == [0, 7, 14]
    assert gate_observations(1) == [0]


def test_gate_runs_every_arm_against_the_first(world):
    dataset, payload = world
    rows = run_gate(dataset, payload, ARMS, [Case("churro", 10, "1d"), Case("box", 500, "1d")])
    assert len(rows) == 2 * 3 * 3  # cases × observations × non-reference arms
    passed = {"tuned_h5py": "reference", "user_stack": "pass", "download": "pass", "virtual_icechunk": "pass"}
    assert gate_verdicts(rows, "tuned_h5py") == passed


def test_gate_reads_its_observations_as_one_question(world, monkeypatch):
    """The gate must check the code a trial times: several granules at once."""
    from benchmarks.trade_study import arms

    dataset, payload = world
    asked = []
    real_read = arms.TunedH5py.read

    def recording_read(self, observations, selection):
        asked.append((list(observations), self._uses_workers(observations)))
        return real_read(self, observations, selection)

    monkeypatch.setattr(arms.TunedH5py, "read", recording_read)
    rows = run_gate(dataset, payload, ("virtual_icechunk", "tuned_h5py"), [Case("box", 500, "1d")])
    assert asked == [([0, 1, 2], True)]  # one read, through the worker processes
    assert [row["observation"] for row in rows] == [0, 1, 2] and {row["status"] for row in rows} == {"exact"}


def test_a_variant_is_gated_as_an_arm_of_its_own(world):
    import zarr

    dataset, payload = world
    pipeline = zarr.config.get("codec_pipeline.path")
    variants = [("virtual_icechunk", {"zarrs_pipeline": True}), ("tuned_h5py", {"max_workers": 1})]
    rows = run_gate(dataset, payload, ("tuned_h5py", "virtual_icechunk"), [Case("box", 500, "1d")], variants)
    assert zarr.config.get("codec_pipeline.path") == pipeline  # the variant's settings did not leak
    zarrs = gate_key("virtual_icechunk", {"zarrs_pipeline": True})
    assert zarrs == 'virtual_icechunk {"zarrs_pipeline": true}'
    assert gate_verdicts(rows, "tuned_h5py") == {
        "tuned_h5py": "reference",
        "virtual_icechunk": "pass",
        zarrs: "pass",
        'tuned_h5py {"max_workers": 1}': "pass",
    }


def test_a_variant_row_is_not_gated_by_its_arms_verdict(world, tmp_path, monkeypatch):
    dataset, payload = world
    monkeypatch.chdir(tmp_path)
    path = run.paths.gate_path(dataset, "1d")
    run.write_json(path, {"verdicts": {"tuned_h5py": "pass", 'tuned_h5py {"max_workers": 1}': "pass"}, "rows": []})
    statuses = {}
    for variant in ({}, {"max_workers": 1}, {"max_workers": 2}):
        row = Row(arm="tuned_h5py", arm_variant=variant)
        run.stamp(row, dataset, "1d", "t", "1", payload)
        statuses[json.dumps(variant)] = row.gate_status
    assert statuses == {"{}": "pass", '{"max_workers": 1}': "pass", '{"max_workers": 2}': "not gated"}


def test_a_later_gate_keeps_the_verdicts_of_arms_it_did_not_read(world, tmp_path, monkeypatch):
    dataset, payload = world
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run, "get_dataset", lambda name, transport: dataset)
    monkeypatch.setattr(run, "load_manifest", lambda dataset, window: payload)
    gate = ["gate", "--dataset", "tempo_no2", "--case", "box:500:1d"]
    assert run.main([*gate, "--arms", "tuned_h5py,download"]) == 0
    assert run.main([*gate, "--arms", "tuned_h5py", "--variants", 'tuned_h5py={"max_workers": 1}']) == 0
    written = json.loads(run.paths.gate_path(dataset, "1d").read_text())  # filed under the cases' window
    assert written["verdicts"] == {
        "download": "pass",
        "tuned_h5py": "reference",
        'tuned_h5py {"max_workers": 1}': "pass",
    }
    assert {row["arm"] for row in written["rows"]} == {"download", 'tuned_h5py {"max_workers": 1}'}


def test_cases_and_window_must_name_one_window(world, monkeypatch):
    dataset, payload = world
    monkeypatch.setattr(run, "get_dataset", lambda name, transport: dataset)
    monkeypatch.setattr(run, "load_manifest", lambda dataset, window: payload)
    with pytest.raises(SystemExit, match="--window 1w but the cases are over 1d"):
        run.main(["gate", "--dataset", "tempo_no2", "--window", "1w", "--case", "box:500:1d"])
    with pytest.raises(SystemExit, match="more than one window"):
        run.main(["session", "--dataset", "tempo_no2", "--run-id", "t", "--case", "box:1:1d", "--case", "box:1:1w"])
    parse = run.build_parser().parse_args
    assert run.resolve_window(parse(["gate", "--case", "box:500:1d"])) == "1d"
    assert run.resolve_window(parse(["gate", "--window", "1w"])) == "1w"
    assert run.resolve_window(parse(["gate"])) == "1h"


def test_probe_without_range_reads_keeps_the_concurrent_rate(world, tmp_path, monkeypatch, capsys):
    dataset, payload = world
    monkeypatch.setattr(run, "get_dataset", lambda name, transport: dataset)
    monkeypatch.setattr(run, "load_manifest", lambda dataset, window: payload)
    out = tmp_path / "probes.json"
    assert run.main(["probe", "--dataset", "tempo_no2", "--window", "1d", "--range-reads", "0", "--out", str(out)]) == 0
    probes = json.loads(out.read_text())
    assert probes["bandwidth_single_stream_mb_s"] is None
    assert probes["bandwidth_mb_s"] == probes["bandwidth_concurrent_mb_s"] > 0


def test_an_arm_that_cannot_read_fails_the_gate_without_stopping_it(world):
    dataset, payload = world
    missing = [{**g, "source_url": g["source_url"] + ".missing"} for g in payload["granules"]]
    # the virtual arm reads through its store's own references, so it still answers; the file readers cannot
    rows = run_gate(
        dataset, {**payload, "granules": missing}, ("virtual_icechunk", "download"), [Case("box", 500, "1d")]
    )
    assert len(rows) == 3 and all(row["status"].startswith("fail") for row in rows)
    assert gate_verdicts(rows, "virtual_icechunk")["download"] == "fail"


def test_gate_verdicts_fail_and_vacuous():
    rows = [
        {"case": "a", "arm": "x", "status": "exact"},
        {"case": "b", "arm": "x", "status": "fail: boom"},
        {"case": "a", "arm": "y", "status": "vacuous"},
        {"case": "a", "arm": "z", "status": "close"},
    ]
    assert gate_verdicts(rows, "ref") == {"ref": "reference", "x": "fail", "y": "vacuous", "z": "pass"}


def test_the_default_gate_skips_cases_another_case_covers():
    assert [str(case) for case in run.gate_cases("1d")] == [
        "pancake:1:1d",
        "churro:10:1d",
        "churro:250:1d",
        "box:100:1d",
    ]


# --- plan -------------------------------------------------------------------------


def test_plan_runs_every_cell_once_per_repetition_in_shuffled_order():
    cases = [Case("churro", 10, "1d"), Case("box", 100, "1d")]
    variants = [("tuned_h5py", {"max_workers": 1})]
    specs = plan(("tuned_h5py", "download"), cases, reps=3, variants=variants)
    assert len(specs) == 3 * (2 * 2 * 2 + 1)  # arms × cases × measures, + the variant on the box case
    for rep in range(3):
        block = [spec for spec in specs if spec.rep == rep]
        assert len({(s.arm, s.case, s.measure, json.dumps(s.settings)) for s in block}) == len(block)
    assert [spec.arm for spec in specs[:9]] != sorted(spec.arm for spec in specs[:9])
    assert specs == plan(("tuned_h5py", "download"), cases, reps=3, variants=variants)


def test_trial_command_carries_the_spec_and_the_transport(world):
    dataset, _ = world
    dataset.transport = "https"
    try:
        spec = TrialSpec("tuned_h5py", Case("box", 100, "1d"), "full", 2, {"max_workers": 1})
        command = trial_command(dataset, spec, SessionOptions("r", "3", 0.5, 30, "probes.json"))
    finally:
        del dataset.transport
    assert command[1:4] == ["-m", "benchmarks.trade_study.run", "trial"]
    flags = dict(zip(command[4::2], command[5::2], strict=True))
    assert flags == {
        "--dataset": "tempo_no2", "--transport": "https", "--arm": "tuned_h5py", "--case": "box:100:1d",
        "--measure": "full", "--rep": "2", "--run-id": "r", "--session": "3", "--instance-rate-usd-h": "0.5",
        "--settings": '{"max_workers": 1}', "--probes": "probes.json",
    }  # fmt: skip


# --- session ----------------------------------------------------------------------


def _fake_trials(monkeypatch, script):
    """Make every trial subprocess run ``script`` instead of the real trial."""
    monkeypatch.setattr(session, "trial_command", lambda *_: [sys.executable, "-c", script])


def test_session_writes_each_trials_row(world, tmp_path, monkeypatch, capsys):
    row = Row(run_id="t", arm="tuned_h5py", measure="full", end_to_end_seconds=1.5, status="ok")
    _fake_trials(monkeypatch, f"print('noise'); print({row.to_json()!r})")
    specs = [TrialSpec("tuned_h5py", Case("box", 100, "1d"), "full", rep) for rep in range(2)]
    assert run_session(world[0], specs, OPTIONS, tmp_path) == (2, 0)
    written = [Row.from_json(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert written == [row, row]
    assert "[2/2] tuned_h5py box:100:1d full rep 1: 1.50 s" in capsys.readouterr().err


def test_session_keeps_a_crashed_trial_as_a_row(world, tmp_path, monkeypatch):
    _fake_trials(monkeypatch, "import sys; sys.stderr.write('boom'); sys.exit(3)")
    spec = TrialSpec("download", Case("box", 100, "1d"), "full", 0, {"concurrency": 2})
    assert run_session(world[0], [spec], OPTIONS, tmp_path) == (1, 1)
    row = Row.from_json((tmp_path / "results.jsonl").read_text())
    assert row.status == "subprocess: exit 3, unparseable output" and row.error == "boom"
    assert (row.arm, row.workload, row.case_size, row.arm_variant) == ("download", "box", 100, {"concurrency": 2})


def test_session_keeps_a_timed_out_trial_as_a_row(world, tmp_path, monkeypatch):
    _fake_trials(monkeypatch, "import time; time.sleep(30)")
    options = SessionOptions("t", "1", 0.192, timeout_s=1, probes=None)
    spec = TrialSpec("download", Case("box", 100, "1d"), "full", 0)
    assert run_session(world[0], [spec], options, tmp_path) == (1, 1)
    assert Row.from_json((tmp_path / "results.jsonl").read_text()).status == "subprocess: timeout after 1 s"


def test_a_session_run_again_skips_the_trials_it_already_has(world, tmp_path, monkeypatch):
    dataset, _ = world
    specs = [TrialSpec("tuned_h5py", Case("box", 100, "1d"), "full", rep) for rep in range(3)]
    variant = TrialSpec("tuned_h5py", Case("box", 100, "1d"), "full", 0, {"max_workers": 1})
    done = [session._failed_row(dataset, spec, OPTIONS, "", "") for spec in specs[:2]]
    done[0].status, done[1].status = "ok", "subprocess: timeout after 30 s"  # only an ok row is done
    (tmp_path / "results.jsonl").write_text("".join(row.to_json() + "\n" for row in done))
    third = session._failed_row(dataset, specs[2], OPTIONS, "", "")
    third.status, third.end_to_end_seconds = "ok", 1.0
    _fake_trials(monkeypatch, f"print({third.to_json()!r})")
    assert run_session(dataset, [*specs, variant], OPTIONS, tmp_path) == (3, 0)  # rep 0 is skipped
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2 + 3
    other_session = SessionOptions("t", "2", 0.192, 30, None)
    assert run_session(dataset, specs[:1], other_session, tmp_path)[0] == 1  # another session is not a repeat


def test_session_refuses_ungated_variants_and_variants_of_unplanned_arms(world, tmp_path, monkeypatch, capsys):
    dataset, payload = world
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run, "get_dataset", lambda name, transport: dataset)
    monkeypatch.setattr(run, "load_manifest", lambda dataset, window: payload)
    run.write_json(run.paths.gate_path(dataset, "1d"), {"verdicts": {"tuned_h5py": "reference"}, "rows": []})
    arguments = ["session", "--dataset", "tempo_no2", "--window", "1d", "--run-id", "t", "--arms", "tuned_h5py"]
    assert run.main([*arguments, "--variants", 'tuned_h5py={"max_workers": 1}']) == 2
    error = capsys.readouterr().err
    assert """not gated: ['tuned_h5py {"max_workers": 1}']""" in error and "--variants 'tuned_h5py=" in error
    with pytest.raises(SystemExit, match="not in --arms"):
        run.main([*arguments, "--variants", 'download={"concurrency": 2}'])


def test_session_command_refuses_ungated_arms_and_waits_for_yes(world, tmp_path, monkeypatch, capsys):
    dataset, payload = world
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run, "get_dataset", lambda name, transport: dataset)
    monkeypatch.setattr(run, "load_manifest", lambda dataset, window: payload)
    arguments = ["session", "--dataset", "tempo_no2", "--window", "1d", "--run-id", "t", "--arms", "tuned_h5py"]
    assert run.main(arguments) == 2
    assert "not gated: ['tuned_h5py']" in capsys.readouterr().err
    assert run.main([*arguments, "--allow-ungated"]) == 2
    assert "re-run with --yes" in capsys.readouterr().out
    assert not (tmp_path / "results").exists()
