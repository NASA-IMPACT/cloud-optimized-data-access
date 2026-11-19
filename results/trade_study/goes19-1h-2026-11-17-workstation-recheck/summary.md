# Trade-study run `goes19-1h-2026-11-17-workstation-recheck`

145 trials, 1 failed. Medians of cold runs, one fresh process each.

Transport: s3. Client: **out of region** (client region: unknown, not AWS; host: other). Virtual-store index read from: local-disk. Discovery (s3-list): 0.15 s, measured when the granules were pinned; not in first byte.

## goes19: time to first byte, in parts (seconds)

| Case | Granules | Arm | n | Auth | Manifest | Chunk id | Bytes | **First byte** | min–max | Warm | Requests | MB on wire |
|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|
| `batch:0:1h` | 6 | `virtual_icechunk` | 3 | 0.00 | 0.41 | — | 0.06 | **0.47** | 0.46–0.48 | 0.052 | ≥ 8 | 0.1 |
| `batch:0:1h` | 6 | `tuned_h5py` | 3 | 0.00 | 0.55 | 0.00 | 0.30 | **0.86** | 0.84–0.87 | 0.002 | 3 | 16.8 |
| `batch:0:1h` | 6 | `download` | 3 | 0.01 | 0.00 | — | 1.09 | **1.10** | 1.08–1.38 | 0.901 | 3 | 25.4 |
| `batch:0:1h` | 6 | `user_stack` | 3 | 0.00 | 7.14 | — | 0.02 | **7.15** | 7.14–7.72 | 0.017 | 12 | 153.7 |
| `box:100:1h` | 6 | `virtual_icechunk` | 3 | 0.00 | 0.40 | — | 0.09 | **0.49** | 0.48–0.50 | 0.048 | ≥ 8 | 0.2 |
| `box:100:1h` | 6 | `tuned_h5py` | 3 | 0.00 | 0.56 | 0.00 | 0.32 | **0.88** | 0.78–1.01 | 0.003 | 3 | 16.8 |
| `box:100:1h` | 6 | `download` | 3 | 0.01 | 0.00 | — | 1.20 | **1.21** | 1.15–2.03 | 1.271 | 3 | 25.5 |
| `box:100:1h` | 6 | `user_stack` | 3 | 0.00 | 6.92 | — | 0.12 | **6.93** | 6.88–7.45 | 0.046 | 12 | 153.7 |
| `churro:10:1h` | 6 | `tuned_h5py` | 3 | 0.00 | 0.49 | 0.00 | 0.29 | **0.82** | 0.77–0.82 | 0.003 | 3 | 16.6 |
| `churro:10:1h` | 6 | `virtual_icechunk` | 3 | 0.00 | 0.40 | — | 0.45 | **0.86** | 0.85–0.87 | 0.055 | ≥ 8 | 0.2 |
| `churro:10:1h` | 6 | `download` | 3 | 0.01 | 0.00 | — | 1.11 | **1.12** | 1.10–1.20 | 0.884 | 3 | 25.5 |
| `churro:10:1h` | 6 | `user_stack` | 3 | 0.00 | 7.40 | — | 0.05 | **7.44** | 7.17–7.64 | 0.094 | 12 | 153.7 |
| `churro:250:1h` | 6 | `tuned_h5py` | 3 | 0.00 | 0.54 | 0.00 | 0.29 | **0.82** | 0.80–0.89 | 0.003 | 3 | 16.8 |
| `churro:250:1h` | 6 | `virtual_icechunk` | 3 | 0.00 | 0.39 | — | 0.45 | **0.84** | 0.83–0.87 | 0.054 | ≥ 8 | 0.2 |
| `churro:250:1h` | 6 | `download` | 3 | 0.01 | 0.00 | — | 1.23 | **1.24** | 1.21–1.33 | 1.081 | 3 | 25.5 |
| `churro:250:1h` | 6 | `user_stack` | 3 | 0.00 | 7.25 | — | 0.12 | **7.38** | 7.08–7.50 | 0.047 | 12 | 152.4 |
| `pancake:1:1h` | 1 | `virtual_icechunk` | 3 | 0.00 | 0.39 | — | 0.05 | **0.45** | 0.44–0.46 | 0.050 | ≥ 8 | 0.1 |
| `pancake:1:1h` | 1 | `tuned_h5py` | 3 | 0.00 | 0.52 | 0.00 | 0.31 | **0.83** | 0.79–0.85 | 0.002 | 3 | 16.8 |
| `pancake:1:1h` | 1 | `download` | 3 | 0.01 | 0.00 | — | 1.27 | **1.29** | 1.13–2.18 | 1.046 | 3 | 25.4 |
| `pancake:1:1h` | 1 | `user_stack` | 3 | 0.00 | 1.85 | — | 0.01 | **1.86** | 1.80–2.49 | 0.008 | 2 | 25.4 |
| `pancake:15:1h` | 6 | `virtual_icechunk` | 3 | 0.00 | 0.44 | — | 0.06 | **0.50** | 0.49–0.51 | 0.046 | ≥ 8 | 0.1 |
| `pancake:15:1h` | 6 | `tuned_h5py` | 3 | 0.00 | 0.52 | 0.00 | 0.31 | **0.83** | 0.79–0.86 | 0.002 | 3 | 16.7 |
| `pancake:15:1h` | 6 | `download` | 3 | 0.01 | 0.00 | — | 1.32 | **1.33** | 1.31–1.34 | 1.106 | 3 | 25.5 |
| `pancake:15:1h` | 6 | `user_stack` | 3 | 0.00 | 7.88 | — | 0.03 | **7.92** | 7.12–8.63 | 0.051 | 12 | 153.7 |

