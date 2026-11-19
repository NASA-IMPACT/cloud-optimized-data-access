"""The questions every dataset is asked.

A question is asked in coordinates, not grid indices: a seeded set of points,
or a box of a stated side length, both around one centre. Each dataset turns
the coordinates into its own indices (a :class:`Selection`), so two datasets
answer the same question on the ground.

A :class:`Case` names a question as ``workload:size:window``, for example
``churro:10:1d``: a time series at 10 points over one day. The workloads and
their sizes are listed in :mod:`benchmarks.trade_study.scope`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from benchmarks.trade_study.scope import WORKLOAD_INFO

# Tulsa, Oklahoma: the centre of the 14 March 2025 NO₂ event the first benchmark studied.
CENTER_LAT, CENTER_LON = 36.98, -95.97
POINTS_SEED = 20250314
POINTS_BOX_KM = 500  # the points are drawn inside a box of this side
MAX_POINTS = 250
KM_PER_DEGREE_LAT, KM_PER_DEGREE_LON_AT_EQUATOR = 110.574, 111.320


@dataclass(frozen=True)
class Case:
    """One question.

    - ``pancake``: the whole map, for the first ``size`` observations;
    - ``churro``: a time series at the first ``size`` points;
    - ``box``: every value in a ``size``-km box, over the window;
    - ``batch``: every chunk of the window (``size`` is unused).
    """

    workload: Literal["pancake", "churro", "box", "batch"]
    size: int
    window: str

    def __str__(self) -> str:
        return f"{self.workload}:{self.size}:{self.window}"


def parse_case(text: str) -> Case:
    workload, size, window = text.split(":")
    if workload not in WORKLOAD_INFO:
        raise ValueError(f"bad case {text!r}: workload must be one of {sorted(WORKLOAD_INFO)}")
    return Case(workload, int(size), window)  # type: ignore[arg-type]


def headline_cases(window: str) -> list[Case]:
    """Every workload at every size the scope lists, over ``window``."""
    return [Case(w.name, size, window) for w in WORKLOAD_INFO.values() for size in w.sizes]  # type: ignore[arg-type]


@dataclass
class Selection:
    """Grid indices for a question: paired point indices, or a (y, x) window."""

    kind: Literal["points", "window"]
    y: Any
    x: Any

    def first(self) -> Selection:
        """The first value of the answer: one grid cell."""
        if self.kind == "points":
            return Selection("points", np.asarray(self.y)[:1], np.asarray(self.x)[:1])
        return Selection("window", slice(self.y.start, self.y.start + 1), slice(self.x.start, self.x.start + 1))


def box_bounds(size_km: float) -> tuple[float, float, float, float]:
    """(south, north, west, east) of a ``size_km`` box around the centre, in degrees."""
    half = size_km / 2
    delta_lat = half / KM_PER_DEGREE_LAT
    delta_lon = half / (KM_PER_DEGREE_LON_AT_EQUATOR * np.cos(np.deg2rad(CENTER_LAT)))
    return CENTER_LAT - delta_lat, CENTER_LAT + delta_lat, CENTER_LON - delta_lon, CENTER_LON + delta_lon


def question_points(n: int = MAX_POINTS) -> tuple[np.ndarray, np.ndarray]:
    """(lats, lons) of the first ``n`` question points. The sample is seeded and
    always drawn whole, so the 10-point set is the first 10 of the 250."""
    south, north, west, east = box_bounds(POINTS_BOX_KM)
    rng = np.random.default_rng(POINTS_SEED)
    lats = rng.uniform(south, north, MAX_POINTS)
    lons = rng.uniform(west, east, MAX_POINTS)
    return lats[:n], lons[:n]
