"""Helpers for datasets on a regular latitude/longitude grid.

A manifest describes such a grid by the first value, last value and size of
each axis (:func:`grid_spec`), so no coordinate file is committed.
"""

from __future__ import annotations

import numpy as np

GRID_TOLERANCE_DEG = 1e-4
GridSpec = dict[str, dict[str, float | int]]


def grid_spec(latitudes: np.ndarray, longitudes: np.ndarray) -> GridSpec:
    """First, last and size per axis. Raises if an axis is not evenly spaced."""
    spec: GridSpec = {}
    axes = (("latitude", np.asarray(latitudes, dtype=float)), ("longitude", np.asarray(longitudes, dtype=float)))
    for name, values in axes:
        rebuilt = np.linspace(values[0], values[-1], len(values))
        worst = float(np.abs(rebuilt - values).max())
        if worst > GRID_TOLERANCE_DEG:
            raise ValueError(f"{name} axis is not regular: max deviation {worst:.2e} deg")
        spec[name] = {"first": float(values[0]), "last": float(values[-1]), "size": len(values)}
    return spec


def grid_arrays(spec: GridSpec) -> tuple[np.ndarray, np.ndarray]:
    """(latitudes, longitudes) rebuilt from a :func:`grid_spec`."""
    lat, lon = spec["latitude"], spec["longitude"]
    return (
        np.linspace(lat["first"], lat["last"], int(lat["size"])),
        np.linspace(lon["first"], lon["last"], int(lon["size"])),
    )


def coordinate_slice(coordinates: np.ndarray, low: float, high: float) -> slice:
    """The index range of the coordinates inside [low, high]."""
    indices = np.flatnonzero((coordinates >= low) & (coordinates <= high))
    if not len(indices):
        raise ValueError(f"No coordinates in [{low}, {high}]")
    return slice(int(indices[0]), int(indices[-1]) + 1)


def nearest_indices(coordinates: np.ndarray, requested: np.ndarray) -> np.ndarray:
    """The index of the coordinate nearest each requested value, on an
    ascending or descending axis."""
    ascending = coordinates[0] <= coordinates[-1]
    values = coordinates if ascending else coordinates[::-1]
    right = np.searchsorted(values, requested).clip(1, len(values) - 1)
    left = right - 1
    chosen = np.where(np.abs(values[right] - requested) < np.abs(values[left] - requested), right, left)
    return chosen if ascending else len(values) - 1 - chosen
