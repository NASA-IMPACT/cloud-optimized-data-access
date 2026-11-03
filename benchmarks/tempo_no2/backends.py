"""The arms: three ways (plus one checker) to the same TEMPO NO₂ bytes.

Every backend returns a :class:`SelectionResult` for a point set or an AOI
window over a list of observations (indices into the pinned manifest), so
the gate can compare them exactly and the runner can time them alike.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import h5py
import numpy as np
import pandas as pd
import xarray as xr
from obspec_utils.readers import BlockStoreReader
from obspec_utils.registry import ObjectStoreRegistry

from benchmarks.tempo_no2.cases import CONFIG
from benchmarks.tempo_no2.counters import RequestTrace, S3fsCounter, probe_paging, trace_counts, traced_store

NO2_PATH = "product/vertical_column_troposphere"
QUALITY_PATH = "product/main_data_quality_flag"
NO2_VAR = "vertical_column_troposphere"
QUALITY_VAR = "main_data_quality_flag"
TEMPO_EPOCH = np.datetime64("1980-01-06T00:00:00", "ns")
SOURCE_BUCKET = "asdc-prod-protected"
SOURCE_PREFIX = f"s3://{SOURCE_BUCKET}"


@dataclass
class SelectionResult:
    times: np.ndarray
    no2: np.ndarray
    quality: np.ndarray


class Backend(Protocol):
    name: str
    settings: dict[str, Any]

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult: ...

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult: ...

    def counts(self) -> dict[str, Any] | None: ...


def quality_mask(result: SelectionResult) -> np.ndarray:
    """Provisional V04 rule: keep NO₂ where the flag equals the accepted value."""
    return np.where(result.quality == CONFIG.accepted_quality_flag, result.no2, np.nan)


# --- credentials -----------------------------------------------------------


class CredentialCache:
    """DAAC temporary credentials, refreshed when older than ``max_age_s``."""

    def __init__(self, fetch: Callable[[], dict[str, Any]] | None = None, max_age_s: float = 3000) -> None:
        self._fetch = fetch or _fetch_asdc_credentials
        self.max_age_s = max_age_s
        self._value: dict[str, Any] | None = None
        self._at = 0.0

    def get(self) -> dict[str, Any]:
        if self._value is None or time.monotonic() - self._at > self.max_age_s:
            self._value = self._fetch()
            self._at = time.monotonic()
        return self._value


def _fetch_asdc_credentials() -> dict[str, Any]:
    import earthaccess

    earthaccess.login(strategy="all", persist=True)
    credentials = earthaccess.get_s3_credentials(daac="ASDC")
    missing = [k for k in ("accessKeyId", "secretAccessKey", "sessionToken") if not credentials.get(k)]
    if missing:
        raise RuntimeError(f"ASDC credential response missing {missing}")
    return credentials


def source_registry(credentials: dict[str, Any]) -> ObjectStoreRegistry:
    from obstore.store import S3Store

    store = S3Store(
        SOURCE_BUCKET,
        region=CONFIG.region,
        access_key_id=credentials["accessKeyId"],
        secret_access_key=credentials["secretAccessKey"],
        token=credentials["sessionToken"],
    )
    return ObjectStoreRegistry({SOURCE_PREFIX: store})


# --- shared HDF5 decoding (from the notebook) --------------------------------


def _decode_cf(dataset: h5py.Dataset, raw: Any) -> np.ndarray:
    raw_values = np.asarray(raw)
    fill = dataset.attrs.get("_FillValue")
    mask = np.zeros(raw_values.shape, dtype=bool)
    if fill is not None:
        mask = raw_values == np.asarray(fill).reshape(-1)[0]
    values = raw_values.astype(np.float64)
    if "scale_factor" in dataset.attrs:
        values *= float(np.asarray(dataset.attrs["scale_factor"]).reshape(-1)[0])
    if "add_offset" in dataset.attrs:
        values += float(np.asarray(dataset.attrs["add_offset"]).reshape(-1)[0])
    return np.where(mask, np.nan, values)


def _source_time(handle: h5py.File) -> np.datetime64:
    seconds = float(np.asarray(handle["time"][0]))
    return TEMPO_EPOCH + np.timedelta64(round(seconds * 1_000_000_000), "ns")


# --- arm: what users do today -----------------------------------------------


class NetcdfUserStack:
    """``earthaccess.open`` + ``xarray.open_mfdataset(engine="h5netcdf")``, defaults."""

    name = "netcdf_user_stack"

    def __init__(self, urls: Sequence[str], opener: Callable[[Sequence[str]], Sequence[Any]] | None = None) -> None:
        self.urls = list(urls)
        self.settings: dict[str, Any] = {"engine": "h5netcdf", "defaults": True}
        self._opener = opener or _earthaccess_open
        self._counter: S3fsCounter | None = None
        files = list(self._opener(self.urls))
        fs = getattr(files[0], "fs", None)
        if fs is not None and hasattr(fs, "s3"):
            self._counter = S3fsCounter()
            self._counter.attach(fs)
        kwargs: dict[str, Any] = {"engine": "h5netcdf", "combine": "nested", "concat_dim": "time"}
        self.product = xr.open_mfdataset(files, group="product", **kwargs)
        # The time coordinate lives in the root group; open the files again for it.
        self.root = xr.open_mfdataset(list(self._opener(self.urls)), **kwargs)

    def _times(self, observations: list[int]) -> np.ndarray:
        return np.asarray(self.root["time"].isel(time=observations).values, dtype="datetime64[ns]")

    def _result(self, observations: list[int], indexers: dict[str, Any]) -> SelectionResult:
        selected = self.product[[NO2_VAR, QUALITY_VAR]].isel(time=observations, **indexers).compute()
        return SelectionResult(
            self._times(observations),
            np.asarray(selected[NO2_VAR].values, dtype=np.float64),
            np.asarray(selected[QUALITY_VAR].values, dtype=np.float64),
        )

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult:
        return self._result(
            observations,
            {
                "latitude": xr.DataArray(point_rows["lat_index"].to_numpy(), dims="point"),
                "longitude": xr.DataArray(point_rows["lon_index"].to_numpy(), dims="point"),
            },
        )

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult:
        return self._result(observations, {"latitude": window[0], "longitude": window[1]})

    def counts(self) -> dict[str, Any] | None:
        return self._counter.counts() if self._counter else None


def _earthaccess_open(urls: Sequence[str]) -> Sequence[Any]:
    import earthaccess

    earthaccess.login(strategy="all", persist=True)
    return earthaccess.open(list(urls), provider="LARC_CLOUD")


# --- arm: the baseline the HDF5 team would write ------------------------------


class NetcdfTuned:
    """h5py over an 8 MiB block reader, granules concurrent, points batched per chunk.

    Settings follow the NSIDC cloud-optimized HDF5 study; ``page_buf_size``
    applies only when the file uses the paged file-space strategy.
    """

    name = "netcdf_tuned"

    def __init__(
        self,
        urls: Sequence[str],
        registry: ObjectStoreRegistry,
        block_size: int = 8 * 2**20,
        max_workers: int = 8,
        page_buf_size: int = 16 * 2**20,
    ) -> None:
        self.urls = list(urls)
        self._traces: list[RequestTrace] = []
        self._wrapped: dict[int, Any] = {}
        self._targets = [self._resolve(registry, url) for url in self.urls]
        self.block_size, self.max_workers, self.page_buf_size = block_size, max_workers, page_buf_size
        with self._open(0, paged=False) as handle:
            self.paged = probe_paging(handle)
        self.settings: dict[str, Any] = {
            "block_size": block_size,
            "max_workers": max_workers,
            "page_buf_size": page_buf_size if self.paged else 0,
            "paged": self.paged,
        }

    def _resolve(self, registry: ObjectStoreRegistry, url: str) -> tuple[Any, str]:
        store, path = registry.resolve(url)
        if id(store) not in self._wrapped:
            wrapped, trace = traced_store(store)
            self._wrapped[id(store)] = wrapped
            self._traces.append(trace)
        return self._wrapped[id(store)], path

    @contextmanager
    def _open(self, observation: int, paged: bool | None = None) -> Iterator[h5py.File]:
        store, path = self._targets[observation]
        paged = self.paged if paged is None else paged
        kwargs = {"page_buf_size": self.page_buf_size} if paged else {}
        with BlockStoreReader(store, path, block_size=self.block_size) as reader, h5py.File(reader, "r", **kwargs) as handle:
            yield handle

    def _read_points(self, observation: int, lat_idx: np.ndarray, lon_idx: np.ndarray) -> tuple[np.datetime64, np.ndarray, np.ndarray]:
        with self._open(observation) as handle:
            t = _source_time(handle)
            out = []
            for path in (NO2_PATH, QUALITY_PATH):
                dataset = handle[path]
                ch, cw = dataset.chunks[-2], dataset.chunks[-1]
                values = np.full(len(lat_idx), np.nan)
                # One read per touched chunk, then pick the points inside it.
                for key in {(int(a) // ch, int(b) // cw) for a, b in zip(lat_idx, lon_idx, strict=True)}:
                    ys, xs = slice(key[0] * ch, (key[0] + 1) * ch), slice(key[1] * cw, (key[1] + 1) * cw)
                    block = _decode_cf(dataset, dataset[(0,) * (dataset.ndim - 2) + (ys, xs)])
                    inside = (lat_idx // ch == key[0]) & (lon_idx // cw == key[1])
                    values[inside] = block[lat_idx[inside] - ys.start, lon_idx[inside] - xs.start]
                out.append(values)
            return t, out[0], out[1]

    def _read_window(self, observation: int, window: tuple[slice, slice]) -> tuple[np.datetime64, np.ndarray, np.ndarray]:
        with self._open(observation) as handle:
            t = _source_time(handle)
            out = []
            for path in (NO2_PATH, QUALITY_PATH):
                dataset = handle[path]
                out.append(_decode_cf(dataset, dataset[(0,) * (dataset.ndim - 2) + tuple(window)]))
            return t, out[0], out[1]

    def _collect(self, results: list[tuple[np.datetime64, np.ndarray, np.ndarray]]) -> SelectionResult:
        return SelectionResult(
            np.asarray([r[0] for r in results], dtype="datetime64[ns]"),
            np.stack([r[1] for r in results]),
            np.stack([r[2] for r in results]),
        )

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult:
        lat_idx = point_rows["lat_index"].to_numpy()
        lon_idx = point_rows["lon_index"].to_numpy()
        with ThreadPoolExecutor(self.max_workers) as pool:
            return self._collect(list(pool.map(lambda o: self._read_points(o, lat_idx, lon_idx), observations)))

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult:
        with ThreadPoolExecutor(self.max_workers) as pool:
            return self._collect(list(pool.map(lambda o: self._read_window(o, window), observations)))

    def counts(self) -> dict[str, Any] | None:
        total: dict[str, Any] = {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}}
        for trace in self._traces:
            c = trace_counts(trace)
            total["request_count"] += c["request_count"]
            total["bytes_requested"] += c["bytes_requested"]
            for k, v in c["requests_by_method"].items():
                total["requests_by_method"][k] = total["requests_by_method"].get(k, 0) + v
        return total


# --- arm: the access layer ----------------------------------------------------


class _VirtualSelection:
    """Shared selection logic for the two virtual arms."""

    def __init__(self, dataset: xr.Dataset, manifest: pd.DataFrame) -> None:
        self.dataset = dataset
        store_times = pd.DatetimeIndex(dataset["time"].values).tz_localize("UTC")
        nominal = pd.to_datetime(manifest["start_time"], utc=True)
        tolerance = pd.Timedelta(minutes=CONFIG.time_tolerance_minutes)
        indexer = store_times.get_indexer(nominal, method="nearest", tolerance=tolerance)
        if np.any(indexer < 0):
            missing = manifest.loc[indexer < 0, "granule_ur"].tolist()
            raise RuntimeError(f"Event granules missing from the store snapshot: {missing}")
        self.time_indices = indexer
        self._offsets_s = np.abs((store_times[indexer] - pd.DatetimeIndex(nominal)).total_seconds().to_numpy())

    def assert_one_to_one(self) -> float:
        """Mapping is injective and within tolerance; returns the max offset in seconds."""
        if len(set(self.time_indices.tolist())) != len(self.time_indices):
            raise AssertionError("two granules map to the same store time step")
        return float(self._offsets_s.max())

    def _selection(self, observations: list[int], indexers: dict[str, Any]) -> SelectionResult:
        time_indices = self.time_indices[observations]
        selected = self.dataset[[NO2_VAR, QUALITY_VAR]].isel(
            time=xr.DataArray(time_indices, dims="observation"), **indexers
        ).load()
        return SelectionResult(
            np.asarray(selected["time"].values, dtype="datetime64[ns]"),
            np.asarray(selected[NO2_VAR].values, dtype=np.float64),
            np.asarray(selected[QUALITY_VAR].values, dtype=np.float64),
        )

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult:
        return self._selection(
            observations,
            {
                "latitude": xr.DataArray(point_rows["lat_index"].to_numpy(), dims="point"),
                "longitude": xr.DataArray(point_rows["lon_index"].to_numpy(), dims="point"),
            },
        )

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult:
        return self._selection(observations, {"latitude": window[0], "longitude": window[1]})


class VirtualIcechunk(_VirtualSelection):
    name = "virtual_icechunk"

    def __init__(self, session_store: Any, manifest: pd.DataFrame) -> None:
        self.settings: dict[str, Any] = {"client": "icechunk", "chunks": None}
        super().__init__(xr.open_zarr(session_store, consolidated=False, chunks=None), manifest)

    def counts(self) -> dict[str, Any] | None:
        return None  # not instrumented; the static minimum is reported instead


class VirtualManifestStore(_VirtualSelection):
    """The same references through VirtualiZarr's ManifestStore, with a tracer.

    Exists to validate the static minimum request count; never a headline arm.
    """

    name = "virtual_manifeststore"

    def __init__(self, session: Any, stores: dict[str, Any], native_chunks_prefix: str, manifest: pd.DataFrame) -> None:
        """``stores`` maps URL prefixes to obstore stores; each is wrapped with a tracer."""
        from virtualizarr.parsers.icechunk import IcechunkParser

        self._traces: list[RequestTrace] = []
        wrapped: dict[str, Any] = {}
        for prefix, store in stores.items():
            wrapped[prefix], trace = traced_store(store)
            self._traces.append(trace)
        traced_registry = ObjectStoreRegistry(wrapped)
        manifest_store = IcechunkParser().parse_session(session, traced_registry, native_chunks_prefix=native_chunks_prefix)
        self.settings = {"client": "virtualizarr.ManifestStore", "chunks": None}
        super().__init__(xr.open_zarr(manifest_store, consolidated=False, chunks=None, zarr_format=3), manifest)

    def counts(self) -> dict[str, Any] | None:
        total: dict[str, Any] = {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}}
        for trace in self._traces:
            c = trace_counts(trace)
            total["request_count"] += c["request_count"]
            total["bytes_requested"] += c["bytes_requested"]
            for k, v in c["requests_by_method"].items():
                total["requests_by_method"][k] = total["requests_by_method"].get(k, 0) + v
        return total


# --- the store ------------------------------------------------------------------


def open_mirror_repo() -> Any:
    """The public Source Cooperative mirror, with Earthdata credentials for virtual chunks."""
    import icechunk
    from earthaccess_auth.adapters.icechunk import earthdata_containers_credentials

    storage = icechunk.s3_storage(
        bucket=CONFIG.mirror_bucket, prefix=CONFIG.mirror_prefix, region=CONFIG.region, anonymous=True
    )
    repo = icechunk.Repository.open(storage)
    return repo.reopen(authorize_virtual_chunk_access=earthdata_containers_credentials(repo))


def pinned_session(repo: Any, snapshot_id: str) -> Any:
    return repo.readonly_session(snapshot_id=snapshot_id)


def resolve_pin(repo: Any) -> tuple[str, str | None]:
    """(snapshot_id, tag): the benchmark tag when it exists, else main's tip untagged."""
    if CONFIG.snapshot_tag in repo.list_tags():
        return repo.lookup_tag(CONFIG.snapshot_tag), CONFIG.snapshot_tag
    return repo.lookup_branch("main"), None
