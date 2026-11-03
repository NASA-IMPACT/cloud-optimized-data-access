# TEMPO NO₂ event access: file-based netCDF vs Zarr as an access layer

The protocol for this benchmark. The code is in this directory; results
live under `results/tempo_no2/`; the talk that uses them is
`decks/coda-presentation/`.

## The question

A user asks for TEMPO tropospheric NO₂ over one event day (14 March 2025),
as a time series at a handful of points and as statistics over a box, then
the same question over a week and a month. How long does the answer take,
how many requests and bytes does it cost, and why, when the same granules
are read file by file versus through a Zarr index over the identical bytes?

The claim under test is not "Zarr the format beats netCDF the format". The
virtual store contains references to the original HDF5 chunks; nothing is
copied or re-encoded. What changes is the access layer.

## Who the workload represents

An analyst with an Earthdata login, on a machine in us-west-2 (the VEDA
JupyterHub, or an EC2 instance), asking in coordinates, not indices. The
headline metric is end-to-end seconds from "I have a question" to "I have
the numbers". Request count and bytes are co-primary because they are what
the previous talks in this series (SlideRule, HDF5, earthaccess) reduce
their results to: requests × per-request latency.

## Arms and what each comparison isolates

| Arm | Access path | Comparison | Isolates |
|---|---|---|---|
| `netcdf_user_stack` | `earthaccess.open()` + `xarray.open_mfdataset(engine="h5netcdf")`, default settings | user stack → `virtual_icechunk` | what a user gains today (headline) |
| `netcdf_tuned` | h5py over an 8 MiB block reader, granules read concurrently (8 workers), points batched per chunk, 16 MiB page buffer when the file is paged | tuned → `virtual_icechunk` | the access layer against a competent client |
| `netcdf_tuned` at 1 worker | the same, serial over granules | 1 worker → 8 workers | concurrency alone |
| `virtual_icechunk` | Icechunk store of VirtualiZarr references, opened at the pinned snapshot from the public Source Cooperative mirror, `xarray` selection | | the access layer as deployed |
| `virtual_manifeststore` | the same references through VirtualiZarr's `ManifestStore` with a request tracer | once per session | validates the Icechunk arm's static request minimum; never a headline |
| `noop` | builds nothing | once per session | the per-trial fixed cost (process start, imports, credentials) and the network-bytes baseline |

## Baseline settings and their source

The tuned arm follows the NSIDC cloud-optimized HDF5 study (Lopez, Barrett,
Jelenak, Lee, 2025): an 8 MiB block cache and a 16 MiB HDF5 page buffer.
The page buffer applies only to files written with the paged file-space
strategy; `run.py probe` checks the first granule and every row records
`arm_settings.paged`. If TEMPO L3 V04 granules are not paged, the page
buffer is not applied and the row says so.

The user-stack arm is deliberately untuned: it is the stack the earthaccess
documentation and the study's own discussion thread describe users running.

## Assumptions

- Region us-west-2. ASDC's temporary S3 credentials are rejected elsewhere.
- Quality rule: keep NO₂ where `main_data_quality_flag == 0` (V04: normal).
  Provisional; confirm against the product guide.
- AOI: a square of the stated side length centred on 36.98° N, 95.97° W;
  points are a seeded uniform sample inside the 500 km square (seed
  20250314), nested so the 10-point set is the first 10 of the 250.
- Granule-to-store time matching: nearest store time within 4 minutes,
  one-to-one; the maximum offset is recorded.
- The Icechunk arm's request count is a **static minimum**: 4 metadata
  objects (repository config, snapshot, manifest, `zarr.json`) plus the
  chunk references the selection intersects, computed from the chunk grid
  (`(1, 738, 1938)` for float64, `(1, 984, 2584)` for the flag). It is
  reported as "≥ minimum, not instrumented" and checked once per session
  against `virtual_manifeststore`'s traced count.

## Measurement

Every trial is a fresh process (`cold`) or a reused backend in one process
(`warm_process`). Each row records:

- phases: `catalog_seconds` (CMR search, measured by `run.py catalog` in
  unpinned trials; the pinned trials read a committed manifest),
  `open_seconds`, `retrieval_seconds`, `processing_seconds`,
  `end_to_end_seconds`;
- resources: CPU user and system seconds and peak RSS from `getrusage` in
  the trial process; load average and CPU steal around the trial;
