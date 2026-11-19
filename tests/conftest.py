"""Shared fixtures. No test may open a network connection."""

from __future__ import annotations

import numpy as np
import pytest

from benchmarks.trade_study.datasets import latlon
from benchmarks.trade_study.datasets.tempo_no2 import TempoNo2
from tests.trade_study import fixtures

FIXTURE_LAT, FIXTURE_LON = np.linspace(20.0, 50.0, 60), np.linspace(-120.0, -70.0, 80)


@pytest.fixture(scope="session")
def lat():
    """TEMPO's real latitude axis."""
    return np.linspace(17, 64, 2950)


@pytest.fixture(scope="session")
def lon():
    """TEMPO's real longitude axis."""
    return np.linspace(-170, -14, 7750)


class FixtureTempo(TempoNo2):
    """TEMPO's layout on local files: no credentials, a local repository."""

    public = True

    def __init__(self, repo):
        self._repo = repo

    def auth(self):
        return None

    def store_spec(self, credentials):
        return {"kind": "local"}

    def filesystem(self, credentials):
        import fsspec

        return fsspec.filesystem("file")

    def open_repo(self, settings):
        return self._repo


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    """(dataset, manifest payload) over three synthetic TEMPO-shaped granules and a local virtual store."""
    tmp = tmp_path_factory.mktemp("trade_study")
    granules = fixtures.make_granules(tmp / "granules", n=3)
    repo, snapshot = fixtures.make_virtual_store(tmp, granules)
    payload = {
        "window": "1d",
        "snapshot_id": snapshot,
        "discovery_seconds": 0.5,
        "index_location": "local-disk",
        "grid": latlon.grid_spec(FIXTURE_LAT, FIXTURE_LON),
        "granules": fixtures.granule_records(granules),
    }
    return FixtureTempo(repo), payload


@pytest.fixture(autouse=True)
def no_imds(monkeypatch):
    """Tests never touch the network: the EC2 metadata service is never asked."""
    from benchmarks.trade_study import host

    monkeypatch.setattr(host, "_imds", lambda path, timeout=0.5: None)
