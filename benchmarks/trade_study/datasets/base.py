"""What the harness needs to know about a product.

A dataset here is a stack of gridded granules, one observation per file, with
one or more (time, y, x) variables. To add one, subclass :class:`Dataset` in
a new module, fill in every method that raises ``NotImplementedError``, and
register the class in ``datasets/__init__.py`` and ``scope.py``.

Point-cloud and swath products (ATL03) do not fit this shape yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from benchmarks.trade_study.questions import Case, Selection, box_bounds, question_points

PACKAGE_DIR = Path(__file__).resolve().parents[1]
MANIFEST_DIR = PACKAGE_DIR / "manifests"
STORE_DIR = PACKAGE_DIR / "stores"  # locally built virtual stores; not committed

Granule = dict[str, Any]
Grid = dict[str, Any]
Credentials = dict[str, Any] | None


class Dataset:
    name: ClassVar[str]
    region: ClassVar[str]  # the AWS region the data live in
    variables: ClassVar[tuple[str, ...]]  # HDF5 paths
    group: ClassVar[str | None] = None  # the group xarray opens; None for the root
    time_path: ClassVar[str]
    time_epoch: ClassVar[str]
    dims: ClassVar[tuple[str, str, str]]  # (time, y, x) names in xarray
    public: ClassVar[bool] = False
    discovery_kind: ClassVar[str] = "cmr"
    index_location: ClassVar[str]  # where the virtual store's index is read from
    probe_range_mib: ClassVar[int] = 16  # bandwidth probe read size; five must fit in one granule
    transports: ClassVar[tuple[str, ...]] = ("s3",)
    windows: ClassVar[dict[str, Any]]  # window name → whatever ``discover`` needs to find its granules
    # s3: direct, in the data's region; https: the DAAC's distribution endpoint, from anywhere
    transport: str = "s3"

    @property
    def variable_names(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path in self.variables]

    def url(self, granule: Granule) -> str:
        """The granule's address on this transport."""
        return granule["source_url"]

    # --- pinned inputs -----------------------------------------------------------

    def manifest_path(self, window: str) -> Path:
        return MANIFEST_DIR / f"{self.name}-{window}.json"

    def discover(self, window: str) -> list[Granule]:
        """The window's granules, oldest first. Network."""
        raise NotImplementedError

    def describe_grid(self, granules: list[Granule]) -> Grid:
        """What ``index_points`` and ``index_window`` need, as JSON, for the manifest.
        May include ``snapshot_id`` when the dataset reads an existing virtual store. Network."""
        raise NotImplementedError

    # --- questions ---------------------------------------------------------------

    def index_points(self, grid: Grid, lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    def index_window(self, grid: Grid, bounds: tuple[float, float, float, float]) -> tuple[slice, slice]:
        raise NotImplementedError

    def full_window(self, grid: Grid) -> tuple[slice, slice]:
        raise NotImplementedError

    def selection(self, case: Case, grid: Grid) -> Selection:
        """The grid indices ``case`` reads in every observation."""
        if case.workload == "churro":
            return Selection("points", *self.index_points(grid, *question_points(case.size)))
        if case.workload == "box":
            return Selection("window", *self.index_window(grid, box_bounds(case.size)))
        return Selection("window", *self.full_window(grid))

    @staticmethod
    def observations(case: Case, n_granules: int) -> list[int]:
        """Indices into the pinned granule list that ``case`` reads."""
        return list(range(min(case.size, n_granules) if case.workload == "pancake" else n_granules))

    # --- access ------------------------------------------------------------------

    def auth(self) -> Credentials:
        """Temporary credentials for the source bucket; None on public data. Network."""
        raise NotImplementedError

    def store_spec(self, credentials: Credentials) -> dict[str, Any]:
        """A spec for :func:`benchmarks.trade_study.stores.build_store`."""
        raise NotImplementedError

    def filesystem(self, credentials: Credentials) -> Any:
        """The fsspec filesystem a user would be handed."""
        raise NotImplementedError

    def open_virtual(
        self, payload: dict[str, Any], settings: dict[str, Any], source: str = "hdf5"
    ) -> tuple[Any, np.ndarray]:
        """(xarray dataset over the virtual store, the store's time index for each pinned granule).
        ``source`` names what the references were built from: "hdf5" or "dmrpp"."""
        raise NotImplementedError

    def h5_spec(self) -> dict[str, Any]:
        """What a worker process needs to read a granule with h5py."""
        return {"variables": self.variables, "time_path": self.time_path, "time_epoch": self.time_epoch}

    # --- building a virtual store (only where none exists to read) ---------------

    def build_virtual_store(self, payload: dict[str, Any], source: str = "hdf5") -> tuple[str, Path]:
        """Write virtual references for the pinned granules to a local Icechunk
        repository. Returns (snapshot id, the repository's path). Network.

        Datasets that cannot build from ``source`` raise ``ValueError`` saying why.
        """
        raise ValueError(f"{self.name} has no virtual store to build from {source}")


def write_local_repository(path: Path, virtual_dataset: Any, container: Any, message: str, **to_icechunk: Any) -> str:
    """Replace the Icechunk repository at ``path`` with ``virtual_dataset``'s
    references, readable through ``container``. Returns the snapshot id."""
    import shutil

    import icechunk

    shutil.rmtree(path, ignore_errors=True)
    config = icechunk.RepositoryConfig.default()
    config.set_virtual_chunk_container(container)
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(path)), config=config)
    session = repo.writable_session("main")
    virtual_dataset.vz.to_icechunk(session.store, **to_icechunk)
    return session.commit(message)


def concat_granules(parts: list[Any], time_dim: str) -> Any:
    """Per-granule virtual datasets joined along time, keeping the first one's coordinates and attributes."""
    import xarray as xr

    return xr.concat(parts, dim=time_dim, coords="minimal", compat="override", combine_attrs="override")