## goes19: the whole question (seconds)

| Case | Granules | Arm | n | **End to end** | min–max | Last byte | Warm | Floor | × floor | Requests | MB on wire | Peak RSS MB | $ per question |
|---|---:|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `batch:0:1h` | 6 | `download` | 3 | **8.72** | 8.70–9.51 | 8.11 | 8.32 | 8.08 | 1.1 | 18 | 153.0 | 3,367.3 | 0.014245 |
| `batch:0:1h` | 6 | `tuned_h5py` | 3 | **9.44** | 8.96–9.47 | 8.84 | 6.52 | 7.91 | 1.2 | 24 | 153.4 | 3,383.0 | 0.014306 |
| `batch:0:1h` | 6 | `virtual_icechunk` | 3 | **10.71** | 10.63–10.89 | 10.11 | 9.63 | 8.50 | 1.3 | ≥ 1,363 | 157.2 | 3,367.5 | 0.015273 |
| `batch:0:1h` | 6 | `user_stack` | 3 | **13.25** | 13.11–13.45 | 12.59 | 5.43 | 7.87 | 1.7 | 12 | 153.7 | 3,954.5 | 0.014535 |
| `box:100:1h` | 6 | `virtual_icechunk` | 3 | **0.58** | 0.56–0.60 | 0.58 | 0.14 | 0.24 | 2.4 | ≥ 25 | 2.5 | 165.0 | 0.000264 |
| `box:100:1h` | 6 | `tuned_h5py` | 3 | **4.98** | 4.95–5.27 | 4.98 | 3.33 | 5.20 | 1.0 | 18 | 100.7 | 138.5 | 0.009334 |
| `box:100:1h` | 6 | `download` | 3 | **6.61** | 6.04–8.60 | 6.61 | 6.06 | 8.08 | 0.8 | 18 | 153.1 | 278.7 | 0.014123 |
| `box:100:1h` | 6 | `user_stack` | 3 | **7.23** | 6.92–7.31 | 7.23 | 0.07 | 7.87 | 0.9 | 12 | 152.4 | 367.1 | 0.014108 |
| `churro:10:1h` | 6 | `virtual_icechunk` | 3 | **1.08** | 1.06–1.16 | 1.08 | 0.29 | 0.35 | 3.1 | ≥ 43 | 4.5 | 196.4 | 0.000484 |
| `churro:10:1h` | 6 | `tuned_h5py` | 3 | **4.73** | 4.45–4.75 | 4.73 | 3.53 | 5.20 | 0.9 | 18 | 101.0 | 140.1 | 0.009340 |
| `churro:10:1h` | 6 | `user_stack` | 3 | **7.40** | 7.22–7.53 | 7.40 | 0.14 | 7.87 | 0.9 | 12 | 153.7 | 370.2 | 0.014221 |
| `churro:10:1h` | 6 | `download` | 3 | **7.83** | 5.84–8.52 | 7.83 | 6.35 | 8.08 | 1.0 | 18 | 153.0 | 285.9 | 0.014192 |
| `churro:250:1h` | 6 | `virtual_icechunk` | 3 | **1.16** | 1.15–1.21 | 1.16 | 0.37 | 0.46 | 2.5 | ≥ 61 | 6.6 | 198.1 | 0.000679 |
| `churro:250:1h` | 6 | `tuned_h5py` | 3 | **5.18** | 4.77–7.19 | 5.18 | 3.60 | 5.20 | 1.0 | 18 | 100.6 | 140.5 | 0.009355 |
| `churro:250:1h` | 6 | `download` | 3 | **6.48** | 6.07–6.61 | 6.48 | 6.07 | 8.08 | 0.8 | 18 | 152.9 | 278.0 | 0.014096 |
| `churro:250:1h` | 6 | `user_stack` | 3 | **7.32** | 6.95–7.35 | 7.32 | 0.20 | 7.87 | 0.9 | 12 | 153.7 | 372.8 | 0.014205 |
| `pancake:1:1h` | 1 | `tuned_h5py` | 3 | **1.60** | 1.59–1.65 | 1.49 | 0.36 | 1.39 | 1.2 | 4 | 25.4 | 707.6 | 0.002374 |
| `pancake:1:1h` | 1 | `download` | 3 | **1.70** | 1.68–1.84 | 1.59 | 1.32 | 1.34 | 1.3 | 3 | 25.5 | 733.1 | 0.002383 |
| `pancake:1:1h` | 1 | `virtual_icechunk` | 3 | **2.09** | 2.08–2.10 | 1.98 | 1.57 | 1.52 | 1.4 | ≥ 233 | 26.5 | 710.0 | 0.002588 |
| `pancake:1:1h` | 1 | `user_stack` | 3 | **2.84** | 2.72–2.98 | 2.73 | 0.90 | 1.34 | 2.1 | 2 | 25.6 | 790.8 | 0.002454 |
| `pancake:15:1h` | 6 | `tuned_h5py` | 3 | **9.53** | 8.88–9.92 | 8.93 | 6.80 | 7.91 | 1.2 | 24 | 153.4 | 3,382.9 | 0.014314 |
| `pancake:15:1h` | 6 | `virtual_icechunk` | 3 | **10.33** | 10.31–10.94 | 9.71 | 9.20 | 8.49 | 1.2 | ≥ 1,363 | 157.1 | 3,367.8 | 0.015233 |
| `pancake:15:1h` | 6 | `download` | 3 | **10.60** | 9.97–11.14 | 9.99 | 9.04 | 8.08 | 1.3 | 18 | 153.3 | 3,375.3 | 0.014381 |
| `pancake:15:1h` | 6 | `user_stack` | 3 | **12.86** | 12.84–13.03 | 12.27 | 5.35 | 7.87 | 1.6 | 12 | 152.4 | 3,976.1 | 0.014417 |

