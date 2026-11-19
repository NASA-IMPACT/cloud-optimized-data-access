"""The built arms: each is one way of getting the same values.

Every arm answers a question in the same four steps, so a trial can time
them apart:

1. ``authenticate``: credentials for the source bucket.
2. ``open``: load the index. A virtual store opens its repository; a file
   reader opens the files it will read first.
3. ``locate``: find the chunks the selection needs, where the arm exposes
   that as a step of its own. An arm that cannot says why in
   ``chunk_id_reason`` and the time lands in the read.
4. ``read``: fetch and decode. Returns physical values (fill → NaN).

To add an arm, subclass :class:`Arm`, add it to ``ARM_CLASSES`` at the bottom
of this file and describe it in ``scope.py``. Settings are the first TEMPO
benchmark's; each library's authors review their arm before a headline run
(see the README).
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import h5py
import numpy as np
import xarray as xr
from obspec_utils.readers import BlockStoreReader

from benchmarks.trade_study import h5
from benchmarks.trade_study.counters import (
    STATIC_MINIMUM,
    S3fsCounter,
    add_counts,
    empty_counts,
    trace_counts,
    traced_store,
)
from benchmarks.trade_study.datasets import Dataset
from benchmarks.trade_study.measure import METADATA_OBJECTS, static_minimum_requests
from benchmarks.trade_study.questions import Selection
from benchmarks.trade_study.stores import build_store, fetch_object, store_path

MIB = 2**20


@dataclass
class Result:
    """times: (observation,) datetime64[ns]; values[name]: (observation, ...) float64, fill as NaN."""

    times: np.ndarray
    values: dict[str, np.ndarray]


class Arm:
    """One access path. ``payload`` is the manifest ``run.py discover`` wrote;
    ``settings`` overrides the arm's defaults, one tuning factor at a time."""

    name: str
    auth_reason: str | None = None  # why ``authenticate`` is not a step of its own
    chunk_id_reason: str | None = None  # why ``locate`` is not a step of its own
    manifest_reason: str | None = None  # why ``open`` loads no index
    first_value_only = False  # set by a first-byte trial: set up for the question, read one value

    def __init__(self, dataset: Dataset, payload: dict[str, Any], settings: dict[str, Any] | None = None) -> None:
        self.dataset, self.payload = dataset, payload
        self.granules: list[dict[str, Any]] = payload["granules"]
        self.given = dict(settings or {})  # the overrides asked for
        self.settings: dict[str, Any] = {}  # every setting in force, recorded in the row
        self.credentials: dict[str, Any] | None = None
        self.read_split: dict[str, float] = {}  # parts of the last read, where the arm can time them

    def authenticate(self) -> None:
        self.credentials = self.dataset.auth()

    def open(self, observations: list[int]) -> None:
        """Load the index for a question over ``observations``."""

    def locate(self, observations: list[int], selection: Selection) -> bool:
        """Identify the chunks as a step of its own; False when the arm cannot."""
        return False

    def read(self, observations: list[int], selection: Selection) -> Result:
        raise NotImplementedError

    def counts(self) -> dict[str, Any] | None:
        """Requests and bytes so far (see ``counters.py``); None when nothing was counted."""
        return None

    def static_minimum(self, observations: list[int], selection: Selection) -> dict[str, Any] | None:
        """The fewest requests the selection can take, for an arm that cannot count them."""
        return None

    def close(self) -> None:
        pass


# --- helpers shared by the arms -----------------------------------------------------


def _stack(names: list[str], per_granule: list[tuple[np.datetime64, list[np.ndarray]]]) -> Result:
    """Per-granule (time, values per variable) → one Result along the observation axis."""
    times = np.asarray([when for when, _ in per_granule], dtype="datetime64[ns]")
    return Result(times, {name: np.stack([values[i] for _, values in per_granule]) for i, name in enumerate(names)})


def _indexers(y: str, x: str, selection: Selection) -> dict[str, Any]:
    """The selection as xarray ``isel`` arguments."""
    if selection.kind == "points":
        return {
            y: xr.DataArray(np.asarray(selection.y), dims="point"),
            x: xr.DataArray(np.asarray(selection.x), dims="point"),
        }
    return {y: selection.y, x: selection.x}


def _from_xarray(selected: xr.Dataset, dataset: Dataset) -> Result:
    times = np.asarray(selected[dataset.dims[0]].values, dtype="datetime64[ns]").reshape(-1)
    values = {name: np.asarray(selected[name].values, dtype=np.float64) for name in dataset.variable_names}
    return Result(times, values)


