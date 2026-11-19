# Trade-study benchmark: a minimum viable version

The one harness for the Cloud-Optimized Data Access trade study's scope: any
dataset, any arm, the same questions, with time to first byte reported in
parts. It replaced the first benchmark, which could only read TEMPO NO₂
(see [What came from the first benchmark](#what-came-from-the-first-benchmark)).

New here? Read [How the code is laid out](#how-the-code-is-laid-out), then
run the [no-credentials example](#reproduce-no-credentials).

What this version does:

- holds the scope as data (`scope.py`), so `run.py status` prints what is
  built and what is only proposed, and proposed arms refuse to run with the
  reason;
- runs the four built client-side arms on two datasets: **GOES-19** (public,
  no credentials) and **TEMPO NO₂** (through the existing access code), plus
  a first-cut fifth arm on TEMPO, a virtual store built from OPeNDAP's DMR++
  sidecars. Both datasets have been run live from a workstation: GOES-19
  over S3, TEMPO over HTTPS. Nothing has been run in us-west-2 yet;
- times every trial in the scope's parts and records requests, bytes, a
  floor, and dollars;
- gates every arm on returning the same values before its timings count;
- bounds what superchunks can buy, from a granule's real chunk index;
- reports medians, paired speedups and one figure per case.

## How the code is laid out

A run goes left to right through the subcommands of `run.py`, and each
subcommand is a thin wrapper around one module:

```
discover ──► build-store ──► probe ──► gate ──► session ──► report
(pin the     (only where no  (the      (do the  (time it;   (medians,
 granules)    store exists)   link)     arms     one fresh   speedups,
                                        agree?)  process     figures)
                                                 per trial)
```

| File | What is in it |
|---|---|
| `scope.py` | The trade study's scope as data: every arm, dataset, workload and metric, built or only proposed. `run.py status` prints it. |
| `questions.py` | What is asked: the seeded points and the boxes, and `Case` (`workload:size:window`). |
| `datasets/` | One module per product: `goes19.py`, `tempo_no2.py`. `base.py` is the interface; `latlon.py` helps with regular grids. |
| `arms.py` | One class per access path: `UserStack`, `TunedH5py`, `Download`, `VirtualIcechunk`, `VirtualDmrpp`. |
| `h5.py`, `stores.py` | Shared reading code: one HDF5 granule with h5py; object stores from a plain description. |
| `trial.py` | One timed trial, and `Row`, the schema of `results.jsonl`. |
| `counters.py`, `measure.py`, `probes.py`, `host.py` | What a row records besides time: requests, CPU and memory, the link's speed, the machine and the code. |
| `gate.py` | The correctness gate. |
| `session.py` | The plan (which trials, in what order) and the subprocess that runs each. Running a run id again resumes it: trials with an `ok` row are skipped. |
| `report.py`, `figures.py` | `results.jsonl` → `summary.json`, `summary.md`, `figures/`. |
| `superchunks.py` | Arithmetic on chunk layouts; times nothing. |
| `legacy.py` | Reads results written by the first benchmark. Temporary; see below. |
| `run.py` | The command line. Parses arguments and writes files, nothing else. |
| `paths.py` | Where manifests, gates, probes and results are filed. |

Tests are in `tests/trade_study/`, one file per area, and never touch the
network: they run every arm against synthetic TEMPO-shaped granules and a
local virtual store (`tests/trade_study/fixtures.py`).

### Adding a dataset

1. Copy `datasets/goes19.py` (public data) or `datasets/tempo_no2.py`
   (Earthdata login) and fill in each method `datasets/base.py` lists.
2. Add the class to `DATASETS` in `datasets/__init__.py`.
3. Set its status to `built` in `scope.py`. A test fails until steps 2 and
   3 agree.
4. `discover`, then `gate`. An arm's timings do not count until it passes.

### Adding an arm

1. Subclass `Arm` in `arms.py`: `open`, `read`, and `locate` if the library
   can find chunks as a step of its own.
2. Add the class to `ARM_CLASSES` at the bottom of `arms.py` and set the
   arm's status in `scope.py`.
3. Add its name to `ARMS` in `tests/trade_study/test_arms.py`, so the tests
   check it returns the reference arm's values.

### Changing what a row records

Add the field to `Row` in `trial.py` and fill it in `run_trial`. If the
report should show it, add it to `MEDIANS` in `report.py`. Rows written
before the change still load: a missing field reads as null.

## Status against the scope

`uv run python -m benchmarks.trade_study.run status` prints these tables
from `scope.py`.

### Section 1: Client side

| Arm | Name in code | Access path | Status | Who tunes it | Needs |
|---|---|---|---|---|---|
| Default user stack | `user_stack` | s3fs + xarray + h5netcdf, default fsspec read-ahead | built | As documented for users |  |
| Tuned h5py | `tuned_h5py` | 8 MiB blocks, one process per granule, page buffer when paged | built | NSIDC cloud-optimized HDF5 study settings |  |
| Whole-file download | `download` | Download, then h5py on the in-memory file | built | — |  |
| Virtual store | `virtual_icechunk` | Icechunk references to the original chunks | built | Icechunk maintainers |  |
| Python h5coro | `h5coro` | SlideRule's cloud-optimized reader, client side | proposed | Its authors (to ask) | h5coro as a dependency (it is not in uv.lock) and an arm written with its authors |
| C++ H5Coro via SlideRule | `sliderule_h5p` | h5p endpoint, server side in us-west-2 | proposed | Its authors (to ask) | a SlideRule deployment that can read the benchmark granules |
| HDF5 2.2.0 ROS3 block cache | `ros3` | libhdf5 reading S3 directly | proposed | Its authors (to ask) | an h5py built against HDF5 2.2.0 (released 2026-07-29), where the block cache arrived: conda-forge has one (h5py 3.16.0, linux-64, py312, hdf5 >=2.2.0); the PyPI wheel in uv.lock bundles HDF5 2.0.0 and registers no ros3 driver |
| Virtual store from DMR++ | `virtual_dmrpp` | Same store, built from OPeNDAP's existing index | first-cut | OPeNDAP | review by OPeNDAP; TEMPO only (GOES-19 has no sidecars); built with VirtualiZarr's DMR++ parser into a local Icechunk store |

### Section 2: Server side

| Arm | Name in code | Access path | Status | Who tunes it | Needs |
|---|---|---|---|---|---|
| Superchunks / buffer chunks | `superchunks` | Small chunks grouped into fewer, larger reads | analysis | To decide | a client that issues the grouped reads; `run.py superchunks` gives the request and byte bounds meanwhile |
| S3 Express One Zone | `s3_express` | Same arm, data in a single-zone bucket | proposed | To decide | a directory bucket holding copies of the pinned granules |
| Caching service, materialized Zarr | `materialized_zarr` | Proposed in the CODA deck | later | — |  |

### Section 3: End to end (stretch)

| Arm | Name in code | Access path | Status | Who tunes it | Needs |
|---|---|---|---|---|---|
| Harmony request | `harmony` | Total time for Harmony requests from inside EC2 | proposed | To decide | stretch |
| AppEEARS | `appeears` |  | proposed | To decide | stretch |
| OPeNDAP | `opendap` |  | proposed | To decide | stretch |

### Datasets

| Dataset | Name in code | Why | Status | Needs |
|---|---|---|---|---|
| TEMPO NO₂ L3 V04 (grid, ASDC) | `tempo_no2` | Reference case; virtual store exists | built | Earthdata login; run live over HTTPS only, not yet from a host in us-west-2 |
| GOES-19 ABI L2 CMI band 13 (public, us-east-1) | `goes19` | Rerunnable by anyone without credentials | built |  |
| ICESat-2 ATL03 (photons, NSIDC) | `atl03` | Swath-like; h5coro's home ground | proposed | a point-cloud question shape; the harness assumes (time, y, x) grids |
| A paged cloud-optimized HDF5 file | `paged_hdf5` | Tests the page buffer and ROS3 cache | proposed | a pinned paged file |
| GPM_3IMERGHH_07 | `gpm_imerg` | So widely used | proposed | a dataset adapter |
| HLS | `hls` | Also very widely used | proposed | a dataset adapter (COG, tiled) |

### Workloads

| Request shape | Name in code | Cases | Scoping-doc use case |
|---|---|---|---|
| One time, whole map | `pancake` | `pancake:1`, `pancake:15` (observations scanned whole) | Visualization; in-region, few files |
| Time series at points | `churro` | `churro:10`, `churro:250` (points) | In-region, many files |
| Box statistics over time | `box` | `box:100` (km on a side) | In-region, many files; internal services |
| Every byte of the window | `batch` | `batch:0` | Internal services (Harmony, Giovanni) |

### Metrics

| Metric | What it measures | Row field | Recorded |
|---|---|---|---|
| Discovery (CMR query) | Finding the granules for the question | `discovery_seconds` | timed at `discover`, copied into every row; kept out of first byte |
| Authentication | Fetching temporary S3 credentials | `auth_seconds` | per trial; 0 with a reason on public data; null with a reason for the virtual arms, whose store fetches its own credentials inside manifest retrieval |
| Manifest retrieval | Loading the store's index, or opening the files | `manifest_seconds` | per trial |
| Chunk identification | Finding which chunks a selection needs | `chunk_id_seconds` | per trial where the arm exposes the step; null with a reason where it happens inside the first read |
| Byte retrieval | Reading and decoding the chunks | `byte_seconds` | per trial |
| Concurrency | Same arm at 1 and 8 workers | `arm_variant` | `session --variants` |
| First byte (total) | Sum of the four parts, for the first value of the answer | `first_byte_seconds` | `--measure first_byte` trials |
| End to end | Question to answer | `end_to_end_seconds` | `--measure full` trials |
| Last byte | All bytes in memory, for batch cases | `last_byte_seconds` | `--measure full` trials |
| Warm second query | The same read again in the same process | `warm_seconds` | per trial |
| Requests and bytes on the wire | Per run, plus the floor they imply | `request_count, bytes_requested, net_rx_bytes, floor_seconds` | per trial |
| Dollars per question | Requests, transfer and compute at list price | `cost_usd (requests_usd, transfer_usd, compute_usd)` | per trial |

Scope rows this version does not reach:

| Scope row | State here |
|---|---|
| In-region access (us-west-2) | The harness records `client_region` and `in_region` on every row and never mixes them in a cell. No in-region session has been run with it yet. |
| HTTPS through Earthdata Login and CloudFront | `--transport https` on every network subcommand (TEMPO): ASDC's distribution endpoint with an Earthdata bearer token, from anywhere. Probes, gate verdicts and report cells are filed per transport and never pooled with S3. First session below. |
| Software tuning | One factor at a time through `--settings` / `session --variants`; recorded in `arm_variant`. |
| CMR discovery | Timed when granules are pinned, copied into every row as context, never in first byte. GOES-19 is not in CMR, so its discovery is an S3 listing (`discovery_kind`). |
| Out-of-region clients | Contested in the scope. The first GOES-19 session below is out of region by necessity and is labelled so. |
| Swath, photon and tiled structures | Not supported: questions assume (time, y, x) grids, one observation per granule. |
| Weighting request shapes by real usage | Not done; needs the request logs. |

## Definitions

**A question** is asked in coordinates: seeded points and boxes around
36.98° N, 95.97° W (`questions.py`). Each dataset turns them into its own
grid indices (`datasets.py`), so two datasets answer the same question on
the ground.

**A trial** is one fresh process. It runs the arm's four steps and times
each (`trial.py`):

| Part | Step | Virtual store | File readers |
|---|---|---|---|
| Authentication | `authenticate` | temporary S3 credentials; none on public data | the same |
| Manifest retrieval | `open` | open the repository and the store's metadata | open the file(s) the arm reads first |
| Chunk identification | `locate` | not separable: Icechunk fetches the chunk manifest on the first chunk request | h5py: look up each needed chunk's byte range in the file's chunk index; not separable for the user stack or inside worker processes |
| Byte retrieval | `read` | fetch and decode | fetch and decode |

Where an arm cannot separate a step, the part is null, the row says why
(`chunk_id_reason`, `manifest_reason`) and the time is inside byte
retrieval.

**First byte** (`--measure first_byte`): the arm sets up for the whole
question, then reads only the first value of the answer.
`first_byte_seconds` is the sum of the four parts. What "sets up for the
whole question" means is the arm's own behaviour: the user stack opens every
granule before it can return anything; tuned h5py opens one file; a virtual
store opens once whatever the window.

**End to end and last byte** (`--measure full`): the whole question.
`last_byte_seconds` is when every value is in memory;
`end_to_end_seconds` adds the per-observation mean computed from them.

**Warm second query**: the same read again in the same process
(`warm_seconds`), reported beside the cold number.

**Process start and imports** are recorded as `startup_seconds` and are
outside every other number.

## Protocol, and where this version stands on it

| Protocol line | Here |
|---|---|
| Where: a dedicated EC2 host in us-west-2, plus the VEDA JupyterHub | Any host; `host_kind`, `instance_type`, `client_region`, `in_region` on every row. Run on EC2 for headlines. |
| Runs: fresh process per trial, medians of 10 cold runs, order shuffled across arms | `session`: one subprocess per trial, order shuffled within each repetition, `--reps 10` by default. |
| Pinning: versions, store snapshot and granule list, in every row | `git_sha`, `uv_lock_sha256`, `packages`, `snapshot_id`, `manifest_path`. Manifests are committed. |
| Correctness gate | `gate`: every arm against the first (tuned h5py) on the first, middle and last observation of each case, read together as one question so the gate runs the code a trial times (worker processes, a multi-file open). Equal fill masks, equal times, values within a relative 1e-6 (h5py decodes packed integers in float64, xarray in float32); `exact` and `close` are recorded apart. A variant is gated as an arm of its own (`gate --variants arm=JSON`) and never inherits its arm's verdict. A later gate adds to the window's file. `session` refuses ungated arms and variants. |
| Floor: round-trip time and bandwidth first | `probe`; every row then carries `floor_seconds`. |
| Fairness: authors review their arm; one factor at a time | Settings are listed under [Arm settings](#arm-settings). **No arm here has been reviewed by its library's authors yet.** |
| Reproducibility: public GOES-19 cases without credentials | Below. |

## Reproduce (no credentials)

```sh
uv sync
run="uv run python -m benchmarks.trade_study.run"
$run status
$run discover    --dataset goes19 --window 1h   # pins 6 granules (S3 listing) and the grid
$run build-store --dataset goes19 --window 1h   # virtual references → a local Icechunk store
$run probe       --dataset goes19               # round-trip time and bandwidth
$run gate        --dataset goes19 --window 1h   # must print GATE PASSED
$run session     --dataset goes19 --window 1h --run-id <id> --yes
$run report      results/trade_study/<id>
$run superchunks --dataset goes19 --window 1h
```

`report` writes `summary.json`, `summary.md` and `figures/` into the run's
directory: one figure per case, each arm's end-to-end time split into
setup and reading. Of these, only `summary.md` is committed, beside the
run's `results.jsonl`; a test fails when a committed `summary.md` is not
what `report` writes today.

`--window 1d` pins 144 granules; `1w` and `1m` are defined. One trial:
`$run trial --dataset goes19 --arm tuned_h5py --case churro:10:1h --measure first_byte`.

TEMPO NO₂ needs an Earthdata login (`EARTHDATA_TOKEN`, netrc, or
earthaccess's prompt). On a host in us-west-2:
`$run discover --dataset tempo_no2 --window 1d`, then `probe`, `gate`,
`session` with `--dataset tempo_no2 --window 1d`. From anywhere else, add
`--transport https` to each. The HDF5-built virtual store is the public
mirror; the DMR++ one is built locally, per transport:

```sh
$run build-store --dataset tempo_no2 --window 1d --source dmrpp --transport https
$run gate --dataset tempo_no2 --window 1d --transport https --arms tuned_h5py,virtual_icechunk,virtual_dmrpp
```

TEMPO granules are about 0.7 GB. `session --measures first_byte` and a
short `--arms` list keep an out-of-region run from pulling many gigabytes
through the distribution endpoint.

## Arm settings

| Arm | Defaults | Source | Tuning factors (`--settings`, `session --variants`) |
|---|---|---|---|
| `user_stack` | Whatever the libraries default to; fsspec's read-ahead cache fetches a block per HDF5 metadata read | The earthaccess documentation | none: it is deliberately untuned |
| `tuned_h5py` | 8 MiB block reader; one process per granule, 8 at a time; 16 MiB page buffer on paged files only | The NSIDC cloud-optimized HDF5 study (Lopez, Barrett, Jelenak, Lee, 2025) | `block_size`, `max_workers`, `page_buf_size` |
| `download` | One HEAD, then 16 MiB ranges with 16 in flight; granules one at a time | — | `part_bytes`, `concurrency` |
| `virtual_icechunk`, `virtual_dmrpp` | zarr-python's and Icechunk's defaults | — | `zarr_concurrency` (outstanding chunk requests, default 10), `icechunk_concurrency`, `icechunk_decodes`, `zarrs_pipeline` (decode in Rust, in parallel) |

A variant changes one factor at a time and is recorded in `arm_variant`;
`arm_settings` holds every setting in force.

## Statistics

Per cell: n, median, min and max. Speedups are paired by session and
repetition: a trial of one arm is compared with the other arm's trial from
the same session and repetition, so both saw the same network and host. A
trial counts once: if a file holds the same trial of a session twice, the
later row is used and the summary says how many were set aside. The 95%
interval is a percentile bootstrap of the median speedup, the number it is
printed beside. When a run has several sessions, the spread between the
sessions' median speedups is reported, and a speedup inside that spread is
marked as not a detectable difference; with one session there is no spread,
so `detectable` is null and the report says so. Every arm is compared with
`virtual_icechunk`, and every variant with its own arm's defaults.

`gate` and `session` work on one window: `--window`, or the window their
`--case` values name. They refuse cases from two windows, or a `--window`
that disagrees with the cases.

## What came from the first benchmark

The first benchmark (`benchmarks/tempo_no2`, in this repository's history
until the two were merged) read TEMPO NO₂ only. Its access code, request
counters, floor model, gate and statistics are now this package's. Three
things changed names on the way:

| First benchmark | Here |
|---|---|
| arms `netcdf_user_stack`, `netcdf_tuned`, `netcdf_download` | `user_stack`, `tuned_h5py`, `download` |
| workloads `scan`, `point`, `aoi` | `pancake`, `churro`, `box` |
| phases `open`, `retrieval` | `manifest_seconds`, `byte_seconds` (and authentication and chunk identification timed apart) |

Not carried over: the per-second host sampler and the "likely limiter"
label; the pre-run estimate of hours and dollars; the `noop` and
`virtual_manifeststore` checker arms; warm-process trials (every trial here
times a warm second read instead).

`report` still reads a `results.jsonl` the first benchmark wrote and
upgrades its rows on load (`legacy.py`), because the only in-region
measurements so far, the run the CODA talk quotes, are in that format.
Delete `legacy.py` once that run has been repeated with this harness.

## First run: GOES-19 from a workstation (a method check, not a headline)

`results/trade_study/goes19-1h-2026-11-12-workstation/`: 410 trials, 0
failed, gate passed for all four arms (download and Icechunk bit-exact with
tuned h5py; the user stack within 7e-8, relative). One hour of band 13, 6
granules of 24 MB. Full tables: `summary.md` in that directory.

**Read this before the numbers.** The client was a home workstation, out of
region (22 ms round trip to us-east-1, about 23 MB/s), not a dedicated
host; the offline tests ran once during the session. The data are in
us-east-1, not the brief's us-west-2. The virtual store's index was on
local disk. No arm's settings were reviewed by its authors. The rows record
the parent commit's `git_sha` because the harness was not yet committed,
and a `uv.lock` hash that differs from the committed file only in uv's
`exclude-newer` options (same package versions). This run shows the
harness works end to end; it ranks nothing for the trade study.

Medians of 10 cold runs, seconds, for the 100 km box over the hour:

| Arm | Manifest | Chunk id | Bytes | **First byte** | End to end | Warm second query | MB on wire (whole question) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `virtual_icechunk` | 0.41 | — | 0.09 | **0.51** | 0.58 | 0.14 | 2.5 |
| `tuned_h5py` | 0.54 | 0.00 | 0.30 | **0.85** | 5.67 | 3.90 | 109.6 |
| `download` | 0.00 | — | 1.25 | **1.26** | 6.53 | 6.20 | 153.7 |
| `user_stack` | 7.30 | — | 0.06 | **7.35** | 7.50 | 0.07 | 153.1 |

What the parts show that one end-to-end number does not:

- The user stack's first byte is all manifest retrieval: it opens (and, with
  fsspec's default read-ahead on 24 MB files, effectively downloads) all six
  granules before the first value. For one granule its first byte is 1.9 s.
- Tuned h5py's chunk identification is free here: the first 8 MiB block
  already holds the file's chunk index. The same block size means it moves
  16 MB to return one value from a 24 MB file; the setting comes from a
  study of much larger files.
- On this link, 8 worker processes were no faster than 1 (5.67 s against
  5.06 s): the arm is limited by bandwidth, not round trips. In region this
  should differ, which is why the concurrency pair is in the plan.
- For the whole map of one observation the file readers win (1.65 s tuned,
  1.69 s download, 2.19 s Icechunk), as the first TEMPO benchmark found.
- For every chunk of the hour (batch), all three non-default arms land
  within 7% of each other at about 10 s, about 1.5 times the floor.

Loose ends in this run:

- Icechunk's first value at a point takes 0.46 s of byte retrieval against
  0.09 s for the first value of a box, though each needs one chunk.
  Unexplained; the first thing to ask the Icechunk maintainers.
- The floor is not a true lower bound on this link: one cell came in at 0.9
  times its floor because bandwidth moved between the probe and the trial.
- One user-stack trial of 250 points took 39.5 s against a 7.8 s median.

## Second run: TEMPO NO₂ over HTTPS, with the DMR++ arm (a shakedown)

`results/trade_study/tempo-https-1d-2026-11-13-workstation/`: 32 trials, 0
failed. One event day, 15 granules, read through ASDC's distribution
endpoint with a bearer token from the same workstation. Gate: the mirror
store and the DMR++-built store are both bit-exact with tuned h5py on the
10-point and 100 km box cases (the only ones gated and run, to limit
egress). n = 3 per cell, n = 1 for tuned h5py's whole question; this is not
the protocol's 10.

| 100 km box, 15 granules | Auth | Manifest | Chunk id | Bytes | **First byte** | End to end | n |
|---|---:|---:|---:|---:|---:|---:|---:|
| `virtual_dmrpp` (index on local disk) | 0.00 | 0.48 | — | 3.38 | **3.79** | 9.78 | 3 |
| `virtual_icechunk` (index on the mirror, us-west-2) | 0.09 | 3.76 | — | 3.97 | **7.62** | 13.00 | 3 |
| `tuned_h5py` | 0.05 | 2.88 | 7.11 | 4.33 | **14.29** | 47.14 | 3, then 1 |

What it shows:

- **The DMR++ route works.** VirtualiZarr's DMR++ parser turned the 15
  sidecars (about 108 kB each) into a store that returns the same values as
  the HDF5-built mirror. Building it took 172 s over HTTPS, nearly all of it
  loading each granule's coordinate arrays.
- **The two virtual arms differ here by where the index lives, not by what
  built it.** The DMR++ store is a local directory; the mirror is in S3,
  out of region for this client. Their byte retrieval is the same within
  noise. A fair comparison needs both indexes in the same place.
- **Chunk identification is a real cost for h5py on TEMPO over HTTPS**: 7 s
  to look up two chunks' addresses, against nothing on GOES-19. Each lookup
  that misses the block cache is a request, and a request through the
  distribution endpoint took 0.7–0.9 s here (median HEAD).
- **The floor model does not hold on this transport**: several cells finish
  below their floor. The probe's round-trip time is a cold HEAD through the
  Earthdata redirect, which later requests on a warm connection do not pay.
  The HTTPS floor needs a warm-connection ranged read as its unit.

## Superchunks: what grouping can and cannot buy

`run.py superchunks` reads one granule's chunk index, takes the chunks each
question needs, and merges neighbours on disk (`superchunks.py`). It times
nothing. For GOES-19 (`results/trade_study/superchunks-goes19-1h.json`):

| Case | Granules | One request per chunk | Grouped, no gap allowed | Grouped, gap ≤ 64 KiB | Over-read |
|---|---:|---:|---:|---:|---:|
| Whole map, one observation | 1 | 226 | 4 | 1 | 0% |
| 10 points over the hour | 6 | 36 | 6 | 6 | 0% |
| 250 points over the hour | 6 | 54 | 6 | 6 | 0% |
| 100 km box over the hour | 6 | 18 | 6 | 6 | 0% |
| Every chunk of the hour | 6 | 1,356 | 24 | 6 | 0% |

GOES-19 is close to the best case: its chunks are full-width strips of 24
rows, written in order, so any regional question needs a contiguous run of
bytes. The limits, which hold for any layout:

- **One read never spans two files.** Grouping stops at one request per
  granule, so a time series over N files needs at least N requests. For the
  churro and box cases that floor is reached with no gap allowance at all.
- **Requests fall, bytes do not.** Every grouped read here carries the same
  bytes; the projected floor for the whole map falls from 1.67 s to 1.07 s
  at this link's round-trip time, and the rest is transfer.
- **Decompression is unchanged.** Each chunk is still inflated on its own.
- **First byte gets later.** The first value arrives when the first grouped
  read lands; a whole-file group is the download arm.
- **Memory.** A group is held whole; the cap here is 64 MiB (`--max-mib`).
- **Not yet shown:** a layout where needed chunks are scattered (TEMPO's
  two-dimensional chunks, or files rewritten out of order), where grouping
  must over-read to cut requests. The same command gives that table for
  TEMPO once it is run in us-west-2.

## What is not built, and what each needs

- **h5coro (Python)** and **SlideRule `h5p`**: not attempted; each should be
  written with its authors, who have not been asked yet, and h5coro is not a
  dependency.
- **HDF5 2.2.0 ROS3 block cache**: the block cache arrived in HDF5 2.2.0
  (released 2026-07-29). The h5py wheel this repository locks (3.16.0)
  bundles HDF5 2.0.0 and registers no `ros3` driver, so it cannot be tried
  from PyPI. conda-forge publishes h5py 3.16.0 built against HDF5 ≥ 2.2.0
  (linux-64, Python 3.12); the arm needs that environment, best created on
  the EC2 host. Whether h5py exposes the cache's settings is still untested.
- **Virtual store from DMR++ in a shared location**: the first cut writes
  to local disk. It has not been reviewed by OPeNDAP, and GOES-19 has no
  sidecars.
- **S3 Express One Zone**, **caching service**, **Harmony / AppEEARS /
  OPeNDAP**: registered in `scope.py`, nothing more.
- **A real superchunk reader**: `superchunks` is arithmetic on layouts. A
  timed arm needs a client that issues the grouped reads.

## Known limits of this version

- The GOES-19 virtual store and the TEMPO DMR++ store are on local disk
  (`index_location` per row); for GOES-19 that is because no public store
  exists and there is no credential-free bucket to write one to. Its
  manifest retrieval therefore has no network in it; rows say
  `index_location: local-disk`.
- Request counts mean different things per arm (`counters.py`):
  byte ranges asked for (tuned h5py, download), completed S3 calls (user
  stack), and a static minimum from the chunk grid (Icechunk). Icechunk can
  split one chunk into several ranged reads, so its real count is higher.
- `net_rx_bytes` is the host's receive counter; on a shared host it includes
  other traffic.
- A batch reads every chunk of the benchmark variables for every granule in
  the window, not every byte of every file (only the download arm does
  that).
- No host sampler or limiter labels: the first benchmark sampled per-core CPU
  once a second and labelled each cell's likely limiter; that was not carried
  over.
