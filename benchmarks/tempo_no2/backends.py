"""The arms: three ways (plus one checker) to the same TEMPO NO₂ bytes.

Every backend returns a :class:`SelectionResult` for a point set or an AOI
window over a list of observations (indices into the pinned manifest), so
the gate can compare them exactly and the runner can time them alike.
"""

from __future__ import annotations

import math
import multiprocessing
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import h5py
import numpy as np
import pandas as pd
import xarray as xr
from obspec_utils.readers import BlockStoreReader
from obspec_utils.registry import ObjectStoreRegistry

from benchmarks.tempo_no2.cases import CONFIG
from benchmarks.tempo_no2.counters import (
    CLIENT_RANGES,
    RequestTrace,
    S3fsCounter,
    probe_paging,
    trace_counts,
    traced_store,
)
from benchmarks.tempo_no2.measure import METADATA_OBJECTS, static_minimum_requests

NO2_PATH = "product/vertical_column_troposphere"
QUALITY_PATH = "product/main_data_quality_flag"
NO2_VAR = "vertical_column_troposphere"
QUALITY_VAR = "main_data_quality_flag"
COORDS = ("time", "latitude", "longitude")
TEMPO_EPOCH = np.datetime64("1980-01-06T00:00:00", "ns")
SOURCE_BUCKET = "asdc-prod-protected"
SOURCE_PREFIX = f"s3://{SOURCE_BUCKET}"
PROVIDER = "LARC_CLOUD"
# NASA's distribution endpoint serves the same objects over HTTPS from anywhere,
# with an Earthdata bearer token. Used by --access https (laptop runs).
HTTPS_ENDPOINT = "https://data.asdc.earthdata.nasa.gov"
ASDC_CONTAINER = "asdc"


def https_url(s3_url: str) -> str:
    """s3://bucket/key → the distribution endpoint's URL for the same object."""
    u = urlparse(s3_url)
    return f"{HTTPS_ENDPOINT}/{u.netloc}{u.path}"


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


# --- credentials and stores -----------------------------------------------------


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


def build_store(spec: dict[str, Any]) -> Any:
    """An obstore store from a picklable spec: {"kind": "local"},
    {"kind": "http", "base_url", "client_options"} or
    {"kind": "s3", "bucket", "prefix", "region", "credentials"}."""
    if spec["kind"] == "local":
        from obstore.store import LocalStore

        return LocalStore(prefix=spec["prefix"]) if spec.get("prefix") else LocalStore()
    if spec["kind"] == "http":
        from obstore.store import HTTPStore

        return HTTPStore(spec["base_url"], client_options=spec.get("client_options") or None)
    from obstore.store import S3Store

    c = spec.get("credentials")
    if c:
        return S3Store(
            spec["bucket"], prefix=spec.get("prefix") or None, region=spec.get("region", CONFIG.region),
            access_key_id=c["accessKeyId"], secret_access_key=c["secretAccessKey"], token=c["sessionToken"],
        )
    return S3Store(spec["bucket"], prefix=spec.get("prefix") or None, region=spec.get("region", CONFIG.region), skip_signature=True)


def source_store_spec(credentials: dict[str, Any] | None, access: str = "s3", client_options: dict[str, Any] | None = None) -> dict[str, Any]:
    if access == "https":
        return {"kind": "http", "base_url": HTTPS_ENDPOINT, "client_options": client_options or _https_client_options()}
    return {"kind": "s3", "bucket": SOURCE_BUCKET, "prefix": "", "region": CONFIG.region, "credentials": credentials}


def _https_client_options() -> dict[str, Any]:
    import earthaccess_auth
    from earthaccess_auth.adapters.obstore import http_client_options

    return http_client_options(earthaccess_auth.login())


