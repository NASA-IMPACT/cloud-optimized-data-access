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
| `docs/superpowers/` | Design and implementation plan. |

## Reproduce

Filled in by the benchmark's protocol (see `benchmarks/tempo_no2/README.md`).
