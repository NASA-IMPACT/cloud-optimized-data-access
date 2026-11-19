# Trade-study run `tempo-https-1d-2026-11-13-workstation`

32 trials, 0 failed. Medians of cold runs, one fresh process each.

Transport: https. Client: **out of region** (client region: unknown, not AWS; host: other). Virtual-store index read from: s3 (public mirror, us-west-2). Discovery (cmr): 0.85 s, measured when the granules were pinned; not in first byte.

## tempo_no2: time to first byte, in parts (seconds)

| Case | Granules | Arm | n | Auth | Manifest | Chunk id | Bytes | **First byte** | min–max | Warm | Requests | MB on wire |
|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| `box:100:1d` | 15 | `virtual_dmrpp` | 3 | 0.00 | 0.48 | — | 3.38 | **3.79** | 3.71–4.76 | 3.193 | ≥ 23 | 10.2 |
| `box:100:1d` | 15 | `virtual_icechunk` | 3 | 0.09 | 3.76 | — | 3.97 | **7.62** | 7.50–8.37 | 3.491 | ≥ 10 | 11.1 |
| `box:100:1d` | 15 | `tuned_h5py` | 3 | 0.05 | 2.88 | 7.11 | 4.33 | **14.29** | 14.27–14.99 | 0.137 | 9 | 70.4 |
| `churro:10:1d` | 15 | `virtual_dmrpp` | 3 | 0.00 | 0.46 | — | 4.56 | **5.02** | 4.81–5.31 | 3.365 | ≥ 23 | 10.2 |
| `churro:10:1d` | 15 | `virtual_icechunk` | 3 | 0.07 | 3.44 | — | 4.41 | **7.92** | 7.87–8.64 | 3.403 | ≥ 10 | 11.1 |
| `churro:10:1d` | 15 | `tuned_h5py` | 3 | 0.07 | 2.95 | 7.95 | 4.34 | **16.01** | 15.21–16.15 | 0.137 | 9 | 70.4 |

## tempo_no2: the whole question (seconds)

| Case | Granules | Arm | n | **End to end** | min–max | Last byte | Warm | Floor | × floor | Requests | MB on wire | Peak RSS MB | $ per question |
|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `box:100:1d` | 15 | `virtual_dmrpp` | 3 | **9.78** | 9.43–16.11 | 9.78 | 9.26 | 13.49 | 0.7 | ≥ 51 | 132.8 | 338.0 | 0.012492 |
| `box:100:1d` | 15 | `virtual_icechunk` | 3 | **13.00** | 12.28–13.73 | 13.00 | 8.61 | 13.47 | 1.0 | ≥ 38 | 132.5 | 301.9 | 0.012632 |
| `box:100:1d` | 15 | `tuned_h5py` | 1 | **47.14** | 47.14–47.14 | 47.14 | 41.94 | 76.15 | 0.6 | 134 | 1,038.5 | 133.8 | 0.096029 |
| `churro:10:1d` | 15 | `virtual_dmrpp` | 3 | **10.46** | 10.11–10.59 | 10.46 | 9.07 | 13.49 | 0.8 | ≥ 51 | 132.8 | 373.7 | 0.012532 |
| `churro:10:1d` | 15 | `virtual_icechunk` | 3 | **13.29** | 13.17–14.46 | 13.29 | 8.32 | 13.47 | 1.0 | ≥ 38 | 132.5 | 322.4 | 0.012648 |
| `churro:10:1d` | 15 | `tuned_h5py` | 1 | **46.01** | 46.01–46.01 | 46.01 | 42.12 | 76.15 | 0.6 | 134 | 1,038.3 | 134.2 | 0.095956 |

## tempo_no2: paired speedups

| Case | Measure | From | To | **Speedup** | 95% interval | Pairs |  |
|---|---|---|---|---:|---|---:|---|
| `box:100:1d` | first_byte | `tuned_h5py` | `virtual_icechunk` | **1.88×** | 1.79–1.90 | 3 |  |
| `box:100:1d` | first_byte | `virtual_dmrpp` | `virtual_icechunk` | **0.49×** | 0.45–0.63 | 3 |  |
| `churro:10:1d` | first_byte | `tuned_h5py` | `virtual_icechunk` | **1.92×** | 1.87–2.03 | 3 |  |
| `churro:10:1d` | first_byte | `virtual_dmrpp` | `virtual_icechunk` | **0.61×** | 0.61–0.64 | 3 |  |
| `box:100:1d` | full | `tuned_h5py` | `virtual_icechunk` | **3.63×** | — | 1 |  |
| `box:100:1d` | full | `virtual_dmrpp` | `virtual_icechunk` | **0.77×** | 0.71–1.24 | 3 |  |
| `churro:10:1d` | full | `tuned_h5py` | `virtual_icechunk` | **3.18×** | — | 1 |  |
| `churro:10:1d` | full | `virtual_dmrpp` | `virtual_icechunk` | **0.79×** | 0.70–0.80 | 3 |  |

Speedups measured in one session have no spread between sessions to be judged against: whether they are a detectable difference is not known until a second session is run.

## Reading the tables

- A dash under a part means the arm does not expose that step; its time is inside Bytes:
  - `tuned_h5py`: inside the read: each worker process looks up its own granule's chunks.
  - `virtual_dmrpp`: inside the read: Icechunk fetches the chunk manifest on the first chunk request.
  - `virtual_icechunk`: inside the read: Icechunk fetches the chunk manifest on the first chunk request.
- Requests marked ≥ are a static minimum from the chunk grid, not a count. Other counts are byte ranges the client asked for.
- MB on wire is the host's receive counter over the trial, so it includes other traffic on a shared host.
- Floor = dependent round trips × median round-trip time + bytes ÷ measured bandwidth, from the session's probe.
- $ per question = S3 requests + transfer + compute at list price. Transfer is zero in region and internet egress otherwise.
- A speedup is the median, over trials paired by session and repetition, of one arm's seconds over another's. Its interval is a bootstrap of that median.
- Arms marked † are not gated: their values were not checked against the reference arm.
