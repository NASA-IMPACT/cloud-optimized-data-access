# The cloud archive inventory

`cmr_cloud_inventory.csv` has one row per cloud-hosted CMR collection (9,577,
from CMR on 2026-11-05): provider, format family, processing level, granule
count and an estimated size. It is the input to the DMR++ survey
(`python -m dmrpp.survey run --inventory benchmarks/cost/data/cmr_cloud_inventory.csv`),
which samples the 5,617 rows that are HDF-family collections with granules.

`scripts/cmr_cloud_inventory.py` rebuilds it from CMR's metadata catalog
alone: no data-server traffic and no login, one worker, rate limited. Its
docstring lists the stages. One of them reads the other file here:

| File | What | Source |
|---|---|---|
| `ems_fy2026.json` | Per-provider archive volume, growth and distribution, and the ten most-distributed products. The `resample` stage uses the product list to sample those collections more thoroughly. | EOSDIS FY2026 Annual Metrics Report (EMS), earthdata.nasa.gov/about/data-metrics/annual-metrics-reports |

Known gaps:

- The script uses CMR's normal search, which leaves out deprecated
  collections, so the inventory does too.
- The `grade` and `grade_blocker` columns are empty. The `merge` stage fills
  them from a `virtualizability_scores.csv` placed in this directory (grades
  published by alex-s-gardner/EarthDataVirtualizabilityScore), and that file
  is not kept here. Fourteen rows whose `size_source` is
  `virtualizability-score` took their granule size from it when this
  inventory was built; a rebuild without the file leaves those rows unsized.