## goes19: paired speedups

| Case | Measure | From | To | **Speedup** | 95% interval | Pairs |  |
|---|---|---|---|---:|---|---:|---|
| `batch:0:1h` | first_byte | `download` | `virtual_icechunk` | **2.42×** | 2.23–2.91 | 3 |  |
| `batch:0:1h` | first_byte | `tuned_h5py` | `virtual_icechunk` | **1.81×** | 1.73–1.90 | 3 |  |
| `batch:0:1h` | first_byte | `user_stack` | `virtual_icechunk` | **15.66×** | 14.77–16.33 | 3 |  |
| `box:100:1h` | first_byte | `download` | `virtual_icechunk` | **2.43×** | 2.37–4.23 | 3 |  |
| `box:100:1h` | first_byte | `tuned_h5py` | `virtual_icechunk` | **1.77×** | 1.61–2.10 | 3 |  |
| `box:100:1h` | first_byte | `user_stack` | `virtual_icechunk` | **14.18×** | 13.89–15.53 | 3 |  |
| `churro:10:1h` | first_byte | `download` | `virtual_icechunk` | **1.32×** | 1.26–1.40 | 3 |  |
| `churro:10:1h` | first_byte | `tuned_h5py` | `virtual_icechunk` | **0.94×** | 0.89–0.96 | 3 |  |
| `churro:10:1h` | first_byte | `user_stack` | `virtual_icechunk` | **8.75×** | 8.32–8.78 | 3 |  |
| `churro:250:1h` | first_byte | `download` | `virtual_icechunk` | **1.45×** | 1.43–1.57 | 3 |  |
| `churro:250:1h` | first_byte | `tuned_h5py` | `virtual_icechunk` | **0.99×** | 0.95–1.02 | 3 |  |
| `churro:250:1h` | first_byte | `user_stack` | `virtual_icechunk` | **8.85×** | 8.18–8.88 | 3 |  |
| `pancake:1:1h` | first_byte | `download` | `virtual_icechunk` | **2.82×** | 2.55–4.87 | 3 |  |
| `pancake:1:1h` | first_byte | `tuned_h5py` | `virtual_icechunk` | **1.84×** | 1.79–1.86 | 3 |  |
| `pancake:1:1h` | first_byte | `user_stack` | `virtual_icechunk` | **4.08×** | 4.07–5.55 | 3 |  |
| `pancake:15:1h` | first_byte | `download` | `virtual_icechunk` | **2.65×** | 2.63–2.66 | 3 |  |
| `pancake:15:1h` | first_byte | `tuned_h5py` | `virtual_icechunk` | **1.64×** | 1.56–1.73 | 3 |  |
| `pancake:15:1h` | first_byte | `user_stack` | `virtual_icechunk` | **15.64×** | 14.40–17.10 | 3 |  |
| `batch:0:1h` | full | `download` | `virtual_icechunk` | **0.82×** | 0.80–0.89 | 3 |  |
| `batch:0:1h` | full | `tuned_h5py` | `virtual_icechunk` | **0.87×** | 0.84–0.88 | 3 |  |
| `batch:0:1h` | full | `user_stack` | `virtual_icechunk` | **1.23×** | 1.23–1.24 | 3 |  |
| `box:100:1h` | full | `download` | `virtual_icechunk` | **11.06×** | 10.34–15.29 | 3 |  |
| `box:100:1h` | full | `tuned_h5py` | `virtual_icechunk` | **8.80×** | 8.52–8.83 | 3 |  |
| `box:100:1h` | full | `user_stack` | `virtual_icechunk` | **12.30×** | 12.09–12.51 | 3 |  |
| `churro:10:1h` | full | `download` | `virtual_icechunk` | **6.74×** | 5.49–7.88 | 3 |  |
| `churro:10:1h` | full | `tuned_h5py` | `virtual_icechunk` | **4.12×** | 4.09–4.45 | 3 |  |
| `churro:10:1h` | full | `user_stack` | `virtual_icechunk` | **6.68×** | 6.37–7.08 | 3 |  |
| `churro:250:1h` | full | `download` | `virtual_icechunk` | **5.56×** | 5.01–5.75 | 3 |  |
| `churro:250:1h` | full | `tuned_h5py` | `virtual_icechunk` | **4.27×** | 4.15–6.18 | 3 |  |
| `churro:250:1h` | full | `user_stack` | `virtual_icechunk` | **6.06×** | 5.97–6.36 | 3 |  |
| `pancake:1:1h` | full | `download` | `virtual_icechunk` | **0.81×** | 0.81–0.87 | 3 |  |
| `pancake:1:1h` | full | `tuned_h5py` | `virtual_icechunk` | **0.77×** | 0.76–0.79 | 3 |  |
| `pancake:1:1h` | full | `user_stack` | `virtual_icechunk` | **1.36×** | 1.31–1.42 | 3 |  |
| `pancake:15:1h` | full | `download` | `virtual_icechunk` | **1.02×** | 0.97–1.03 | 3 |  |
| `pancake:15:1h` | full | `tuned_h5py` | `virtual_icechunk` | **0.91×** | 0.86–0.92 | 3 |  |
| `pancake:15:1h` | full | `user_stack` | `virtual_icechunk` | **1.24×** | 1.19–1.24 | 3 |  |

