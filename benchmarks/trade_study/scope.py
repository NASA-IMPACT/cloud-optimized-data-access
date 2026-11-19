"""The trade study's scope as data: sections, arms, datasets, workloads, metrics.

One place says what is built and what is only proposed, so the status table
in the README, ``run.py status`` and every result row agree. Statuses:

- ``built``: runs under the protocol, on at least one dataset.
- ``first-cut``: runs, but its settings have not been reviewed by the
  library's authors; never a headline until they have.
- ``analysis``: answered from chunk layouts without timing anything.
- ``proposed``: registered, not built. ``needs`` says what is missing.
- ``later``: outside this round.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArmInfo:
    name: str
    section: int  # 1 client side, 2 server side, 3 end to end
    title: str
    access_path: str
    status: str
    tuned_by: str
    needs: str = ""


# The tables below are kept one row per entry; the formatter is off until the end of the file.
# fmt: off
ARMS: tuple[ArmInfo, ...] = (
    ArmInfo("user_stack", 1, "Default user stack", "s3fs + xarray + h5netcdf, default fsspec read-ahead", "built", "As documented for users"),
    ArmInfo("tuned_h5py", 1, "Tuned h5py", "8 MiB blocks, one process per granule, page buffer when paged", "built", "NSIDC cloud-optimized HDF5 study settings"),
    ArmInfo("download", 1, "Whole-file download", "Download, then h5py on the in-memory file", "built", "—"),
    ArmInfo("virtual_icechunk", 1, "Virtual store", "Icechunk references to the original chunks", "built", "Icechunk maintainers"),
    ArmInfo("h5coro", 1, "Python h5coro", "SlideRule's cloud-optimized reader, client side", "proposed", "Its authors (to ask)",
            "h5coro as a dependency (it is not in uv.lock) and an arm written with its authors"),
    ArmInfo("sliderule_h5p", 1, "C++ H5Coro via SlideRule", "h5p endpoint, server side in us-west-2", "proposed", "Its authors (to ask)",
            "a SlideRule deployment that can read the benchmark granules"),
    ArmInfo("ros3", 1, "HDF5 2.2.0 ROS3 block cache", "libhdf5 reading S3 directly", "proposed", "Its authors (to ask)",
            "an h5py built against HDF5 2.2.0 (released 2026-07-29), where the block cache arrived: conda-forge has one (h5py 3.16.0, linux-64, py312, hdf5 >=2.2.0); "
            "the PyPI wheel in uv.lock bundles HDF5 2.0.0 and registers no ros3 driver"),
    ArmInfo("virtual_dmrpp", 1, "Virtual store from DMR++", "Same store, built from OPeNDAP's existing index", "first-cut", "OPeNDAP",
            "review by OPeNDAP; TEMPO only (GOES-19 has no sidecars); built with VirtualiZarr's DMR++ parser into a local Icechunk store"),
    ArmInfo("superchunks", 2, "Superchunks / buffer chunks", "Small chunks grouped into fewer, larger reads", "analysis", "To decide",
            "a client that issues the grouped reads; `run.py superchunks` gives the request and byte bounds meanwhile"),
    ArmInfo("s3_express", 2, "S3 Express One Zone", "Same arm, data in a single-zone bucket", "proposed", "To decide",
            "a directory bucket holding copies of the pinned granules"),
    ArmInfo("materialized_zarr", 2, "Caching service, materialized Zarr", "Proposed in the CODA deck", "later", "—"),
    ArmInfo("harmony", 3, "Harmony request", "Total time for Harmony requests from inside EC2", "proposed", "To decide", "stretch"),
    ArmInfo("appeears", 3, "AppEEARS", "", "proposed", "To decide", "stretch"),
    ArmInfo("opendap", 3, "OPeNDAP", "", "proposed", "To decide", "stretch"),
)
ARM_INFO = {a.name: a for a in ARMS}
RUNNABLE = tuple(a.name for a in ARMS if a.status in ("built", "first-cut"))
SECTIONS = {1: "Client side", 2: "Server side", 3: "End to end (stretch)"}


@dataclass(frozen=True)
class DatasetInfo:
    name: str
    title: str
    why: str
    status: str
    needs: str = ""


DATASETS: tuple[DatasetInfo, ...] = (
    DatasetInfo("tempo_no2", "TEMPO NO₂ L3 V04 (grid, ASDC)", "Reference case; virtual store exists", "built",
                "Earthdata login; run live over HTTPS only, not yet from a host in us-west-2"),
    DatasetInfo("goes19", "GOES-19 ABI L2 CMI band 13 (public, us-east-1)", "Rerunnable by anyone without credentials", "built"),
    DatasetInfo("atl03", "ICESat-2 ATL03 (photons, NSIDC)", "Swath-like; h5coro's home ground", "proposed",
                "a point-cloud question shape; the harness assumes (time, y, x) grids"),
    DatasetInfo("paged_hdf5", "A paged cloud-optimized HDF5 file", "Tests the page buffer and ROS3 cache", "proposed", "a pinned paged file"),
    DatasetInfo("gpm_imerg", "GPM_3IMERGHH_07", "So widely used", "proposed", "a dataset adapter"),
    DatasetInfo("hls", "HLS", "Also very widely used", "proposed", "a dataset adapter (COG, tiled)"),
)


@dataclass(frozen=True)
class WorkloadInfo:
    name: str
    shape: str
    sizes: tuple[int, ...]
    size_meaning: str
    use_case: str


WORKLOADS: tuple[WorkloadInfo, ...] = (
    WorkloadInfo("pancake", "One time, whole map", (1, 15), "observations scanned whole", "Visualization; in-region, few files"),
    WorkloadInfo("churro", "Time series at points", (10, 250), "points", "In-region, many files"),
    WorkloadInfo("box", "Box statistics over time", (100,), "km on a side", "In-region, many files; internal services"),
    WorkloadInfo("batch", "Every byte of the window", (0,), "(unused)", "Internal services (Harmony, Giovanni)"),
)
WORKLOAD_INFO = {w.name: w for w in WORKLOADS}

# (metric, what it measures, row field, how this harness records it)
METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("Discovery (CMR query)", "Finding the granules for the question", "discovery_seconds",
     "timed at `discover`, copied into every row; kept out of first byte"),
    ("Authentication", "Fetching temporary S3 credentials", "auth_seconds", "per trial; 0 with a reason on public data"),
    ("Manifest retrieval", "Loading the store's index, or opening the files", "manifest_seconds", "per trial"),
    ("Chunk identification", "Finding which chunks a selection needs", "chunk_id_seconds",
     "per trial where the arm exposes the step; null with a reason where it happens inside the first read"),
    ("Byte retrieval", "Reading and decoding the chunks", "byte_seconds", "per trial"),
    ("Concurrency", "Same arm at 1 and 8 workers", "arm_variant", "`session --variants`"),
    ("First byte (total)", "Sum of the four parts, for the first value of the answer", "first_byte_seconds", "`--measure first_byte` trials"),
    ("End to end", "Question to answer", "end_to_end_seconds", "`--measure full` trials"),
    ("Last byte", "All bytes in memory, for batch cases", "last_byte_seconds", "`--measure full` trials"),
    ("Warm second query", "The same read again in the same process", "warm_seconds", "per trial"),
    ("Requests and bytes on the wire", "Per run, plus the floor they imply", "request_count, bytes_requested, net_rx_bytes, floor_seconds", "per trial"),
    ("Dollars per question", "Requests, transfer and compute at list price", "cost_usd (requests_usd, transfer_usd, compute_usd)", "per trial"),
)

# fmt: on


def render_status() -> str:
    """The scope tables in Markdown, with each row's real status."""
    out: list[str] = []
    for number, title in SECTIONS.items():
        out += [
            f"### Section {number}: {title}",
            "",
            "| Arm | Name in code | Access path | Status | Who tunes it | Needs |",
            "|---|---|---|---|---|---|",
        ]
        out += [
            f"| {a.title} | `{a.name}` | {a.access_path} | {a.status} | {a.tuned_by} | {a.needs} |"
            for a in ARMS
            if a.section == number
        ]
        out.append("")
    out += ["### Datasets", "", "| Dataset | Name in code | Why | Status | Needs |", "|---|---|---|---|---|"]
    out += [f"| {d.title} | `{d.name}` | {d.why} | {d.status} | {d.needs} |" for d in DATASETS]
    out += [
        "",
        "### Workloads",
        "",
        "| Request shape | Name in code | Cases | Scoping-doc use case |",
        "|---|---|---|---|",
    ]
    for w in WORKLOADS:
        cases = ", ".join(f"`{w.name}:{s}`" for s in w.sizes) + (f" ({w.size_meaning})" if w.sizes != (0,) else "")
        out.append(f"| {w.shape} | `{w.name}` | {cases} | {w.use_case} |")
    out += ["", "### Metrics", "", "| Metric | What it measures | Row field | Recorded |", "|---|---|---|---|"]
    out += [f"| {m} | {what} | `{field}` | {how} |" for m, what, field, how in METRICS]
    return "\n".join(out) + "\n"