- transport: `request_count` and `bytes_requested` (obspec-utils tracer
  for the tuned and ManifestStore arms; botocore event hooks for the user
  stack; the static minimum for Icechunk), `requests_by_method`, `retries`
  (botocore retry events; `obstore`/`icechunk` retry log lines; null with
  a reason when a library does not log them), and `net_rx_bytes` on a
  dedicated host, minus the session's `noop` row;
- the floor: `serial_phases × median RTT + bytes ÷ measured bandwidth`,
  from `run.py probe` at the start of each session (20 HEADs and 5 × 16 MiB
  range reads against the source bucket; HEADs against the mirror).
  Serial phases: for the virtual arm, the metadata objects plus one round
  of concurrent chunk reads; for the file arms, one granule's mean request
  chain, because granules are read concurrently. `floor_ratio` is
  end-to-end over floor: the "why not double?" number;
- cost: S3 GET requests at list price (`$0.0004` per 1,000, us-west-2,
  2026-11-02) plus instance hours (`m6i.xlarge` on-demand by default;
  `--instance-rate-usd-h` to change). In-region egress is zero;
- provenance: git SHA, `uv.lock` hash, package versions, snapshot id and
  tag, manifest path, host kind, instance type and AZ (IMDS on EC2),
  CPU model, core count, memory.

The orchestrator samples the host once a second from its own process while
each trial runs (per-core CPU, network bytes, steal) and stores the
timeline next to the row. The summary derives `cpu_fraction`,
`bandwidth_fraction`, the coefficient of variation, and a `likely_limiter`
label per cell (`cpu-one-core`, `cpu-all-cores`, `network-bandwidth`,
`latency`, `unexplained`).

## Pinning

`run.py discover` writes the manifest of granules for a window and records
the store snapshot it will read: the tag `bench-no2-2025-03-14` when it
exists on the mirror, else the tip of `main`. The manifest is committed.
Creating the tag is an operation in the pipeline repository with write
credentials; the benchmark needs read only. A run without the tag is
reproducible only until the pipeline's garbage collector expires the
snapshot (30 days after it stops being a branch tip). The mirror is never
pruned, so an already-mirrored snapshot survives upstream expiry.

## Gate

Before any timing: exact equality (times, shapes, dtypes, flags, NaN
masks, values, `atol = 0`) between each file arm and the virtual arm. For
the one-day manifest: every observation for the 1-point and 500 km cases,
observation 0 elsewhere. For the week and month windows: the first, last
and four random observations per case. An observation that is all-NaN on
both sides is `vacuous`, excluded, never a pass. Any failure blocks the
run.

## Statistics

Per cell: n, median, p90, min, max, coefficient of variation. Speedups are
paired by session and repetition; the 95% interval is a t-interval on
`log(speedup)` (bootstrap on the median when n < 10). Headline cells run
n = 10 in three sessions; the session-to-session spread of the per-session
medians is reported and a delta inside that spread is "no detectable
difference".

## Hosts

| Step | Host | Why |
|---|---|---|
| `probe`, `gate`, `smoke`, `orchestrate --estimate` | CodeBuild or the JupyterHub | correctness and request counts do not depend on the host |
| headline sessions | one dedicated us-west-2 EC2 instance, same type for all three sessions | timing needs a quiet host: `load_avg_1m` below 1 and `cpu_steal_s` near 0 on every row, or the session is discarded |
| analysis and figures | anywhere | reads committed results |

## What would change the conclusion

- The tuned arm matching the virtual arm **per question**, not just per
  file, over the month-long window.
- A catalog path for files that is as cheap as a coordinate lookup.
- Cloud-optimized, repacked TEMPO granules, which would narrow the per-file
  gap the NSIDC study describes.
- A measured Icechunk request count materially above the static minimum.

## Unmeasured

- OPeNDAP, and any server-side subsetting service.
- Point and trajectory data.
- Concurrent users; this is one client at a time.

## Benchmark checklist (filled in after the first run)

- Why not double? _pending: floor ratios per cell_
- Did it break any limits? _pending: likely_limiter per cell_
- Did it error? _pending: failed rows and retries_
- Did it reproduce? _pending: session spread_
- Does it matter? _pending: the per-question delta over one month_
- Is it sunny-day only? Yes: one client, no throttling, no failures injected.

## Reviewed by

_(baseline settings checked with:)_
