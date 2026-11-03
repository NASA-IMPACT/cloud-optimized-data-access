"""Shared fixtures. No test may open a network connection."""

import numpy as np
import pytest


@pytest.fixture(scope="session")
def lat():
    return np.linspace(17, 64, 2950)


@pytest.fixture(scope="session")
def lon():
    return np.linspace(-170, -14, 7750)
