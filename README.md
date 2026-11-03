# cloud-optimized-data-access

Supporting materials for the Cloud Optimized Data Access Trade Study: the
ODSI team's benchmarks and decks for the working
group.

## What is here

| Path | What |
|---|---|
| `benchmarks/tempo_no2/` | TEMPO NO₂ event access: file-based netCDF vs the same bytes through a virtual Zarr (Icechunk) store. Protocol in its README. |
| `results/tempo_no2/` | Committed results, one directory per run, with provenance. `latest.json` names the run the deck shows. |
| `decks/coda-presentation/` | Slidev deck skeleton (placeholder title). Reads its numbers from `results/`. |

## Reproduce

Anyone with an Earthdata login on a machine in us-west-2 (the VEDA
JupyterHub, CodeBuild, or an EC2 instance). Credentials come through
earthaccess's normal strategies (environment, netrc, or prompt); the store
mirror is anonymous; nothing is pasted anywhere.

```sh
uv sync
uv run python -m benchmarks.tempo_no2.run discover --window 1d   # manifest + coordinates; both committed
uv run python -m benchmarks.tempo_no2.run probe                  # RTT, bandwidth, HDF5 paging → results/tempo_no2/probes.json
uv run python -m benchmarks.tempo_no2.run gate                   # exact-equality gate; must print GATE PASSED
uv run python -m benchmarks.tempo_no2.run smoke                  # CodeBuild or Hub; < 2 min; records/asserts request counts
uv run python -m benchmarks.tempo_no2.orchestrate --run-id <id> --headline-only --estimate   # trials, requests, hours, dollars
uv run python -m benchmarks.tempo_no2.orchestrate --run-id <id> --session 1 --headline-only --yes   # EC2; reads probes.json
uv run python -m benchmarks.tempo_no2.analyze results/tempo_no2/<id>
cd decks/coda-presentation && npm install && npm run build
```

From a laptop, add `--access https`: the virtual chunks are read from
NASA's distribution endpoint with your Earthdata token, and rows are
labelled so they never mix with in-region numbers.

Headline timing runs on one dedicated EC2 instance. After `analyze`,
commit `results/tempo_no2/`, the manifests, `expected/requests.json`, and
the deck's `public/results` and `public/figures`.

The protocol, including assumptions and what would change the conclusion,
is `benchmarks/tempo_no2/README.md`.
