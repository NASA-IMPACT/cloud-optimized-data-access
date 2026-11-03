"""Tiny synthetic TEMPO NO₂ L3 granules and a local virtual Icechunk store.

Layout mirrors the real files at a small grid: root ``time``/``latitude``/
``longitude`` dimension scales and a ``product`` group holding
``vertical_column_troposphere`` (float64) and ``main_data_quality_flag``
(int16), both chunked with shuffle + deflate(1). Values are deterministic
functions of the scan index so read-back can be asserted.
"""

from __future__ import annotations

from pathlib import Path

import h5py
import icechunk
import numpy as np
import pandas as pd
import xarray as xr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from virtualizarr import open_virtual_dataset
from virtualizarr.parsers import HDFParser

TEMPO_EPOCH = np.datetime64("1980-01-06T00:00:00", "ns")
NO2_FILL = -1.0e30
FLAG_FILL = np.int16(-9999)
FIRST_SCAN = np.datetime64("2025-03-14T12:00:00", "ns")


def scan_time_seconds(i: int) -> float:
    return float((FIRST_SCAN + np.timedelta64(i, "h") - TEMPO_EPOCH) / np.timedelta64(1, "s"))


def expected_no2(i: int, lat: int, lon: int) -> np.ndarray:
    grid = 1e15 * (i + 1) + 1e12 * np.arange(lat)[:, None] + 1e9 * np.arange(lon)[None, :]
    grid = grid.astype("float64")
    grid[: lat // 4, : lon // 4] = NO2_FILL  # a fill block in every scan
    return grid[None]


def expected_flag(i: int, lat: int, lon: int) -> np.ndarray:
    return ((np.arange(lat)[:, None] + np.arange(lon)[None, :] + i) % 3).astype("int16")[None]


def make_granules(tmp_path: Path, n: int = 2, lat: int = 60, lon: int = 80, chunks: tuple[int, int] = (20, 40)) -> list[Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    lats = np.linspace(20.0, 50.0, lat, dtype="float32")
    lons = np.linspace(-120.0, -70.0, lon, dtype="float32")
    paths = []
    for i in range(n):
        path = tmp_path / f"TEMPO_NO2_L3_V04_20250314T{12 + i:02d}0000Z_S{i:03d}.nc"
        with h5py.File(path, "w") as f:
            t = f.create_dataset("time", data=np.array([scan_time_seconds(i)], dtype="float64"))
            t.attrs["units"] = "seconds since 1980-01-06T00:00:00Z"
            t.make_scale("time")
            la = f.create_dataset("latitude", data=lats)
            la.make_scale("latitude")
            lo = f.create_dataset("longitude", data=lons)
            lo.make_scale("longitude")
            product = f.create_group("product")
            for name, data, fill in (
                ("vertical_column_troposphere", expected_no2(i, lat, lon), NO2_FILL),
                ("main_data_quality_flag", expected_flag(i, lat, lon), FLAG_FILL),
            ):
                ds = product.create_dataset(
                    name, data=data, chunks=(1, *chunks), shuffle=True, compression="gzip", compression_opts=1, fillvalue=fill
                )
                ds.attrs["_FillValue"] = np.array([fill], dtype=data.dtype)
                ds.dims[0].attach_scale(t)
                ds.dims[1].attach_scale(la)
                ds.dims[2].attach_scale(lo)
        paths.append(path)
    return paths


def manifest_for(granules: list[Path]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "granule_ur": [p.stem for p in granules],
            "source_url": [f"file://{p}" for p in granules],
            "start_time": [str(FIRST_SCAN + np.timedelta64(i, "h")) + "Z" for i in range(len(granules))],
        }
    )


def make_virtual_store(tmp_path: Path, granules: list[Path]) -> tuple[icechunk.Repository, str, str]:
    """Virtual Icechunk store over ``granules``: flat layout, concatenated on time.

    Returns (repo, snapshot_id, native_chunks_prefix).
    """
    registry = ObjectStoreRegistry({"file://": LocalStore()})
    parts = []
    for p in granules:
        url = f"file://{p}"
        root = open_virtual_dataset(url, registry, HDFParser(), loadable_variables=["time", "latitude", "longitude"])
        prod = open_virtual_dataset(url, registry, HDFParser(group="product"))
        prod = prod.assign_coords(time=root["time"], latitude=root["latitude"], longitude=root["longitude"])
        parts.append(prod)
    vds = xr.concat(parts, dim="time", coords="minimal", compat="override", combine_attrs="override")
    granule_dir = granules[0].parent
    prefix = f"file://{granule_dir}/"
    chunk_store = icechunk.local_filesystem_store(str(granule_dir))
    config = icechunk.RepositoryConfig.default()
    config.set_virtual_chunk_container(icechunk.VirtualChunkContainer(prefix, chunk_store, name="local"))
    repo = icechunk.Repository.open_or_create(
        storage=icechunk.local_filesystem_storage(str(tmp_path / "repo")),
        config=config,
        authorize_virtual_chunk_access={prefix: icechunk.credentials.LocalFileSystemAccess},
    )
    session = repo.writable_session("main")
    vds.vz.to_icechunk(session.store, validate_containers=False)
    snapshot = session.commit("fixture")
    return repo, snapshot, f"file://{tmp_path / 'repo' / 'chunks'}"
