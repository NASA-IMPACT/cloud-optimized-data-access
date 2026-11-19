# cloud-optimized-data-access

Supporting materials for the Cloud Optimized Data Access Trade Study, from
the ODSI team: a benchmark of ways to read the same data, a study of whether
DMR++ sidecars still describe their files, and decks for the working group.

## What is here

| Path | What |
|---|---|
| `benchmarks/trade_study/` | The benchmark harness: any dataset and arm, the same questions, time to first byte in parts, a correctness gate, paired speedups and figures. Its README has the status of every arm, the protocol, and a map of the code. |
| `results/trade_study/` | Committed runs, probes, gate verdicts and superchunk tables. |
| `decks/coda-presentation/` | The CODA talk of 3 November 2026, "One file is easy" (Slidev). |
| `dmrpp/`, `results/dmrpp/` | The DMR++ study: which sidecars are older than their data files, and whether they still describe them. Its README has the pipeline. Needs the `dmrpp` extra. |
| `benchmarks/cost/data/`, `scripts/cmr_cloud_inventory.py` | An inventory of every cloud-hosted CMR collection, and the script that rebuilds it from CMR metadata. The DMR++ survey samples from it. |
| `tests/` | Offline tests for all of the above. |

## Try it

The GOES-19 dataset is public, so this needs no credentials and runs from
anywhere:

```sh
uv sync
run="uv run python -m benchmarks.trade_study.run"
$run status                                      # what is built, what is only proposed
$run discover    --dataset goes19 --window 1h    # pin the granules
$run build-store --dataset goes19 --window 1h    # a local virtual store over them
$run probe       --dataset goes19                # the link's round-trip time and bandwidth
$run gate        --dataset goes19 --window 1h    # every arm must return the same values
$run session     --dataset goes19 --window 1h --run-id <id> --yes
$run report      results/trade_study/<id>        # summary.md (the committed copy), summary.json, figures/
```

TEMPO NO₂ needs an Earthdata login and either a machine in us-west-2 or
`--transport https`; see `benchmarks/trade_study/README.md`. Credentials come
through earthaccess's normal strategies (environment, netrc, or prompt);
nothing is pasted anywhere. Headline timing runs on one dedicated EC2
instance.

## The DMR++ study

A DMR++ sidecar records where every chunk of one version of a file sits. If
the file is replaced and the sidecar is not rebuilt, whatever reads through
the sidecar reads the wrong bytes. `dmrpp/` asks how often that has happened
in NASA's cloud archives, in three steps: a sampled survey of every
collection, a complete listing of one collection, and a verifier that
compares a sidecar with the file's own index chunk by chunk.

The survey's report can be rebuilt offline from the committed data:

```sh
uv run python -m dmrpp.survey report
```

The other steps need the `dmrpp` extra and an Earthdata login, and the
listing needs a machine in us-west-2; see `dmrpp/README.md`.

## Develop

```sh
uv run pytest               # offline; about 20 seconds
uvx ruff@0.17.5 check       # lint
uvx ruff@0.17.5 format      # format
cd decks/coda-presentation && npm ci && npm run build
```

CI runs the same four (`.github/workflows/ci.yml`); the ruff version is
pinned there, so use the same one locally.