# --- what users do today ------------------------------------------------------------


class UserStack(Arm):
    """The dataset's fsspec filesystem + ``xarray.open_mfdataset(engine="h5netcdf")``,
    default settings. Opens every granule of the question before the first value."""

    name = "user_stack"
    chunk_id_reason = "inside the read: h5netcdf resolves chunks as it reads"

    def open(self, observations: list[int]) -> None:
        dataset = self.dataset
        time_dim = dataset.dims[0]
        filesystem = dataset.filesystem(self.credentials)
        self._counter: S3fsCounter | None = None
        if hasattr(filesystem, "connect"):
            filesystem.connect()
        if hasattr(filesystem, "s3"):  # only s3fs can be counted
            self._counter = S3fsCounter()
            self._counter.attach(filesystem)
        urls = [dataset.url(self.granules[o]) for o in observations]
        options: dict[str, Any] = {"engine": "h5netcdf", "combine": "nested", "concat_dim": time_dim}
        self.settings = {"engine": "h5netcdf", "group": dataset.group, "defaults": True, "files_opened": len(urls)}
        data = xr.open_mfdataset([filesystem.open(url) for url in urls], group=dataset.group, **options)
        if dataset.group:  # the time axis lives in the root group: a second open, as users do
            root = xr.open_mfdataset([filesystem.open(url) for url in urls], **options)
            data = data[dataset.variable_names].assign_coords({time_dim: root[time_dim]})
        self._data = data
        self._position = {o: i for i, o in enumerate(observations)}

    def read(self, observations: list[int], selection: Selection) -> Result:
        time_dim, y, x = self.dataset.dims
        steps = [self._position[o] for o in observations]
        selected = self._data[self.dataset.variable_names].isel({time_dim: steps})
        return _from_xarray(selected.isel(_indexers(y, x, selection)).compute(), self.dataset)

    def counts(self) -> dict[str, Any] | None:
        return self._counter.counts() if self._counter else None


# --- the baseline the HDF5 team would write -----------------------------------------


def _tuned_worker(
    store_spec: dict[str, Any],
    path: str,
    block_size: int,
    page_buf_size: int,
    paged: bool,
    h5_spec: dict[str, Any],
    selection: Selection,
) -> tuple[np.datetime64, list[np.ndarray], dict[str, Any]]:
    """Read one granule in a worker process; returns its time, values and request counts."""
    store, trace = traced_store(build_store(store_spec))
    page_buffer = {"page_buf_size": page_buf_size} if paged else {}
    with (
        BlockStoreReader(store, path, block_size=block_size) as reader,
        h5py.File(reader, "r", **page_buffer) as handle,
    ):
        when, values = h5.read_selection(handle, h5_spec, selection)
    return when, values, trace_counts(trace)


def _ready(_: int) -> int:
    """A no-op that proves a worker has started and finished its imports."""
    return 0


