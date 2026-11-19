"""TEMPO NO₂ L3 V04 at ASDC: the reference case. Needs an Earthdata login.

``transport="s3"`` reads the bucket directly and needs a host in us-west-2;
``"https"`` reads the same objects through ASDC's distribution endpoint with
a bearer token, from anywhere.

Two virtual stores exist for it. The one built from the HDF5 files is the
public Source Cooperative mirror, read at a pinned snapshot. The one built
from OPeNDAP's DMR++ sidecars is written locally by ``build-store --source
dmrpp``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import numpy as np

from benchmarks.trade_study.datasets import latlon
from benchmarks.trade_study.datasets.base import (
    STORE_DIR,
    Credentials,
    Dataset,
    Granule,
    Grid,
    concat_granules,
    write_local_repository,
)
from benchmarks.trade_study.stores import build_store, icechunk_config

CONCEPT_ID = "C3685896708-LARC_CLOUD"
PROVIDER = "LARC_CLOUD"
SOURCE_BUCKET = "asdc-prod-protected"
SOURCE_PREFIX = f"s3://{SOURCE_BUCKET}"
# NASA's distribution endpoint serves the same objects over HTTPS from anywhere.
HTTPS_ENDPOINT = "https://data.asdc.earthdata.nasa.gov"
ASDC_CONTAINER = "asdc"  # the mirror's name for its virtual chunk container
MIRROR_BUCKET = "us-west-2.opendata.source.coop"
MIRROR_PREFIX = "pangeo/tempo-virtual-icechunk/tempo/no2/v04"
# The mirror snapshot the benchmark reads: this tag when it exists, else the tip of main.
SNAPSHOT_TAG = "bench-no2-2025-03-14"
# A granule is matched to the store's nearest time step within this tolerance.
TIME_TOLERANCE_MINUTES = 4


def https_url(s3_url: str) -> str:
    """s3://bucket/key → the distribution endpoint's URL for the same object."""
    parsed = urlparse(s3_url)
    return f"{HTTPS_ENDPOINT}/{parsed.netloc}{parsed.path}"


def fetch_s3_credentials() -> dict[str, Any]:
    """Temporary credentials for ASDC's bucket; valid in us-west-2 only. Network."""
    import earthaccess

    earthaccess.login(strategy="all", persist=True)
    credentials = earthaccess.get_s3_credentials(daac="ASDC")
    missing = [key for key in ("accessKeyId", "secretAccessKey", "sessionToken") if not credentials.get(key)]
    if missing:
        raise RuntimeError(f"ASDC credential response missing {missing}")
    return credentials


def https_client_options() -> dict[str, Any]:
    """obstore client options carrying the Earthdata bearer token. Network."""
    import earthaccess_auth
    from earthaccess_auth.adapters.obstore import http_client_options

    return http_client_options(earthaccess_auth.login())


def open_mirror_repo(transport: str = "s3", settings: dict[str, Any] | None = None) -> Any:
    """The public mirror, with Earthdata credentials for its virtual chunks.

    The mirror itself is anonymous on either transport. ``"https"`` swaps its
    ``asdc`` container for the distribution endpoint with a bearer token, as
    the TEMPO HTTPS quickstart does. ``settings`` may carry Icechunk tuning
    factors (see :func:`benchmarks.trade_study.stores.icechunk_config`). Network.
    """
    import icechunk

    storage = icechunk.s3_storage(bucket=MIRROR_BUCKET, prefix=MIRROR_PREFIX, region=TempoNo2.region, anonymous=True)
    repo = icechunk.Repository.open(storage)
    config = icechunk_config(repo, settings)
    if transport == "https":
        import earthaccess_auth

        token = earthaccess_auth.login().token["access_token"]
        prefix = f"{HTTPS_ENDPOINT}/{SOURCE_BUCKET}/"
        store = icechunk.http_store(headers={"Authorization": f"Bearer {token}"})
        config.set_virtual_chunk_container(icechunk.VirtualChunkContainer(prefix, store, name=ASDC_CONTAINER))
        access = icechunk.containers_credentials({prefix: icechunk.credentials.HttpAccess})
        return repo.reopen(config=config, authorize_virtual_chunk_access=access)
    from earthaccess_auth.adapters.icechunk import earthdata_containers_credentials

    return repo.reopen(config=config, authorize_virtual_chunk_access=earthdata_containers_credentials(repo))


def resolve_pin(repo: Any) -> tuple[str, str | None]:
    """(snapshot id, tag): the benchmark tag when it exists, else main's tip untagged."""
    if SNAPSHOT_TAG in repo.list_tags():
        return repo.lookup_tag(SNAPSHOT_TAG), SNAPSHOT_TAG
    return repo.lookup_branch("main"), None


