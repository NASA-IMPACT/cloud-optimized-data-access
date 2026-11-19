"""Offline checks that the committed DMR++ results are what the code writes today."""

from __future__ import annotations

from pathlib import Path

import pytest

from dmrpp import survey as stale

SURVEY = Path(__file__).parents[2] / "results" / "dmrpp" / "survey"
INVENTORY = Path(__file__).parents[2] / "benchmarks" / "cost" / "data" / "cmr_cloud_inventory.csv"


@pytest.mark.parametrize("command", ["report", "sample"])
def test_the_committed_survey_files_are_what_the_survey_writes_today(command, tmp_path, capsys):
    """collections.csv, stale_granules.csv, summary.md and sampled_with_sidecars.csv
    are derived from granules.jsonl.gz. If this fails, run ``python -m dmrpp.survey
    report`` and ``sample`` and commit the result. The order of the sampled file
    matters: the verifier numbers granules in the order it reads them."""
    (tmp_path / "granules.jsonl.gz").write_bytes((SURVEY / "granules.jsonl.gz").read_bytes())
    assert stale.main([command, str(tmp_path)]) == 0
    written = sorted(path.name for path in tmp_path.iterdir() if path.name != "granules.jsonl.gz")
    assert (
        written
        == {"report": ["collections.csv", "stale_granules.csv", "summary.md"], "sample": ["sampled_with_sidecars.csv"]}[
            command
        ]
    )
    for name in written:
        assert (tmp_path / name).read_bytes() == (SURVEY / name).read_bytes(), name


def test_the_committed_inventory_is_the_one_the_survey_sampled_from():
    """The survey can only be resumed from this branch if its input is here."""
    import csv

    todo = stale.candidates(INVENTORY, None)
    assert len(todo) == 5617 and {"concept_id", "provider", "short_name", "version"} <= set(todo[0])
    in_inventory = {row["concept_id"] for row in todo}
    with (SURVEY / "collections.csv").open() as f:
        surveyed = {row["concept_id"] for row in csv.DictReader(f)}
    assert len(surveyed) == 3941 and surveyed <= in_inventory
    assert len(stale.candidates(INVENTORY, {"POCLOUD"})) < len(todo)