class TunedH5py(Arm):
    """h5py over an 8 MiB block reader; one process per granule when the
    question spans several, because h5py holds one global lock and threads
    cannot overlap its reads. The granule ``open`` loaded is read in this
    process while the workers read the others, so no granule is opened twice.
    Settings follow the NSIDC cloud-optimized HDF5 study; the page buffer
    applies only to paged files."""

    name = "tuned_h5py"

    def __init__(self, dataset: Dataset, payload: dict[str, Any], settings: dict[str, Any] | None = None) -> None:
        super().__init__(dataset, payload, settings)
        self.block_size = int(self.given.get("block_size", 8 * MIB))
        self.max_workers = int(self.given.get("max_workers", 8))
        self.page_buf_size = int(self.given.get("page_buf_size", 16 * MIB))
        self.paged = False
        self._worker_counts = empty_counts()
        self._pool: ProcessPoolExecutor | None = None
        # observation → (open file, the reader it holds open, that reader's request trace)
        self._handles: dict[int, tuple[h5py.File, Any, Any]] = {}

    def authenticate(self) -> None:
        super().authenticate()
        self.store_spec = self.dataset.store_spec(self.credentials)

    def _path(self, observation: int) -> str:
        return store_path(self.store_spec, self.granules[observation]["source_url"])

    def _handle(self, observation: int) -> h5py.File:
        """The granule, opened once in this process."""
        if observation not in self._handles:
            store, trace = traced_store(build_store(self.store_spec))
            reader = BlockStoreReader(store, self._path(observation), block_size=self.block_size)
            handle = h5py.File(reader, "r")
            if h5.is_paged(handle):
                handle.close()
                handle = h5py.File(reader, "r", page_buf_size=self.page_buf_size)
                self.paged = True
            self._handles[observation] = (handle, reader, trace)
        return self._handles[observation][0]

    def open(self, observations: list[int]) -> None:
        """The first granule in this process (its superblock and metadata are the
        index); worker processes for the rest, started here so client set-up is
        not charged to the read. A first-byte trial starts no workers."""
        self.paged = False
        self._handle(observations[0])
        wants_workers = len(observations) > 1 and self.max_workers > 1 and not self.first_value_only
        if wants_workers and self._pool is None:
            # spawn, not fork: a forked child inherits obstore's async runtime and deadlocks
            spawn = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(self.max_workers, mp_context=spawn)
            list(self._pool.map(_ready, range(self.max_workers)))
        self.settings = {
            "block_size": self.block_size,
            "max_workers": self.max_workers,
            "workers": "processes" if self._pool else "in-process",
            "page_buf_size": self.page_buf_size if self.paged else 0,
            "paged": self.paged,
        }

    def _uses_workers(self, observations: list[int]) -> bool:
        return len(observations) > 1 and self._pool is not None

    def locate(self, observations: list[int], selection: Selection) -> bool:
        if self._uses_workers(observations):
            self.chunk_id_reason = "inside the read: each process looks up its own granule's chunks"
            return False
        for o in observations:
            h5.locate_chunks(self._handle(o), self.dataset.variables, selection)
        return True

    def read(self, observations: list[int], selection: Selection) -> Result:
        h5_spec = self.dataset.h5_spec()
        names = self.dataset.variable_names
        if not self._uses_workers(observations):
            return _stack(names, [h5.read_selection(self._handle(o), h5_spec, selection) for o in observations])
        # granules already open here are read here, alongside the workers
        in_workers = {
            o: self._pool.submit(
                _tuned_worker,
                self.store_spec, self._path(o), self.block_size, self.page_buf_size, self.paged, h5_spec, selection,
            )
            for o in observations
            if o not in self._handles
        }  # fmt: skip
        per_granule = {
            o: h5.read_selection(self._handles[o][0], h5_spec, selection) for o in observations if o not in in_workers
        }
        for o, future in in_workers.items():
            when, values, counts = future.result()
            add_counts(self._worker_counts, counts)
            per_granule[o] = (when, values)
        return _stack(names, [per_granule[o] for o in observations])

    def counts(self) -> dict[str, Any] | None:
        total = empty_counts()
        add_counts(total, self._worker_counts)
        for _, _, trace in self._handles.values():
            add_counts(total, trace_counts(trace))
        return total

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        for handle, _, _ in self._handles.values():
            handle.close()


# --- the whole file into memory, then HDF5 from memory ------------------------------


class Download(Arm):
    """Each granule whole into memory in concurrent range reads, then h5py on
    the in-memory image: every byte of the file, at the network's full rate.
    Granules are fetched one at a time."""

    name = "download"
    manifest_reason = "no index: the whole file is fetched"
    chunk_id_reason = "no chunk selection: the whole file is fetched"

    def authenticate(self) -> None:
        super().authenticate()
        self.store_spec = self.dataset.store_spec(self.credentials)
        self.store, self._trace = traced_store(build_store(self.store_spec))
        self.part_bytes = int(self.given.get("part_bytes", 16 * MIB))
        self.concurrency = int(self.given.get("concurrency", 16))
        self.settings = {"part_bytes": self.part_bytes, "concurrency": self.concurrency, "parse": "h5py file image"}

    def read(self, observations: list[int], selection: Selection) -> Result:
        h5_spec, per_granule = self.dataset.h5_spec(), []
        download = decode = 0.0
        for o in observations:
            path = store_path(self.store_spec, self.granules[o]["source_url"])
            start = time.perf_counter()
            image = asyncio.run(
                fetch_object(self.store, path, part_bytes=self.part_bytes, concurrency=self.concurrency)
            )
            downloaded = time.perf_counter()
            with h5py.File.in_memory(image) as handle:
                per_granule.append(h5.read_selection(handle, h5_spec, selection))
            download, decode = download + downloaded - start, decode + time.perf_counter() - downloaded
        # decode = open the file image, read and decompress the selection: no network inside it
        self.read_split = {"download_seconds": download, "decode_seconds": decode}
        return _stack(self.dataset.variable_names, per_granule)

    def counts(self) -> dict[str, Any] | None:
        return trace_counts(self._trace)