def match_store_times(store_times: np.ndarray, granules: list[Granule]) -> np.ndarray:
    """The store's time index for each granule: the nearest time step within the
    tolerance. Raises when a granule has no step, or two granules share one."""
    import pandas as pd

    in_store = pd.DatetimeIndex(store_times).tz_localize("UTC")
    nominal = pd.DatetimeIndex(pd.to_datetime([granule["start_time"] for granule in granules], utc=True))
    tolerance = pd.Timedelta(minutes=TIME_TOLERANCE_MINUTES)
    indices = in_store.get_indexer(nominal, method="nearest", tolerance=tolerance)
    if np.any(indices < 0):
        missing = [granule["granule_ur"] for granule, index in zip(granules, indices, strict=True) if index < 0]
        raise RuntimeError(f"Event granules missing from the store snapshot: {missing}")
    if len(set(indices.tolist())) != len(indices):
        raise RuntimeError("two granules map to the same store time step")
    return indices


class TempoNo2(Dataset):
    name = "tempo_no2"
    region = "us-west-2"
    variables = ("product/vertical_column_troposphere", "product/main_data_quality_flag")
    group = "product"
    time_path = "time"
    time_epoch = "1980-01-06T00:00:00"
    dims = ("time", "latitude", "longitude")
    index_location = "s3 (public mirror, us-west-2)"
    transports = ("s3", "https")
    # (start, end) of the search, from the 14 March 2025 event day
    windows: ClassVar[dict[str, tuple[str, str]]] = {
        "1d": ("2025-03-14T00:00:00Z", "2025-03-15T00:00:00Z"),
        "1w": ("2025-03-14T00:00:00Z", "2025-03-21T00:00:00Z"),
        "1m": ("2025-03-14T00:00:00Z", "2025-04-14T00:00:00Z"),
    }

    def __init__(self, transport: str = "s3") -> None:
        self.transport = transport

    def url(self, granule: Granule) -> str:
        return https_url(granule["source_url"]) if self.transport == "https" else granule["source_url"]

    # --- pinned inputs -----------------------------------------------------------

    def discover(self, window: str) -> list[Granule]:
        """A CMR search."""
        import earthaccess

        earthaccess.login(strategy="all", persist=True)
        found = earthaccess.search_data(
            concept_id=CONCEPT_ID, temporal=self.windows[window], count=-1, sort_key="start_date"
        )
        records = []
        for granule in found:
            umm = granule["umm"]
            temporal = umm["TemporalExtent"]["RangeDateTime"]
            archive = umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation", [])
            netcdf_file = next((item for item in archive if item.get("Name", "").endswith(".nc")), {})
            links = [link for link in granule.data_links(access="direct") if link.endswith(".nc")]
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
        return sorted(records, key=lambda record: record["start_time"])

    def describe_grid(self, granules: list[Granule]) -> Grid:
        """The mirror's grid, and the snapshot the manifest pins."""
        import xarray as xr

        repo = self.open_repo({})
        snapshot_id, _ = resolve_pin(repo)
        dataset = xr.open_zarr(repo.readonly_session(snapshot_id=snapshot_id).store, consolidated=False, chunks=None)
        match_store_times(dataset["time"].values, granules)  # every pinned granule must be in the snapshot
        grid = latlon.grid_spec(dataset["latitude"].values, dataset["longitude"].values)
        return {**grid, "snapshot_id": snapshot_id}

    # --- questions ---------------------------------------------------------------

    def index_points(self, grid: Grid, lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lat, lon = latlon.grid_arrays(grid)
        return latlon.nearest_indices(lat, lats), latlon.nearest_indices(lon, lons)

    def index_window(self, grid: Grid, bounds: tuple[float, float, float, float]) -> tuple[slice, slice]:
        lat, lon = latlon.grid_arrays(grid)
        return latlon.coordinate_slice(lat, *bounds[:2]), latlon.coordinate_slice(lon, *bounds[2:])

    def full_window(self, grid: Grid) -> tuple[slice, slice]:
        return slice(0, int(grid["latitude"]["size"])), slice(0, int(grid["longitude"]["size"]))

    # --- access ------------------------------------------------------------------

    def auth(self) -> Credentials:
        """S3: temporary bucket credentials. HTTPS: the bearer-token client options."""
        if self.transport == "https":
            return {"client_options": https_client_options()}
        return fetch_s3_credentials()

    def store_spec(self, credentials: Credentials) -> dict[str, Any]:
        if self.transport == "https":
            options = (credentials or {}).get("client_options") or https_client_options()
            return {"kind": "http", "base_url": HTTPS_ENDPOINT, "client_options": options}
        return {"kind": "s3", "bucket": SOURCE_BUCKET, "prefix": "", "region": self.region, "credentials": credentials}

    def filesystem(self, credentials: Credentials) -> Any:
        import earthaccess

        earthaccess.login(strategy="all", persist=True)
        if self.transport == "https":
            return earthaccess.get_fsspec_https_session()
        return earthaccess.get_s3_filesystem(provider=PROVIDER)

    # --- the virtual stores ------------------------------------------------------

    def open_repo(self, settings: dict[str, Any]) -> Any:
        """The mirror. Tests replace this with a local repository."""
        tuning = {key: value for key, value in settings.items() if key.startswith("icechunk_")}
        return open_mirror_repo(self.transport, tuning)

    def open_virtual(
        self, payload: dict[str, Any], settings: dict[str, Any], source: str = "hdf5"
    ) -> tuple[Any, np.ndarray]:
        import xarray as xr

        if source == "dmrpp":
            return self._open_dmrpp(payload, settings)
        session = self.open_repo(settings).readonly_session(snapshot_id=payload["snapshot_id"])
        dataset = xr.open_zarr(session.store, consolidated=False, chunks=None)
        return dataset, match_store_times(dataset["time"].values, payload["granules"])

    def dmrpp_store_path(self, window: str) -> Path:
        return STORE_DIR / f"{self.name}-{window}-dmrpp-{self.transport}"

    def _chunk_prefix(self) -> str:
        """The URL prefix the DMR++ store's chunk references start with."""
        return f"{HTTPS_ENDPOINT}/{SOURCE_BUCKET}/" if self.transport == "https" else f"{SOURCE_PREFIX}/"

    def _chunk_access(self, credentials: Credentials) -> tuple[Any, Any]:
        """(virtual chunk container, its credentials) for this transport. The
        bearer token is given at open time only; it is never written to the repository."""
        import icechunk

        prefix = self._chunk_prefix()
        if self.transport == "https":
            headers = dict(((credentials or {}).get("client_options") or {}).get("default_headers") or {})
            container = icechunk.VirtualChunkContainer(prefix, icechunk.http_store(headers=headers or None))
            return container, icechunk.containers_credentials({prefix: icechunk.credentials.HttpAccess})
        keys = credentials or {}
        container = icechunk.VirtualChunkContainer(prefix, icechunk.s3_store(region=self.region))
        access = icechunk.s3_credentials(
            access_key_id=keys["accessKeyId"],
            secret_access_key=keys["secretAccessKey"],
            session_token=keys["sessionToken"],
        )
        return container, icechunk.containers_credentials({prefix: access})

    def build_virtual_store(self, payload: dict[str, Any], source: str = "hdf5") -> tuple[str, Path]:
        """From DMR++: reads one sidecar and the three coordinate arrays per granule."""
        import icechunk
        from obspec_utils.registry import ObjectStoreRegistry
        from virtualizarr import open_virtual_dataset
        from virtualizarr.parsers import DMRPPParser

        if source != "dmrpp":
            raise ValueError(f"{self.name} reads an existing virtual store; nothing to build (try --source dmrpp)")
        root_url = HTTPS_ENDPOINT if self.transport == "https" else SOURCE_PREFIX
        registry = ObjectStoreRegistry({root_url: build_store(self.store_spec(self.auth()))})
        parts = []
        for granule in payload["granules"]:
            sidecar = self.url(granule) + ".dmrpp"
            root = open_virtual_dataset(sidecar, registry, DMRPPParser(), loadable_variables=list(self.dims))
            product = open_virtual_dataset(sidecar, registry, DMRPPParser(group=self.group))[self.variable_names]
            parts.append(product.assign_coords({dim: root[dim] for dim in self.dims}))
        path = self.dmrpp_store_path(payload["window"])
        bare_store = icechunk.http_store() if self.transport == "https" else icechunk.s3_store(region=self.region)
        container = icechunk.VirtualChunkContainer(self._chunk_prefix(), bare_store)
        message = f"{len(parts)} granules from DMR++ sidecars, window {payload['window']}"
        snapshot = write_local_repository(
            path, concat_granules(parts, self.dims[0]), container, message, validate_containers=False
        )
        return snapshot, path

    def _open_dmrpp(self, payload: dict[str, Any], settings: dict[str, Any]) -> tuple[Any, np.ndarray]:
        import icechunk
        import xarray as xr

        path = self.dmrpp_store_path(payload["window"])
        snapshot = (payload.get("dmrpp_snapshot_ids") or {}).get(self.transport)
        if not path.exists() or not snapshot:
            raise FileNotFoundError(
                f"no DMR++ store for {self.transport}: run `run.py build-store --dataset {self.name} "
                f"--window {payload['window']} --source dmrpp --transport {self.transport}`"
            )
        container, access = self._chunk_access(self.auth())
        storage = icechunk.local_filesystem_storage(str(path))
        config = icechunk_config(icechunk.Repository.open(storage), settings)
        config.set_virtual_chunk_container(container)
        repo = icechunk.Repository.open(storage, config=config, authorize_virtual_chunk_access=access)
        session = repo.readonly_session(snapshot_id=snapshot)
        dataset = xr.open_zarr(session.store, consolidated=False, zarr_format=3, chunks=None)
        return dataset, np.arange(len(payload["granules"]))
