"""Shared fixtures. No test may open a network connection."""

import numpy as np
import pytest


@pytest.fixture(scope="session")
def lat():
    return np.linspace(17, 64, 2950)


@pytest.fixture(scope="session")
def lon():
    return np.linspace(-170, -14, 7750)


@pytest.fixture(autouse=True)
def no_imds(monkeypatch):
    """Tests never touch the network: pin the host description instead of probing IMDS."""
    from benchmarks.tempo_no2 import run

    monkeypatch.setattr(run, "_HOST", {
        "cpu_model": "test", "nproc": 4, "mem_total_bytes": 8 * 2**30,
        "instance_type": None, "instance_type_reason": "test", "availability_zone": None, "host_kind": "other",
    })