# --- the access layer ---------------------------------------------------------------


class VirtualIcechunk(Arm):
    """An Icechunk store of references to the original chunks, read through xarray."""

    name = "virtual_icechunk"
    auth_reason = "inside manifest retrieval: the store fetches its own credentials when it opens"
    chunk_id_reason = "inside the read: Icechunk fetches the chunk manifest on the first chunk request"
    source = "hdf5"  # what the references were built from

    def authenticate(self) -> None:
        """Nothing to fetch here: ``open_virtual`` authorizes the chunk container
        itself, and credentials fetched now would be paid for and never used."""

    def _zarr_config(self) -> dict[str, Any]:
        """The zarr settings this arm asked for. They are in force only while it
        opens and reads, so two arms in one process (the gate) do not share them."""
        config: dict[str, Any] = {}
        if "zarr_concurrency" in self.given:
            # zarr-python's cap on outstanding chunk requests (default 10)
            config["async.concurrency"] = int(self.given["zarr_concurrency"])
        if self.given.get("zarrs_pipeline"):
            # zarrs decodes chunks in Rust, in parallel, without the interpreter lock. Keys are set
            # one by one: replacing the whole codec_pipeline block drops zarr's own batch_size.
            config.update(
                {
                    "codec_pipeline.path": "zarrs.ZarrsCodecPipeline",
                    "codec_pipeline.chunk_concurrent_minimum": 4,
                    "codec_pipeline.strict": False,
                }
            )
        return config

    def open(self, observations: list[int]) -> None:
        import zarr

        with zarr.config.set(self._zarr_config()):
            self._data, self._time_index = self.dataset.open_virtual(self.payload, self.given, self.source)
            concurrency = zarr.config.get("async.concurrency")
        self.settings = {
            "client": "icechunk",
            "references_from": self.source,
            "chunks": None,
            "zarr_concurrency": concurrency,
            "codec_pipeline": "zarrs" if self.given.get("zarrs_pipeline") else "zarr",
            **{key: value for key, value in self.given.items() if key.startswith("icechunk_")},
        }

    def read(self, observations: list[int], selection: Selection) -> Result:
        import zarr

        time_dim, y, x = self.dataset.dims
        selected = self._data[self.dataset.variable_names].isel({time_dim: self._time_index[observations]})
        with zarr.config.set(self._zarr_config()):
            return _from_xarray(selected.isel(_indexers(y, x, selection)).load(), self.dataset)

    def static_minimum(self, observations: list[int], selection: Selection) -> dict[str, Any] | None:
        """Metadata objects + coordinate chunks read at open + the data chunks the
        selection intersects, from the store's chunk grid. A lower bound: Icechunk
        may split one chunk into several ranged reads. Bytes are uncompressed chunk
        sizes, an upper bound on what the wire carries."""
        coordinate_chunks = 0
        for name in self.dataset.dims:
            variable = self._data[name]
            if variable.ndim:
                chunk = (variable.encoding.get("chunks") or variable.shape)[0]
                coordinate_chunks += math.ceil(variable.shape[0] / chunk)
        data_chunks, nbytes = 0, 0
        steps = [int(i) for i in self._time_index[observations]]
        for name in self.dataset.variable_names:
            variable = self._data[name]
            chunks = tuple(variable.encoding.get("chunks") or variable.shape)
            stored = np.dtype(variable.encoding.get("dtype", variable.dtype))  # the stored type, not the decoded one
            n, b = static_minimum_requests(
                tuple(variable.shape), chunks, steps, selection.y, selection.x, itemsize=stored.itemsize
            )
            data_chunks, nbytes = data_chunks + n, nbytes + b
        return {
            "request_count": METADATA_OBJECTS + coordinate_chunks + data_chunks,
            "bytes_requested": nbytes,
            "kind": STATIC_MINIMUM,
        }


class VirtualDmrpp(VirtualIcechunk):
    """The same reader over references built from OPeNDAP's DMR++ sidecars
    instead of from the HDF5 files. First cut: OPeNDAP has not reviewed it."""

    name = "virtual_dmrpp"
    source = "dmrpp"


ARM_CLASSES: dict[str, type[Arm]] = {a.name: a for a in (UserStack, TunedH5py, Download, VirtualIcechunk, VirtualDmrpp)}
