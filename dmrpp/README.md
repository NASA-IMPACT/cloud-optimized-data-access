# DMR++ sidecars against the files they describe

A DMR++ sidecar records a byte offset and length for every chunk of one
version of a file. If the file is replaced and the sidecar is not rebuilt,
everything that reads through the sidecar (Hyrax, a virtual store built from
it) reads the wrong bytes. This study asks how often that has happened in
NASA's cloud archives, and whether a sidecar that looks out of date is
actually wrong.

Staleness is where it started, not all it checks: the verifier also compares
how a sidecar describes each array (shape, data type, filters) with the file,
and can compare the values Hyrax serves with a direct read.

Results are in `results/dmrpp/`; start with `survey/summary.md`.

## The pipeline

| Step | Command | What it does | Where it runs |
|---|---|---|---|
| 1. Survey | `python -m dmrpp.survey run`, then `report` and `sample` | Samples 20 granules from every cloud-hosted HDF-family collection in the inventory (`benchmarks/cost/data/`) and compares each file's Last-Modified with its sidecar's. Finds collections where stale sidecars exist; it cannot say how many. `sample` then lists two granules per collection that has sidecars, flagged or not, for step 3. | Anywhere, with `EARTHDATA_TOKEN`; `report` and `sample` need no network |
| 2. List | `python -m dmrpp.list_collection` | Lists a collection's whole S3 prefix and pairs every file with its sidecar: the complete list of stale files for that collection. Takes the survey's `collections.csv`. | us-west-2, with an Earthdata login |
| 3. Verify | `python -m dmrpp.verify` | For a file from either step, reads the chunk index a second way, from the file itself, and compares it with the sidecar chunk by chunk. With `--values`, also compares what Hyrax serves with a direct read. | us-west-2 for `s3`; anywhere with `--transport https`, slower |

A timestamp says a file was written after its sidecar. Only step 3 says
whether the sidecar is wrong: a bulk copy rewrites Last-Modified without
changing a byte.

## Running it

The dependencies are this repository's `dmrpp` extra:

```bash
uv run --extra dmrpp python -m dmrpp.survey run --inventory benchmarks/cost/data/cmr_cloud_inventory.csv
uv run python -m dmrpp.survey report        # collections.csv, stale_granules.csv, summary.md
uv run python -m dmrpp.survey sample        # sampled_with_sidecars.csv
uv run --extra dmrpp python -m dmrpp.list_collection --collections results/dmrpp/survey/collections.csv --only-flagged --summary summary.csv --out stale.csv
uv run --extra dmrpp python -m dmrpp.verify stale.csv --min-gap-days 1 --values --out results/dmrpp/verify/stale.csv
uv run --extra dmrpp python -m dmrpp.verify results/dmrpp/survey/sampled_with_sidecars.csv --provider POCLOUD --out results/dmrpp/verify/podaac.csv
```

Each command's `--help` has its options, and for the verifier the meaning
of every verdict. `report` and `sample` need no extra and no network: they
rebuild their files from the committed `granules.jsonl.gz`, and a test
fails when a committed file is not what they write today.

`kerchunk` is pinned in the extra because `verify/hdf4.py` corrects that
version's HDF4 decoder; move the pin and the corrections together.

## Results

| Path | What |
|---|---|
| `results/dmrpp/survey/` | The survey of 2026-11-13: `granules.jsonl.gz` (one row per sampled granule), and the three files `report` and the one file `sample` derive from it. It stopped at 3,941 of 5,617 collections; `run` resumes where it left off. |
| `results/dmrpp/verify/` | `verdicts.csv`: one row per granule with its newest verdict, and the run it came from. `python -m dmrpp.verify.merge` folds a new run in. Its README says which rows are findings and which are the verifier's limits. |

## Map of the code

| File | What |
|---|---|
| `common.py` | What the three steps share: which names are data files, the gap buckets, and temporary S3 credentials that are renewed before their hour is up. |
| `survey.py` | Step 1. |
| `list_collection.py` | Step 2. |
| `verify/cli.py` | Step 3's command line, the verdicts, and which granules to pick from a CSV. |
| `verify/merge.py` | One verdict per granule from several runs. |
| `verify/granule.py` | One granule: index the file, read the sidecar, compare. |
| `verify/compare.py` | The comparison itself: chunk indexes, array metadata, and matching names the two sides spell differently. |
| `verify/sidecar.py` | What the sidecar's XML says that VirtualiZarr's DMR++ parser does not pass on (byte order, chunks kept outside the file). |
| `verify/hdf4.py` | Corrections to kerchunk's HDF4 decoder. |
| `verify/values.py` | The Hyrax value check. |
| `verify/access.py` | CMR lookups, object stores and credentials. |

Tests are in `tests/dmrpp/` and need neither the network nor the extra.
