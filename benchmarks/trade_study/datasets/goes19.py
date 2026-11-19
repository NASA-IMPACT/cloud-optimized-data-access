"""GOES-19 ABI L2 Cloud and Moisture Imagery, full disk, band 13.

Public, so anyone can rerun the method without credentials.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from benchmarks.trade_study.datasets.base import (
    STORE_DIR,
    Credentials,
    Dataset,
    Granule,
    Grid,
    concat_granules,
    write_local_repository,
)
from benchmarks.trade_study.stores import build_store, icechunk_config, store_path


def goes_scan_angles(grid: Grid, lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(y, x) scan angles in radians for geodetic points, on the GOES-R fixed
    grid (GOES-R Product User Guide, section 4.2.8.2). NaN where the satellite
    cannot see the point."""
    r_eq, r_pol, height = grid["semi_major_axis"], grid["semi_minor_axis"], grid["perspective_point_height"]
    phi = np.deg2rad(np.asarray(lats, dtype=float))
    lam = np.deg2rad(np.asarray(lons, dtype=float) - grid["longitude_of_projection_origin"])
    e2 = 1 - (r_pol / r_eq) ** 2
    phi_c = np.arctan((r_pol / r_eq) ** 2 * np.tan(phi))
    r_c = r_pol / np.sqrt(1 - e2 * np.cos(phi_c) ** 2)
    s_x = (height + r_eq) - r_c * np.cos(phi_c) * np.cos(lam)
    s_y = -r_c * np.cos(phi_c) * np.sin(lam)
    s_z = r_c * np.sin(phi_c)
    # behind the limb the formulas still return an angle; such points are not on the grid
    visible = (height + r_eq) * ((height + r_eq) - s_x) >= s_y**2 + (r_eq / r_pol) ** 2 * s_z**2
    y, x = np.arctan(s_z / s_x), np.arcsin(-s_y / np.sqrt(s_x**2 + s_y**2 + s_z**2))
    return np.where(visible, y, np.nan), np.where(visible, x, np.nan)


