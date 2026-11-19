"""The datasets the harness can read. One module per dataset; see ``base.py`` to add one."""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from benchmarks.trade_study.datasets.base import Dataset
from benchmarks.trade_study.datasets.goes19 import Goes19
from benchmarks.trade_study.datasets.tempo_no2 import TempoNo2

DATASETS: dict[str, type[Dataset]] = {d.name: d for d in (Goes19, TempoNo2)}


def get_dataset(name: str, transport: str = "s3") -> Dataset:
    cls = DATASETS[name]
    if transport not in cls.transports:
        raise SystemExit(f"{name} has no {transport!r} transport; choose from {list(cls.transports)}")
    return cls(transport) if len(cls.transports) > 1 else cls()


def timed_discovery(dataset: Dataset, window: str, reps: int = 3) -> tuple[list[dict[str, Any]], float]:
    """(granules, median seconds over ``reps`` searches). Network."""
    seconds, granules = [], []
    for _ in range(reps):
        start = time.perf_counter()
        granules = dataset.discover(window)
        seconds.append(time.perf_counter() - start)
    return granules, float(np.median(seconds))