def container_stores(repo: Any, credentials: dict[str, Any] | None, client_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Registry entries for every virtual chunk container, under both the
    container's URL prefix and its ``vcc://<name>`` form, so references in
    either form resolve. S3 containers get ``credentials``; HTTPS containers
    get ``client_options`` (the bearer token headers)."""
    stores: dict[str, Any] = {}
    for container in (repo.config.virtual_chunk_containers or {}).values():
        url = urlparse(container.url_prefix)
        if url.scheme == "file":
            store = build_store({"kind": "local", "prefix": url.path})
        elif url.scheme in ("http", "https"):
            store = build_store({"kind": "http", "base_url": container.url_prefix.rstrip("/"), "client_options": client_options})
        else:
            store = build_store({"kind": "s3", "bucket": url.netloc, "prefix": url.path.strip("/"), "credentials": credentials})
        stores[container.url_prefix.rstrip("/")] = store
        if container.name:
            stores[f"vcc://{container.name}"] = store
    return stores


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
    """earthaccess's filesystem + ``xarray.open_mfdataset(engine="h5netcdf",
    group="product")``, default settings: what the earthaccess docs show.

    The time axis lives in the root group, so each granule is opened a second
    time for it. Every open goes through fsspec's default read-ahead cache,
    which fetches a block per HDF5 metadata read; that cost is the point.
    """

    name = "netcdf_user_stack"

    def __init__(self, urls: Sequence[str], filesystem: Any | None = None) -> None:
        self.urls = list(urls)
        self.settings: dict[str, Any] = {"engine": "h5netcdf", "group": "product", "defaults": True}
        fs = filesystem if filesystem is not None else _earthaccess_filesystem()
        self._counter: S3fsCounter | None = None
        if hasattr(fs, "s3"):
            self._counter = S3fsCounter()
            self._counter.attach(fs)
        kwargs: dict[str, Any] = {"engine": "h5netcdf", "combine": "nested", "concat_dim": "time"}
        _progress(f"user stack: opening {len(self.urls)} granules (product group)")
        self.dataset = xr.open_mfdataset([fs.open(u) for u in self.urls], group="product", **kwargs)[[NO2_VAR, QUALITY_VAR]]
        _progress("user stack: opening the root group for the time axis")
        self.root = xr.open_mfdataset([fs.open(u) for u in self.urls], **kwargs)
        self.dataset = self.dataset.assign_coords(time=self.root["time"])

    def _result(self, observations: list[int], indexers: dict[str, Any]) -> SelectionResult:
        selected = self.dataset.isel(time=observations, **indexers).compute()
        return SelectionResult(
            np.asarray(selected["time"].values, dtype="datetime64[ns]"),
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


def _progress(message: str) -> None:
    import sys
    import time as _time

    print(f"[{_time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _earthaccess_filesystem(access: str = "s3") -> Any:
    import earthaccess

    earthaccess.login(strategy="all", persist=True)
    if access == "https":
        return earthaccess.get_fsspec_https_session()
    return earthaccess.get_s3_filesystem(provider=PROVIDER)


# --- arm: the baseline the HDF5 team would write ------------------------------


def _tuned_read(
    store_spec: dict[str, Any], url_path: str, block_size: int, page_buf_size: int, paged: bool,
    kind: str, lat: Any, lon: Any,
) -> tuple[np.datetime64, np.ndarray, np.ndarray, dict[str, Any]]:
    """Read one granule in a worker process; returns the values and this worker's request counts.

    A process per granule because h5py holds one global lock across
    threads, so threads cannot overlap the network reads.
    """
    store, trace = traced_store(build_store(store_spec))
    kwargs = {"page_buf_size": page_buf_size} if paged else {}
    with BlockStoreReader(store, url_path, block_size=block_size) as reader, h5py.File(reader, "r", **kwargs) as handle:
        t, no2, quality = _select(handle, kind, lat, lon)
    return t, no2, quality, trace_counts(trace)


def _select(handle: h5py.File, kind: str, lat: Any, lon: Any) -> tuple[np.datetime64, np.ndarray, np.ndarray]:
    """The time, NO₂ and quality flag for an AOI window or a point set from one open granule."""
    out = []
    for path in (NO2_PATH, QUALITY_PATH):
        dataset = handle[path]
        lead = (0,) * (dataset.ndim - 2)
        if kind == "aoi":
            out.append(_decode_cf(dataset, dataset[lead + (lat, lon)]))
            continue
        lat_idx, lon_idx = np.asarray(lat), np.asarray(lon)
        ch, cw = dataset.chunks[-2], dataset.chunks[-1]
        values = np.full(len(lat_idx), np.nan)
        # One read per touched chunk, then pick the points inside it.
        for key in {(int(a) // ch, int(b) // cw) for a, b in zip(lat_idx, lon_idx, strict=True)}:
            ys, xs = slice(key[0] * ch, (key[0] + 1) * ch), slice(key[1] * cw, (key[1] + 1) * cw)
            block = _decode_cf(dataset, dataset[lead + (ys, xs)])
            inside = (lat_idx // ch == key[0]) & (lon_idx // cw == key[1])
            values[inside] = block[lat_idx[inside] - ys.start, lon_idx[inside] - xs.start]
        out.append(values)
    return _source_time(handle), out[0], out[1]


def store_path(store_spec: dict[str, Any], url: str) -> str:
    """A manifest URL → the path of that object in the store ``store_spec`` builds."""
    if store_spec["kind"] == "local":
        return urlparse(url).path
    if store_spec["kind"] == "http":
        base = store_spec["base_url"].rstrip("/") + "/"
        if url.startswith(base):
            return url[len(base):]
        u = urlparse(url)  # an s3:// manifest URL: bucket/key under the endpoint
        return f"{u.netloc}{u.path}"
    path = urlparse(url).path.lstrip("/")
    prefix = store_spec.get("prefix") or ""
    return path[len(prefix):].lstrip("/") if prefix and path.startswith(prefix) else path


def _warm_worker(_: int) -> int:
    """Imports done; the worker is ready."""
    return 0


class NetcdfTuned:
    """h5py over an 8 MiB block reader, one process per granule (up to
    ``max_workers`` at once), points batched per chunk.

    Settings follow the NSIDC cloud-optimized HDF5 study; ``page_buf_size``
    applies only when the file uses the paged file-space strategy.
    """

    name = "netcdf_tuned"

    def __init__(
        self,
        urls: Sequence[str],
        store_spec: dict[str, Any],
        block_size: int = 8 * 2**20,
        max_workers: int = 8,
        page_buf_size: int = 16 * 2**20,
    ) -> None:
        self.urls = list(urls)
        self.store_spec = store_spec
        self.block_size, self.max_workers, self.page_buf_size = block_size, max_workers, page_buf_size
        self._paths = [store_path(store_spec, url) for url in self.urls]
        self._counts: dict[str, Any] = {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}, "kind": CLIENT_RANGES}
        self._pool: ProcessPoolExecutor | None = None
        store, trace = traced_store(build_store(store_spec))
        with BlockStoreReader(store, self._paths[0], block_size=block_size) as reader, h5py.File(reader, "r") as handle:
            self.paged = probe_paging(handle)
        self._add_counts(trace_counts(trace))
        if max_workers > 1:
            # Start the workers now: client setup belongs in the open phase, not in every read.
            # spawn, not fork: a forked child inherits obstore's async runtime and deadlocks in its first request
            self._pool = ProcessPoolExecutor(max_workers, mp_context=multiprocessing.get_context("spawn"))
            list(self._pool.map(_warm_worker, range(max_workers)))
        self.settings: dict[str, Any] = {
            "block_size": block_size,
            "max_workers": max_workers,
            "workers": "processes",
            "page_buf_size": page_buf_size if self.paged else 0,
            "paged": self.paged,
        }

    def _add_counts(self, c: dict[str, Any]) -> None:
        self._counts["request_count"] += c["request_count"]
        self._counts["bytes_requested"] += c["bytes_requested"]
        for k, v in c["requests_by_method"].items():
            self._counts["requests_by_method"][k] = self._counts["requests_by_method"].get(k, 0) + v

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 -- __del__ may run at interpreter shutdown; nothing to report to
            pass

    def _run(self, observations: list[int], kind: str, lat: Any, lon: Any) -> SelectionResult:
        jobs = [(self.store_spec, self._paths[o], self.block_size, self.page_buf_size, self.paged, kind, lat, lon) for o in observations]
        if self._pool is None:
            results = [_tuned_read(*job) for job in jobs]
        else:
            results = list(self._pool.map(_tuned_read, *zip(*jobs, strict=True)))
        for r in results:
            self._add_counts(r[3])
        return SelectionResult(
            np.asarray([r[0] for r in results], dtype="datetime64[ns]"),
            np.stack([r[1] for r in results]),
            np.stack([r[2] for r in results]),
        )

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult:
        return self._run(observations, "point", point_rows["lat_index"].to_numpy(), point_rows["lon_index"].to_numpy())

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult:
        return self._run(observations, "aoi", window[0], window[1])

    def counts(self) -> dict[str, Any] | None:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self._counts.items()}


# --- arm: the whole file into memory, then HDF5 from memory ---------------------


class NetcdfDownload:
    """Each granule whole into memory in concurrent range reads, then h5py on
    the in-memory file image: no round trips after the download.

    The other end of the bracket from range reads: every byte of the file,
    but the network's full concurrent rate.
    """

    name = "netcdf_download"

    def __init__(self, urls: Sequence[str], store_spec: dict[str, Any], part_bytes: int = 16 * 2**20, concurrency: int = 16) -> None:
        self.store, self._trace = traced_store(build_store(store_spec))
        self._paths = [store_path(store_spec, url) for url in urls]
        self.part_bytes, self.concurrency = part_bytes, concurrency
        self.settings: dict[str, Any] = {"part_bytes": part_bytes, "concurrency": concurrency, "parse": "h5py file image"}

    def _run(self, observations: list[int], kind: str, lat: Any, lon: Any) -> SelectionResult:
        import asyncio

        from benchmarks.tempo_no2.counters import fetch_object

        results = []
        # shortcut: one granule at a time, no overlap of one download with the previous parse; pipeline them if multi-granule cells matter
        for o in observations:
            data = asyncio.run(fetch_object(self.store, self._paths[o], part_bytes=self.part_bytes, concurrency=self.concurrency))
            with h5py.File.in_memory(data) as handle:
                results.append(_select(handle, kind, lat, lon))
        return SelectionResult(
            np.asarray([r[0] for r in results], dtype="datetime64[ns]"),
            np.stack([r[1] for r in results]),
            np.stack([r[2] for r in results]),
        )

    def point(self, observations: list[int], point_rows: pd.DataFrame) -> SelectionResult:
        return self._run(observations, "point", point_rows["lat_index"].to_numpy(), point_rows["lon_index"].to_numpy())

    def aoi(self, observations: list[int], window: tuple[slice, slice]) -> SelectionResult:
        return self._run(observations, "aoi", window[0], window[1])

    def counts(self) -> dict[str, Any] | None:
        return trace_counts(self._trace)


# --- arm: the access layer ----------------------------------------------------


class _VirtualSelection:
    """Shared selection logic for the two virtual arms."""

    def __init__(self, dataset: xr.Dataset, manifest: pd.DataFrame) -> None:
        self.dataset = dataset
        store_times = pd.DatetimeIndex(dataset["time"].values).tz_localize("UTC")
        nominal = pd.DatetimeIndex(pd.to_datetime(manifest["start_time"], utc=True))
        tolerance = pd.Timedelta(minutes=CONFIG.time_tolerance_minutes)
        indexer = store_times.get_indexer(nominal, method="nearest", tolerance=tolerance)
        if np.any(indexer < 0):
            missing = manifest.loc[indexer < 0, "granule_ur"].tolist()
            raise RuntimeError(f"Event granules missing from the store snapshot: {missing}")
        self.time_indices = indexer
        self._offsets_s = np.abs((store_times[indexer] - nominal).total_seconds().to_numpy())

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

    def static_minimum(self, observations: list[int], lat: slice | np.ndarray, lon: slice | np.ndarray) -> dict[str, int]:
        """Requests the selection must make, from the store's own chunk metadata:
        metadata objects + coordinate chunks (read at open) + intersected data chunks."""
        coord_chunks = 0
        for name in COORDS:
            var = self.dataset[name]
            chunk = (var.encoding.get("chunks") or var.shape)[0]
            coord_chunks += math.ceil(var.shape[0] / chunk)
        data_chunks, nbytes = 0, 0
        time_indices = [int(i) for i in self.time_indices[observations]]
        for name in (NO2_VAR, QUALITY_VAR):
            var = self.dataset[name]
            chunks = tuple(var.encoding.get("chunks") or var.shape)
            n, b = static_minimum_requests(tuple(var.shape), chunks, time_indices, lat, lon, itemsize=var.dtype.itemsize)
            data_chunks += n
            nbytes += b
        return {
            "request_count": METADATA_OBJECTS + coord_chunks + data_chunks,
            "bytes_requested": nbytes,
            "coordinate_chunks": coord_chunks,
            "data_chunks": data_chunks,
        }


class VirtualIcechunk(_VirtualSelection):
    name = "virtual_icechunk"

    def __init__(
        self, session_store: Any, manifest: pd.DataFrame, zarr_concurrency: int | None = None, zarrs_pipeline: bool = False
    ) -> None:
        import zarr

        if zarr_concurrency is not None:
            # zarr-python's cap on outstanding chunk requests (default 10); a tuning factor, recorded per row
            zarr.config.set({"async.concurrency": int(zarr_concurrency)})
        if zarrs_pipeline:
            # zarrs decodes chunks in Rust, in parallel, without the interpreter lock. Keys are set
            # one by one: replacing the whole codec_pipeline block drops zarr's own batch_size.
            zarr.config.set({
                "codec_pipeline.path": "zarrs.ZarrsCodecPipeline",
                "codec_pipeline.chunk_concurrent_minimum": 4,
                "codec_pipeline.strict": False,
            })
        self.settings: dict[str, Any] = {
            "client": "icechunk", "chunks": None,
            "zarr_concurrency": zarr.config.get("async.concurrency"),
            "codec_pipeline": "zarrs" if zarrs_pipeline else "zarr",
        }
        super().__init__(xr.open_zarr(session_store, consolidated=False, chunks=None), manifest)

    def counts(self) -> dict[str, Any] | None:
        return None  # not instrumented; static_minimum() is reported instead


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
        seen: dict[int, Any] = {}
        for prefix, store in stores.items():
            if id(store) not in seen:
                seen[id(store)], trace = traced_store(store)
                self._traces.append(trace)
            wrapped[prefix] = seen[id(store)]
        traced_registry = ObjectStoreRegistry(wrapped)
        manifest_store = IcechunkParser().parse_session(session, traced_registry, native_chunks_prefix=native_chunks_prefix)
        self.settings = {"client": "virtualizarr.ManifestStore", "chunks": None}
        super().__init__(xr.open_zarr(manifest_store, consolidated=False, chunks=None, zarr_format=3), manifest)

    def counts(self) -> dict[str, Any] | None:
        total: dict[str, Any] = {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}, "kind": CLIENT_RANGES}
        for trace in self._traces:
            c = trace_counts(trace)
            total["request_count"] += c["request_count"]
            total["bytes_requested"] += c["bytes_requested"]
            for k, v in c["requests_by_method"].items():
                total["requests_by_method"][k] = total["requests_by_method"].get(k, 0) + v
        return total


# --- the store ------------------------------------------------------------------


def icechunk_config(repo: Any, settings: dict[str, Any] | None) -> Any:
    """The repository's config with the benchmark's Icechunk tuning factors applied:
    ``icechunk_concurrency`` (partial-value and request concurrency) and
    ``icechunk_decodes`` (concurrent manifest decodes)."""
    config = repo.config
    settings = settings or {}
    if "icechunk_concurrency" in settings:
        config.get_partial_values_concurrency = int(settings["icechunk_concurrency"])
        config.max_concurrent_requests = int(settings["icechunk_concurrency"])
    if "icechunk_decodes" in settings:
        config.max_concurrent_decodes = int(settings["icechunk_decodes"])
    return config


def open_mirror_repo(access: str = "s3", settings: dict[str, Any] | None = None) -> Any:
    """The public Source Cooperative mirror, with Earthdata credentials for virtual chunks.

    ``access="https"`` swaps the ``asdc`` container for NASA's distribution
    endpoint with a bearer token, as the TEMPO HTTPS quickstart does, so the
    store reads from anywhere; the mirror itself is anonymous either way.
    ``settings`` may carry Icechunk tuning factors (see ``icechunk_config``).
    """
    import icechunk

    storage = icechunk.s3_storage(
        bucket=CONFIG.mirror_bucket, prefix=CONFIG.mirror_prefix, region=CONFIG.region, anonymous=True
    )
    repo = icechunk.Repository.open(storage)
    config = icechunk_config(repo, settings)
    if access == "https":
        import earthaccess_auth

        token = earthaccess_auth.login().token["access_token"]
        prefix = f"{HTTPS_ENDPOINT}/{SOURCE_BUCKET}/"
        config.set_virtual_chunk_container(
            icechunk.VirtualChunkContainer(prefix, icechunk.http_store(headers={"Authorization": f"Bearer {token}"}), name=ASDC_CONTAINER)
        )
        return repo.reopen(
            config=config,
            authorize_virtual_chunk_access=icechunk.containers_credentials({prefix: icechunk.credentials.HttpAccess}),
        )
    from earthaccess_auth.adapters.icechunk import earthdata_containers_credentials

    return repo.reopen(config=config, authorize_virtual_chunk_access=earthdata_containers_credentials(repo))


def pinned_session(repo: Any, snapshot_id: str) -> Any:
    return repo.readonly_session(snapshot_id=snapshot_id)


def resolve_pin(repo: Any) -> tuple[str, str | None]:
    """(snapshot_id, tag): the benchmark tag when it exists, else main's tip untagged."""
    if CONFIG.snapshot_tag in repo.list_tags():
        return repo.lookup_tag(CONFIG.snapshot_tag), CONFIG.snapshot_tag
    return repo.lookup_branch("main"), None