class Goes19(Dataset):
    """Public in ``s3://noaa-goes19`` (us-east-1). Not in CMR, so discovery is an
    S3 listing. No public virtual store exists, so ``build-store`` writes the
    references to a local Icechunk repository: manifest retrieval for this
    dataset is a local disk read, and rows say so.
    """

    name = "goes19"
    region = "us-east-1"
    bucket = "noaa-goes19"
    product = "ABI-L2-CMIPF"
    band = "M6C13"
    variables = ("CMI",)
    time_path = "t"
    time_epoch = "2000-01-01T12:00:00"
    dims = ("t", "y", "x")
    public = True
    discovery_kind = "s3-list"
    index_location = "local-disk"
    probe_range_mib = 4  # a granule is about 24 MB
    # (year, first day of year, first hour, hours)
    windows: ClassVar[dict[str, tuple[int, int, int, int]]] = {
        "1h": (2026, 300, 15, 1),
        "1d": (2026, 300, 0, 24),
        "1w": (2026, 300, 0, 24 * 7),
        "1m": (2026, 300, 0, 24 * 30),
    }

    def _store(self) -> Any:
        return build_store(self.store_spec(None))

    # --- pinned inputs -----------------------------------------------------------

    def discover(self, window: str) -> list[Granule]:
        import obstore

        year, first_day, first_hour, hours = self.windows[window]
        store = self._store()
        stamp = re.compile(rf"{self.band}_G19_s(\d{{4}})(\d{{3}})(\d{{2}})(\d{{2}})(\d{{2}})")
        records = []
        for offset in range(hours):
            day, hour = first_day + (first_hour + offset) // 24, (first_hour + offset) % 24
            for page in obstore.list(store, prefix=f"{self.product}/{year}/{day:03d}/{hour:02d}/"):
                for meta in page:
                    path = str(meta["path"])
                    match = stamp.search(path)
                    if not match:
                        continue
                    y, julian_day, hh, mm, ss = (int(group) for group in match.groups())
                    seconds = ((julian_day - 1) * 24 + hh) * 3600 + mm * 60 + ss
                    start = np.datetime64(f"{y}-01-01T00:00:00") + np.timedelta64(seconds, "s")
                    records.append(
                        {
                            "granule_ur": path.rsplit("/", 1)[-1],
                            "start_time": f"{start}Z",
                            "source_url": f"s3://{self.bucket}/{path}",
                            "source_size_mb": meta["size"] / 1e6,
                        }
                    )
        if not records:
            raise RuntimeError(f"no {self.band} granules listed for window {window}")
        return sorted(records, key=lambda record: record["start_time"])

    def describe_grid(self, granules: list[Granule]) -> Grid:
        import h5py
        from obspec_utils.readers import BlockStoreReader

        path = store_path(self.store_spec(None), granules[0]["source_url"])
        grid: Grid = {}
        with BlockStoreReader(self._store(), path, block_size=2**20) as reader, h5py.File(reader, "r") as f:
            for axis in ("x", "y"):
                variable = f[axis]
                grid[axis] = {
                    "scale": float(variable.attrs["scale_factor"][0]),
                    "offset": float(variable.attrs["add_offset"][0]),
                    "size": int(variable.shape[0]),
                }
            projection = f["goes_imager_projection"].attrs
            for key in (
                "perspective_point_height",
                "semi_major_axis",
                "semi_minor_axis",
                "longitude_of_projection_origin",
            ):
                grid[key] = float(np.asarray(projection[key]).reshape(-1)[0])
        return grid

    # --- questions ---------------------------------------------------------------

    def index_points(self, grid: Grid, lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        y, x = goes_scan_angles(grid, lats, lons)
        indices = []
        for angles, axis in ((y, grid["y"]), (x, grid["x"])):
            position = (angles - axis["offset"]) / axis["scale"]
            if np.isnan(position).any() or position.min() < -0.5 or position.max() > axis["size"] - 0.5:
                raise ValueError("a question point falls off the full-disk grid")
            indices.append(np.rint(position).astype(int).clip(0, axis["size"] - 1))
        return indices[0], indices[1]

    def index_window(self, grid: Grid, bounds: tuple[float, float, float, float]) -> tuple[slice, slice]:
        south, north, west, east = bounds
        corners_lat, corners_lon = np.array([south, south, north, north]), np.array([west, east, west, east])
        ys, xs = self.index_points(grid, corners_lat, corners_lon)
        return slice(int(ys.min()), int(ys.max()) + 1), slice(int(xs.min()), int(xs.max()) + 1)

    def full_window(self, grid: Grid) -> tuple[slice, slice]:
        return slice(0, grid["y"]["size"]), slice(0, grid["x"]["size"])

    # --- access ------------------------------------------------------------------

    def auth(self) -> Credentials:
        return None

    def store_spec(self, credentials: Credentials) -> dict[str, Any]:
        return {"kind": "s3", "bucket": self.bucket, "prefix": "", "region": self.region, "credentials": None}

    def filesystem(self, credentials: Credentials) -> Any:
        import s3fs

        s3fs.S3FileSystem.clear_instance_cache()
        return s3fs.S3FileSystem(anon=True)

    # --- the virtual store -------------------------------------------------------

    def store_path(self, window: str) -> Path:
        return STORE_DIR / f"{self.name}-{window}"

    def build_virtual_store(self, payload: dict[str, Any], source: str = "hdf5") -> tuple[str, Path]:
        """Reads each file's HDF5 metadata."""
        import icechunk
        from obspec_utils.registry import ObjectStoreRegistry
        from virtualizarr import open_virtual_dataset
        from virtualizarr.parsers import HDFParser

        if source != "hdf5":
            raise ValueError(f"{self.name} has no {source} sidecars to build from")
        registry = ObjectStoreRegistry({f"s3://{self.bucket}": self._store()})
        parts = []
        for granule in payload["granules"]:
            virtual = open_virtual_dataset(
                granule["source_url"], registry, HDFParser(), loadable_variables=[self.time_path]
            )
            extra_coords = [name for name in virtual.coords if name not in self.dims]
            parts.append(virtual[list(self.variables)].drop_vars(extra_coords, errors="ignore"))
        path = self.store_path(payload["window"])
        container = icechunk.VirtualChunkContainer(
            f"s3://{self.bucket}/", icechunk.s3_store(region=self.region, anonymous=True)
        )
        message = f"{len(parts)} granules, window {payload['window']}"
        return write_local_repository(path, concat_granules(parts, self.dims[0]), container, message), path

    def open_virtual(
        self, payload: dict[str, Any], settings: dict[str, Any], source: str = "hdf5"
    ) -> tuple[Any, np.ndarray]:
        import icechunk
        import xarray as xr

        if source != "hdf5":
            raise FileNotFoundError(f"{self.name} has no {source} index")
        path = self.store_path(payload["window"])
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing: run `run.py build-store --dataset {self.name} --window {payload['window']}`"
            )
        access = icechunk.containers_credentials({f"s3://{self.bucket}/": icechunk.s3_anonymous_credentials()})
        repo = icechunk.Repository.open(
            icechunk.local_filesystem_storage(str(path)), authorize_virtual_chunk_access=access
        )
        if any(key.startswith("icechunk_") for key in settings):
            repo = repo.reopen(config=icechunk_config(repo, settings), authorize_virtual_chunk_access=access)
        session = repo.readonly_session(snapshot_id=payload["snapshot_id"])
        dataset = xr.open_zarr(session.store, consolidated=False, zarr_format=3, chunks=None)
        return dataset, np.arange(len(payload["granules"]))
