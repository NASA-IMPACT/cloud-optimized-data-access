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
| `netcdf_user_stack` (opt-in: `--arms`) | earthaccess's filesystem + `xarray.open_mfdataset(engine="h5netcdf", group="product")`, default settings (fsspec's read-ahead cache fetches a block per HDF5 metadata read; a second open of the root group supplies the time axis). Slow by design, so not in the default arm set | user stack → `virtual_icechunk` | what a user gains today |
| `netcdf_tuned` | h5py over an 8 MiB block reader, one **process** per granule (8 at a time; h5py holds one global lock, so threads cannot overlap network reads), points batched per chunk, 16 MiB page buffer when the file is paged | tuned → `virtual_icechunk` | the access layer against a competent client |
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
  objects, the coordinate chunks read at open, and the data chunks the
  selection intersects, all computed from the store's own chunk metadata
  at run time. It is reported as "≥ minimum, not instrumented". Once per
  session `virtual_manifeststore` reads the same selection through a traced
  registry; its count must equal the static **data-chunk** count (the
  ManifestStore path reads coordinates and metadata through the parsed
  session, outside the tracer), which is what the local test asserts.

## Measurement

Every trial is a fresh process. `cold` builds the backend inside the timed
region; `warm_process` builds it, runs one discarded selection, and then
times a second one, so caches, pools and connections are warm. Each arm
opens its own store and repository inside the timed region; nothing is
pre-opened. The `noop` arm, once per session, measures the per-trial
fixed cost that remains (process start, imports, credential fetch) and is
the baseline subtracted from each session's network byte counts. Each row
records:

- phases: `catalog_seconds` (CMR search, measured by `run.py catalog` in
  unpinned trials the orchestrator schedules per window; the summary adds
  the per-window median to the file arms' first phase; the virtual arm's
  coordinate lookup is inside its open and is shown as 0), `open_seconds`,
  `retrieval_seconds`, `processing_seconds`, `end_to_end_seconds`;
- resources: CPU user and system seconds and peak RSS from `getrusage` in
  the trial process; load average and CPU steal around the trial;
- transport: `request_count` and `bytes_requested`, with
  `request_count_reason` saying what was counted. Three kinds, kept apart
  in the summary as `request_count_kind`:
  - `client-ranges` (tuned and ManifestStore arms): obspec-utils' tracer
    records Python calls into obstore, one per byte range. obstore's Rust
    side can coalesce adjacent ranges into fewer HTTP requests, split
    large ones, and retry, none of which the tracer sees
    (developmentseed/obstore#105 tracks exposing real request metrics).
    So this is a count of ranges asked for, not of HTTP requests, and the
    floor model that uses it is approximate for these arms.
  - `http-calls` (user stack): a botocore `after-call` hook, one per
    completed S3 operation, with retries from the response metadata; bytes
    counted for GetObject only.
  - `static-minimum` (Icechunk): computed from the store's chunk grid. Its
    `bytes_requested` is the sum of uncompressed chunk sizes, an upper
    bound; `net_rx_bytes` is what the wire carried.
  Bytes on the wire are measured independently of all three as
  `net_rx_bytes` on a dedicated host. CPU time covers every thread, Rust
  included, and the tuned arm's worker processes, because it comes from
  `getrusage` for the process and its waited-on children. `requests_by_method`,
  `retries` (botocore `ResponseMetadata.RetryAttempts` for the user stack;
  null with a reason for the Rust clients, which do not surface retries to
  Python), and `net_rx_bytes` on a dedicated host, minus the session's
  `noop` row;
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

## Transports

`--access s3` (default) reads the granules from ASDC's bucket and needs
us-west-2. `--access https` reads the same objects from NASA's
distribution endpoint with an Earthdata bearer token: the Icechunk arm
swaps the store's `asdc` container for an HTTPS one (the TEMPO HTTPS
quickstart's method), the tuned arm uses an HTTP object store with the
same token, and the user stack uses earthaccess's HTTPS filesystem. Every
row carries `access`, and cells and speedups are keyed by it, so HTTPS
numbers never mix with in-region ones. Over HTTPS the user-stack arm is
not instrumented for request counts; the smoke tripwire keeps a separate
`expected/requests-https.json`.

## Hosts

| Step | Host | Why |
|---|---|---|
| `probe`, `gate`, `smoke`, `orchestrate --estimate` | CodeBuild or the JupyterHub | correctness and request counts do not depend on the host |
| headline sessions | one dedicated us-west-2 EC2 instance, same type for all three sessions | timing needs a quiet host: `load_avg_1m` below 1 and `cpu_steal_s` near 0 on every row, or the session is discarded |
| a first session on the Hub | rows carry `host_kind=jupyterhub` | the node is shared, so load and steal are node-wide; the pod's own network counters are still recorded. Label such numbers as one shared-host session |
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
