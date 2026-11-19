# Verifier verdicts

`verdicts.csv` has one row per granule: the newest verdict from every run of
`python -m dmrpp.verify` so far. Its last column, `run`, names the run a row
came from (a first pass per provider, `_redo` passes after fixes to the
verifier, and `values_` runs that also asked Hyrax for values). The other
columns and the meaning of each verdict are in `python -m dmrpp.verify --help`.

To add a run, write it to a file of its own and fold it in, oldest first:

```sh
uv run --extra dmrpp python -m dmrpp.verify results/dmrpp/survey/sampled_with_sidecars.csv \
    --provider LPCLOUD --redo results/dmrpp/verify/verdicts.csv --out results/dmrpp/verify/lpcloud_redo.csv
uv run python -m dmrpp.verify.merge results/dmrpp/verify/verdicts.csv \
    results/dmrpp/verify/verdicts.csv results/dmrpp/verify/lpcloud_redo.csv
```

`--redo` picks the granules that are not `match` or `skipped`, so it also
repeats the ones whose verdict is a real finding.

## Reading it

Not every row is a finding about a sidecar. As of 2026-11-16:

| Rows | What they are |
|---|---|
| `METADATA_MISMATCH` with `int8_declared_int16` above 0 (53, all PO.DAAC) | A finding: the sidecar declares signed 8-bit variables as `Int16`. Reported as OPENDAP/hyrax#17. |
| `MISMATCH`, `TELLUS_GLDAS-NOAH-3.3_TWS-ANOMALY_MONTHLY` | A finding: the file was replaced and the sidecar was not rebuilt. |
| `METADATA_MISMATCH`, `OMCLDO2` and `OMDOAO3` (2) | A finding: the sidecar declares the `nTimes` dimension as 1 where the data has 1,644 rows. |
| `METADATA_MISMATCH` listing `deflate` twice (11) | The sidecar lists the filter twice where the file has it once. Not checked for an effect on values. |
| `METADATA_MISMATCH` naming `fletcher32` (7, S5P_L2__CO_____HiR, S5P_L2__NO2____HiR, S5P_L2__NP_BD3_HiR, S5P_L2__NP_BD6_HiR) | **Not a finding.** An earlier version of the verifier missed the checksum filter the sidecar lists. These rows were not repeated after the fix; the ones that were all became `match`. |
| `METADATA_MISMATCH`, `ATom_merge_*` (2) | **Not a finding.** A variable written along an unlimited dimension is shorter than the dimension; the sidecar reports what a netCDF reader sees. |
| `MISMATCH`, `MOD17*` and `MYD17*` (8), and `METADATA_MISMATCH`, `AIRS2SUP` (4) | **Not a finding as far as is known.** HDF4 files, where the file side is read through a decoder with known gaps. |
| `error ...` | Files or sidecars the verifier could not read: parser limits, refused buckets, and LP DAAC and GES DISC rows from the first run that were not repeated. They say nothing about the sidecar. |

GES DISC stops at 990 of 1,893 sampled granules, and half of LP DAAC's
collections have only a first-run error. The runs were ended there on
purpose: the remaining sidecars are of the kind that matched everywhere else.