Speedups measured in one session have no spread between sessions to be judged against: whether they are a detectable difference is not known until a second session is run.

## Reading the tables

- A dash under a part means the arm does not expose that step; its time is inside Bytes:
  - `download`: no chunk selection: the whole file is fetched.
  - `download`: no index: the whole file is fetched.
  - `tuned_h5py`: inside the read: each process looks up its own granule's chunks.
  - `user_stack`: inside the read: h5netcdf resolves chunks as it reads.
  - `virtual_icechunk`: inside the read: Icechunk fetches the chunk manifest on the first chunk request.
- Requests marked ≥ are a static minimum from the chunk grid, not a count. Other counts are byte ranges the client asked for.
- MB on wire is the host's receive counter over the trial, so it includes other traffic on a shared host.
- Floor = dependent round trips × median round-trip time + bytes ÷ measured bandwidth, from the session's probe.
- $ per question = S3 requests + transfer + compute at list price. Transfer is zero in region and internet egress otherwise.
- A speedup is the median, over trials paired by session and repetition, of one arm's seconds over another's. Its interval is a bootstrap of that median.
- Arms marked † are not gated: their values were not checked against the reference arm.

## Failed trials

- virtual_icechunk batch:0 full: StorageError: error fetching virtual reference: dispatch failure: other: client error (SendRequest): connection closed before message completed

context:
   0: icechunk::store::get
           with key
