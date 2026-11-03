import sys
import time
from pathlib import Path

from benchmarks.tempo_no2.measure import TrialRow
from benchmarks.tempo_no2.orchestrate import HostSampler, estimate, main, plan, run_matrix

SMOKE = Path(__file__).parent.parent / "fixtures" / "smoke-results.jsonl"
ALL_TIMED = ("netcdf_user_stack", "netcdf_tuned", "virtual_icechunk")


def smoke_rows():
    return [TrialRow.from_json(line) for line in SMOKE.read_text().splitlines()]


def test_plan_headline_counts():
    p = plan(headline_only=True)
    # two default arms: trials + manifeststore check + noop + 1-worker factor + catalog timings (one window at 3 reps)
    assert len(p) == 4 * 2 * 10 * 2 + 1 + 1 + 10 + 3
    assert not any(s.arm == "netcdf_user_stack" for s in p)
    assert p[0].arm == "noop"
    assert not any(s.rep == -1 for s in p)  # warm-ups happen inside the warm_process trial itself
    assert sum(1 for s in p if s.settings.get("max_workers") == 1) == 10
    assert sum(1 for s in p if s.arm == "catalog") == 3


def test_default_command_maps_catalog_to_its_subcommand():
    from benchmarks.tempo_no2.orchestrate import TrialSpec, default_command
    from benchmarks.tempo_no2.cases import Case

    cmd = default_command(TrialSpec("catalog", Case("point", 1, "1d"), 2, "cold"), "r", "1")
    assert "catalog" in cmd and "--rep" in cmd and "1" == cmd[cmd.index("--rep") + 1] and "--window" in cmd


def test_plan_is_shuffled_within_blocks_deterministically():
    assert plan(True) == plan(True)
    arms = [s.arm for s in plan(True, arms=ALL_TIMED) if s.rep >= 0 and s.arm != "noop"][:6]
    assert arms != sorted(arms) or arms != ["netcdf_tuned", "netcdf_user_stack", "virtual_icechunk"] * 2


def test_estimate_scales_by_observations_and_chunks_not_points(lat, lon):
    from benchmarks.tempo_no2.cases import prepare_spatial_cases

    spatial = prepare_spatial_cases(lat, lon)
    e = estimate(plan(True, arms=ALL_TIMED), smoke_rows(), {"1d": 12, "1w": 84, "1m": 360}, 0.192, spatial=spatial)
    assert e.trials == 255 and e.requests_total > 0 and e.usd > 0 and e.hours > 0
    assert set(e.requests_by_bucket) == {"asdc", "mirror"}
    # smoke rows: ~2 s per single-observation trial; a day is 12 observations and 250
    # points touch a few chunks, not 250× the work. The whole headline matrix is well under 3 h.
    assert e.hours < 3
    one = estimate([s for s in plan(True, arms=ALL_TIMED) if s.case.case_size == 10 and s.case.workload == "point"][:1], smoke_rows(), {"1d": 12}, 0.192, spatial=spatial)
    big = estimate([s for s in plan(True, arms=ALL_TIMED) if s.case.case_size == 250 and s.case.workload == "point"][:1], smoke_rows(), {"1d": 12}, 0.192, spatial=spatial)
    assert big.hours < 10 * one.hours


def test_orchestrate_keeps_failed_trial_as_row(tmp_path):
    specs = plan(True)[:3]
    py = sys.executable
    specs = plan(True)[:5]
    cmds = {
        0: [py, "-c", "print('{bad json')"],
        1: [py, "-c", "import sys; sys.exit(3)"],
        2: [py, "-c", "import time; time.sleep(5)"],
        3: [py, "-c", "print('null')"],  # valid JSON, not a row
        4: [py, "-c", "import json; print(json.dumps({'status': 'RuntimeError: no creds'}))"],  # row without an arm
    }
    failed = run_matrix(specs, "r", "1", tmp_path, trial_command=lambda s: cmds[specs.index(s)], timeout_s=1)
    rows = [TrialRow.from_json(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert failed == 5 and len(rows) == 5
    assert all(r.status.startswith("subprocess:") for r in rows[:4])
    assert all((tmp_path / "timelines" / f"{i}.json").exists() for i in range(5))
    assert [r.arm for r in rows] == [s.arm for s in specs]
    assert rows[4].status == "RuntimeError: no creds" and rows[4].arm_settings == dict(specs[4].settings)
    assert rows[0].run_id == "r" and rows[0].session_id == "1"


def test_host_sampler_produces_timeline():
    s = HostSampler(interval_s=0.05)
    s.start()
    time.sleep(0.2)
    s.stop()
    tl = s.timeline()
    assert len(tl) >= 2
    assert {"t", "cpu_busy_pct_mean", "cpu_busy_pct_max_core", "net_rx_bytes_per_s", "steal_pct"} <= set(tl[0])


def test_main_refuses_without_yes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["--run-id", "r", "--session", "1", "--headline-only", "--smoke-results", str(SMOKE)])
    assert rc == 2
    assert "re-run with --yes" in capsys.readouterr().out


def test_default_command_threads_access_through():
    from benchmarks.tempo_no2.cases import Case
    from benchmarks.tempo_no2.orchestrate import TrialSpec, default_command

    cmd = default_command(TrialSpec("netcdf_tuned", Case("point", 1, "1d"), 0, "cold"), "r", "1", access="https")
    assert cmd[cmd.index("--access") + 1] == "https"


def test_plan_can_add_the_user_stack():
    from benchmarks.tempo_no2.orchestrate import plan

    p = plan(headline_only=True, arms=ALL_TIMED)
    assert any(s.arm == "netcdf_user_stack" for s in p)
    assert len(p) == 4 * 3 * 10 * 2 + 1 + 1 + 10 + 3


def test_main_accepts_an_arms_list(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["--run-id", "r", "--headline-only", "--smoke-results", str(SMOKE), "--arms", ",".join(ALL_TIMED), "--estimate"])
    assert rc == 0 and "trials: 255" in capsys.readouterr().out


def test_main_limit_runs_only_the_first_trials(tmp_path, capsys, monkeypatch):
    import benchmarks.tempo_no2.orchestrate as orch

    monkeypatch.chdir(tmp_path)
    seen = []

    def fake_run_matrix(specs, run_id, session, out_dir, trial_command=None, timeout_s=0):
        seen.extend(specs)
        return 0

    monkeypatch.setattr(orch, "run_matrix", fake_run_matrix)
    rc = main(["--run-id", "r", "--headline-only", "--smoke-results", str(SMOKE), "--yes", "--limit", "10", "--out-root", str(tmp_path)])
    assert rc == 0 and len(seen) == 10 and seen[0].arm == "noop"
    assert "limit: running 10 of" in capsys.readouterr().out
