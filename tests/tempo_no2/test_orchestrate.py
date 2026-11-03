import sys
import time
from pathlib import Path

from benchmarks.tempo_no2.measure import TrialRow
from benchmarks.tempo_no2.orchestrate import HostSampler, estimate, main, plan, run_matrix

SMOKE = Path(__file__).parent.parent / "fixtures" / "smoke-results.jsonl"


def smoke_rows():
    return [TrialRow.from_json(line) for line in SMOKE.read_text().splitlines()]


def test_plan_headline_counts():
    p = plan(headline_only=True)
    assert len(p) == 4 * 3 * 10 * 2 + 4 * 3 + 1 + 1 + 10  # trials + warm-ups + manifeststore check + noop + 1-worker factor
    assert p[0].arm == "noop"
    assert all(s.cache_state == "warm_process" for s in p if s.rep == -1)
    assert sum(1 for s in p if s.settings.get("max_workers") == 1) == 10


def test_plan_is_shuffled_within_blocks_deterministically():
    assert plan(True) == plan(True)
    arms = [s.arm for s in plan(True) if s.rep >= 0 and s.arm != "noop"][:6]
    assert arms != sorted(arms) or arms != ["netcdf_tuned", "netcdf_user_stack", "virtual_icechunk"] * 2


def test_estimate_scales_by_observations():
    e = estimate(plan(True), smoke_rows(), {"1d": 12, "1w": 84, "1m": 360}, 0.192)
    assert e.trials == 264 and e.requests_total > 0 and e.usd > 0 and e.hours > 0
    assert set(e.requests_by_bucket) == {"asdc", "mirror"}


def test_orchestrate_keeps_failed_trial_as_row(tmp_path):
    specs = plan(True)[:3]
    py = sys.executable
    cmds = {
        0: [py, "-c", "print('{bad json')"],
        1: [py, "-c", "import sys; sys.exit(3)"],
        2: [py, "-c", "import time; time.sleep(5)"],
    }
    failed = run_matrix(specs, "r", "1", tmp_path, trial_command=lambda s: cmds[specs.index(s)], timeout_s=1)
    rows = [TrialRow.from_json(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert failed == 3 and len(rows) == 3
    assert all(r.status.startswith("subprocess:") for r in rows)
    assert all((tmp_path / "timelines" / f"{i}.json").exists() for i in range(3))
    assert rows[0].arm == specs[0].arm and rows[0].run_id == "r" and rows[0].session_id == "1"


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
