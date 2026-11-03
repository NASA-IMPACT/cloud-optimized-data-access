"""Pinned configuration, spatial cases, time windows and the trial matrix.

The spatial functions are the notebook's (tempo-virtual-zarr-pipeline,
exploration/benchmark_no2_event.ipynb) moved out unchanged. Discovery is
the only network-bound function here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BenchmarkConfig:
    concept_id: str = "C3685896708-LARC_CLOUD"
    event_start: str = "2025-03-14T00:00:00Z"
    center_lat: float = 36.98
    center_lon: float = -95.97
    aoi_sizes_km: tuple[int, ...] = (50, 100, 250, 500)
    point_counts: tuple[int, ...] = (1, 10, 50, 250)
    random_seed: int = 20250314
    accepted_quality_flag: int = 0  # V04: normal=0, suspicious=1, bad=2.
    mirror_bucket: str = "us-west-2.opendata.source.coop"
    mirror_prefix: str = "pangeo/tempo-virtual-icechunk/tempo/no2/v04"
    region: str = "us-west-2"
    snapshot_tag: str = "bench-no2-2025-03-14"
    time_tolerance_minutes: int = 4


CONFIG = BenchmarkConfig()

WINDOWS: dict[str, tuple[str, str]] = {
    "1d": ("2025-03-14T00:00:00Z", "2025-03-15T00:00:00Z"),
    "1w": ("2025-03-14T00:00:00Z", "2025-03-21T00:00:00Z"),
    "1m": ("2025-03-14T00:00:00Z", "2025-04-14T00:00:00Z"),
}

MANIFEST_DIR = Path(__file__).parent / "manifests"


@dataclass(frozen=True)
class Case:
    workload: Literal["point", "aoi"]
    case_size: int
    window: str

    def __str__(self) -> str:
        return f"{self.workload}:{self.case_size}:{self.window}"


@dataclass
class SpatialCases:
    latitudes: np.ndarray
    longitudes: np.ndarray
    points: pd.DataFrame
    windows: dict[int, tuple[slice, slice]]


def aoi_bounds(size_km: float) -> tuple[float, float, float, float]:
    half = size_km / 2
    delta_lat = half / 110.574
    delta_lon = half / (111.320 * np.cos(np.deg2rad(CONFIG.center_lat)))
    return (
        CONFIG.center_lat - delta_lat,
        CONFIG.center_lat + delta_lat,
        CONFIG.center_lon - delta_lon,
        CONFIG.center_lon + delta_lon,
    )


def coordinate_slice(coordinates: np.ndarray, low: float, high: float) -> slice:
    indices = np.flatnonzero((coordinates >= low) & (coordinates <= high))
    if not len(indices):
        raise ValueError(f"No coordinates in [{low}, {high}]")
    return slice(int(indices[0]), int(indices[-1]) + 1)


def nearest_indices(coordinates: np.ndarray, requested: np.ndarray) -> np.ndarray:
    ascending = coordinates[0] <= coordinates[-1]
    values = coordinates if ascending else coordinates[::-1]
    right = np.searchsorted(values, requested).clip(1, len(values) - 1)
    left = right - 1
    chosen = np.where(
        np.abs(values[right] - requested) < np.abs(values[left] - requested),
        right,
        left,
    )
    return chosen if ascending else len(values) - 1 - chosen


def prepare_spatial_cases(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    windows_override: dict[int, tuple[slice, slice]] | None = None,
) -> SpatialCases:
    """Seeded point sample and AOI index windows on the given grid.

    ``windows_override`` lets tests on a tiny grid supply index windows
    directly instead of deriving them from the real-world AOI sizes.
    """
    latitudes = np.asarray(latitudes)
    longitudes = np.asarray(longitudes)
    if windows_override is None:
        outer = aoi_bounds(max(CONFIG.aoi_sizes_km))
        windows = {
            size: (
                coordinate_slice(latitudes, *aoi_bounds(size)[:2]),
                coordinate_slice(longitudes, *aoi_bounds(size)[2:]),
            )
            for size in CONFIG.aoi_sizes_km
        }
    else:
        windows = dict(windows_override)
        lat_slice, lon_slice = windows[max(windows)]
        outer = (
            float(latitudes[lat_slice].min()),
            float(latitudes[lat_slice].max()),
            float(longitudes[lon_slice].min()),
            float(longitudes[lon_slice].max()),
        )
    rng = np.random.default_rng(CONFIG.random_seed)
    n = max(CONFIG.point_counts)
    points = pd.DataFrame(
        {
            "point_id": np.arange(n),
            "requested_lat": rng.uniform(outer[0], outer[1], n),
            "requested_lon": rng.uniform(outer[2], outer[3], n),
        }
    )
    points["lat_index"] = nearest_indices(latitudes, points["requested_lat"].to_numpy())
    points["lon_index"] = nearest_indices(longitudes, points["requested_lon"].to_numpy())
    points["pixel_lat"] = latitudes[points["lat_index"]]
    points["pixel_lon"] = longitudes[points["lon_index"]]
    return SpatialCases(latitudes, longitudes, points, windows)


HEADLINE_CELLS: tuple[Case, ...] = (
    Case("point", 10, "1d"),
    Case("aoi", 100, "1d"),
    Case("point", 250, "1d"),
    Case("aoi", 500, "1d"),
)


def matrix(headline_only: bool) -> list[Case]:
    if headline_only:
        return list(HEADLINE_CELLS)
    cases = [Case("point", n, "1d") for n in CONFIG.point_counts]
    cases += [Case("aoi", s, "1d") for s in CONFIG.aoi_sizes_km]
    for window in ("1w", "1m"):
        cases += [Case("point", 10, window), Case("aoi", 100, window)]
    return cases


def manifest_path(window: str) -> Path:
    return MANIFEST_DIR / f"no2-2025-03-14-{window}.json"


def discover(window: str) -> pd.DataFrame:
    """CMR discovery of the granules in ``window``. Network."""
    import earthaccess

    earthaccess.login(strategy="all", persist=True)
    granules = earthaccess.search_data(
        concept_id=CONFIG.concept_id, temporal=WINDOWS[window], count=-1, sort_key="start_date"
    )
    records: list[dict[str, Any]] = []
    for granule in granules:
        umm = granule["umm"]
        temporal = umm["TemporalExtent"]["RangeDateTime"]
        archive = umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation", [])
        netcdf_file = next((i for i in archive if i.get("Name", "").endswith(".nc")), {})
        links = [u for u in granule.data_links(access="direct") if u.endswith(".nc")]
        if not links:
            raise RuntimeError(f"No direct S3 netCDF link for {granule['meta']['concept-id']}")
        records.append(
            {
                "granule_ur": umm["GranuleUR"],
                "concept_id": granule["meta"]["concept-id"],
                "revision_id": granule["meta"].get("revision-id"),
                "start_time": temporal["BeginningDateTime"],
                "end_time": temporal["EndingDateTime"],
                "source_url": links[0],
                "source_size_mb": netcdf_file.get("Size"),
            }
        )
    if not records:
        raise RuntimeError(f"CMR returned no granules for window {window}")
    return pd.DataFrame(records).sort_values("start_time").reset_index(drop=True)
