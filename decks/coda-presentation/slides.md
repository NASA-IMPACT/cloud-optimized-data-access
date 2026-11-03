---
theme: default
title: One file is easy
info: CODA, 3 November 2026. The figures and result slides are written by benchmarks/tempo_no2/analyze.py into public/; the numbers in the slide copy are from run "hub".
colorSchema: light
transition: fade
mdc: true
routerMode: hash
fonts:
  sans: Inter
addons:
  - '@alukach/slidev-addon-qrcode'
layout: cover
class: gradient text-center
---

<span class="eyebrow">CODA · 3 November 2026</span>

# One file is easy

<p class="text-xl opacity-90">Virtualization, software, and the case for systematic benchmarking</p>

<p class="opacity-80">Max Jones · NASA ODSI</p>

---

<span class="eyebrow">The argument</span>

# Three steps to <span class="accent">fast I/O everywhere</span>

<div class="cards">
  <div class="card"><b>1 · One file</b>Software optimizations alone make a single file fast, if you ignore the time to find it.</div>
  <div class="card"><b>2 · Thousands of files</b>Virtualization plus the same software extends those gains to thousands of files, with no backend.</div>
  <div class="card"><b>3 · Virtualization and software</b>Speeding up existing services (OPeNDAP), libraries (Xarray) and GIS (ArcGIS, QGIS) via Zarr.</div>
</div>

---
layout: section
class: gradient
---

<span class="eyebrow">Part 1</span>

# Software optimizations alone speed up <span class="accent">a single file</span>

---

<span class="eyebrow">One granule</span>

# Tuned, the library <span class="accent">barely matters</span>

<Figure src="figures/scan_1.svg" alt="One granule, whole scan: default stack 26.9 s, almost all of it setup and open; tuned h5py 4.6 s; virtual Zarr 4.6 s; whole-file download then h5py 5.4 s" />

<p class="text-sm">The default stack spends 25 s opening the file. Tuned h5py, virtual Zarr and a whole-file download land within a second of each other.</p>

<!--
Whole scan of one observation (granule S014 of 2025-03-13, 515 MB), 10 cold runs per library, medians. Default and tuned (1 process) from session 2, virtual from session 1, download from sessions 3–4.
Default: earthaccess + xarray.open_mfdataset(engine="h5netcdf"); fsspec's read-ahead pulls 1.9 GB (about 4× the file) while opening. Tuned: 8 MiB block reader, one process, 85 MB.
No floor line: a single-core decode floor (1.5 s here) stops being a floor once a library decodes in parallel, which virtual Zarr does at 15 granules (next slide).
Figure: analyze.py scan_1.svg. The deck's copy is a preview drawn from the medians above; rerun analyze.py on the hub results to regenerate it.
-->

---




<span class="eyebrow">Fifteen granules, one day</span>

# Fifteen files, and the libraries <span class="accent">pull apart</span>

<Figure src="figures/scan_15.svg" alt="Whole scan of 15 granules: tuned h5py with 1 process 59.3 s, with 8 processes 28.5 s, virtual Zarr 21.6 s, whole-file download then h5py 60.7 s" />

<p class="text-sm">Fifteen times the files: virtual Zarr takes 4.7× as long, tuned h5py on one process 13×. Downloading every file moves 10.9 GB.</p>

<!--
scan:15:1d, session 5, 10 cold runs each, medians: tuned 1-process 59.3 s, tuned 8-process 28.5 s, virtual 21.6 s, download + h5py 60.7 s (download 27.3 s, decode 27.9 s). Bytes over the wire: virtual 0.68 GB, tuned 1.47 GB, download 10.9 GB. Peak memory 9–12 GB for all of them: the scan keeps fifteen full grids.
Virtual Zarr decodes in parallel (about 39 CPU-seconds over 21.6 s); the download arm decodes one granule at a time on one core.
Same workload as the previous slide, so the two figures compare directly. CMR search for the day's files: 0.7 s, before any of this.
Figure: analyze.py scan_15.svg, a preview copy as above.
-->

---
layout: section
class: gradient
---

<span class="eyebrow">Part 2</span>

# Virtualization + software extend it to <span class="accent">thousands of files</span>

---

# Three ways to make many files <span class="accent">one dataset</span>

<div class="cards">
  <div class="card"><b>Virtualization</b>A table of contents over the files as they are. No reprocessing, no duplication.</div>
  <div class="card"><b>Caching via a service</b>A service reads the archive once and answers repeat questions from its cache.</div>
  <div class="card"><b>Reprocessing</b>Rewrite the archive into a cloud-optimized layout. Fastest reads, but a second copy to produce and keep in step.</div>
</div>

<!--
Ordered by how much of the archive you have to touch. Virtualization is the cheapest; the rest of the talk measures what it buys.
-->

---

<span class="eyebrow">Case study</span>

# Virtualization performance: <span class="accent">NLDAS-3</span>

<Figure src="figures/external/nldas-redrawn.svg" alt="NLDAS-3 time-series extraction over one month of daily files: original NetCDF 805 s for 1 point and 823 s for 5 points, almost all of it opening the dataset; Virtual Zarr 4.1 s and 3.9 s, 196 and 210 times faster" />

<p class="credits">Data: Siddharth Chaudhary, NASA ODSI DSE · <a href="https://github.com/virtual-zarr/nldas-icechunk/pull/5">github.com/virtual-zarr/nldas-icechunk/pull/5</a> · redrawn</p>

<!--
≈200× end to end: 805 s vs 4.1 s (1 point), 823 s vs 3.9 s (5 points); opening 763 s vs 1.9 s (≈400×). One run each, one month of daily files (31), Virtual Zarr vs the original NetCDF. Notebook in an open PR (head 01c719c).
-->

---

<span class="eyebrow">Case study</span>

# Virtualization performance: <span class="accent">TEMPO NO₂</span>

<Figure src="figures/external/odsi-tempo-speedup.png" alt="TEMPO median paired speedup of Virtual Zarr: rising to about 140 times at 250 points; 12 to 14 times for boxes of 50 to 500 km" />

<p class="credits">Siddharth Chaudhary, NASA ODSI DSE · <a href="https://siddharth0248.github.io/presentation-deck/">siddharth0248.github.io/presentation-deck</a></p>

<!--
≈140× at 250 points, 12–14× for 50–500 km boxes, median paired speedup vs remote NetCDF. The baseline client is not stated; against tuned h5py this repository measures 1.7–2.0× over a day.
Likely source: exploration/benchmark_no2_event.ipynb in virtual-zarr/tempo-virtual-zarr-pipeline (same matrix and day). Its NetCDF baseline reads each point with its own h5py read on one process, so the gain grows with the point count. Rerun it to replace this plot with exact medians from paired-comparisons.csv.
-->

---

<span class="eyebrow">Case study</span>

# Virtualization performance: <span class="accent">MUR SST</span>

<Figure src="figures/external/mur-sst-redrawn.svg" alt="MUR SST points read per second for one 1 by 1 degree box: Icechunk store with zarr-python 2,157,000, original NetCDF granules with open_mfdataset 25,000, 86 times the throughput" />

<p class="credits">Data: Harkins &amp; Barciauskas, ESDIS spotlight · <a href="https://nasa-impact.github.io/esdis-spotlight-virtualization/">nasa-impact.github.io/esdis-spotlight-virtualization</a> · data from earth-mover/icechunk-nasa @ 9a9d0cbc · redrawn</p>

<!--
≈86× read throughput: 21 years from the Icechunk store in 36 s vs one month of NetCDF granules with xarray.open_mfdataset in 48 s, for one 1° × 1° box.
-->

---

<span class="eyebrow">Case study</span>

# Virtualization performance: <span class="accent">GPM IMERG</span>

<Figure src="figures/external/gpm-imerg-redrawn.svg" alt="Time to extract a one-month time series from 1,488 GPM IMERG granules: Giovanni on premises 45 s, Xarray with h5py in the cloud 307 s, Xarray with Icechunk in the cloud 3 s, 102 times faster than h5py and 15 times faster than Giovanni" />

<p class="credits">Data: Ryan Abernathey, Earthmover, 27 March 2025 · <a href="https://www.earthmover.io/blog/nasa-icechunk">earthmover.io/blog/nasa-icechunk</a> · redrawn</p>

<!--
One-month precipitation time series from the original NetCDF granules (1,488 files): Xarray open_mfdataset with h5py and s3fs from S3 307 s; virtual Icechunk store 3 s (the blog's "100×"); NASA's on-prem Giovanni on POSIX storage 45 s, so the virtual store also beats the existing on-prem service by about 15×.
The ESDIS spotlight reports a separate 45× for opening and reading one day; a different test.
-->

---

<span class="eyebrow">Systematic benchmarking</span>

# How to go from case studies to <span class="accent">a trade study</span>

<p>Every case study finds a gain, but each against its own baseline. To choose what to promote, the options need one benchmark.</p>

<div class="cards">
  <div class="card"><b>More than time</b>Time to first byte, end to end, peak memory and cost per question, on every run.</div>
  <div class="card"><b>"Why isn't it twice as fast?"</b>Each result is explained against a floor, not just ranked.</div>
  <div class="card"><b>"Are the data always correct?"</b>Every access path is checked for the same answer before it is timed.</div>
  <div class="card"><b>Tuned versions of every option</b>Virtualization, caching services and reprocessing, each at its best. No strawmen.</div>
</div>

<!--
Built today: time, peak memory and cost per run, the floor, and the correctness gate, for tuned NetCDF libraries and virtual Zarr. Proposed: the caching-service and reprocessed-Zarr arms, under the same protocol.
-->

---

<span class="eyebrow">Systematic benchmarking</span>

# An example: TEMPO across <span class="accent">tuned libraries</span>

<Figure src="figures/aoi_100.svg" alt="100 km box over one day: tuned h5py with 1 process 26.4 s, with 8 processes 8.1 s, Virtual Zarr 4.7 s, each split into setup and open (hatched) and read and decode" />

<p class="credits">This work · one event day of TEMPO NO₂ (15 granules), 100 km box, 10 cold runs per library, medians</p>

<!--
Same files, same question, every library tuned: h5py with 8 MiB blocks at 1 process (26.4 s) and 8 processes (8.1 s), and Virtual Zarr through Icechunk (4.7 s). Tuning the client is worth 3.3×; virtualization adds 1.7× on top. Answers checked equal across libraries.
-->

---
layout: section
class: gradient
---

<span class="eyebrow">Part 3</span>

# Connections between virtualization <span class="accent">and software</span>

<p class="opacity-80">Speeding up existing services via Zarr</p>

---

<span class="eyebrow">Powered by Zarr</span>

# One interface, <span class="accent">lightning-fast I/O</span> everywhere

<div class="cards">
  <div class="card"><b>Services</b>OPeNDAP, Harmony and Giovanni reading the same references.</div>
  <div class="card"><b>Software libraries</b>Xarray and the tools built on it.</div>
  <div class="card"><b>GIS applications</b>ArcGIS and QGIS.</div>
</div>

<p class="mt-6">Today each layer (Dask, fsspec, Zarr) tunes its own concurrency and caching without knowing about the others. We can optimize the I/O to no end, but people won't see it unless the layers above it share the same fast core.</p>

---

# Zarr Python's experience <span class="accent">with performance</span>

<div class="beside-image">
<div>
<div class="cards stacked">
  <div class="card"><b>Zarr Python 2</b>Fast on local, HPC and attached file systems.</div>
  <div class="card"><b>Zarr Python 3</b>Built for object storage: fully async, tunable concurrency.</div>
</div>

<p class="text-xs mt-4 opacity-70">Zarr Python 2 is a library version, not the Zarr v2 specification.</p>
</div>
<Figure src="meme-concurrency.png" alt="Three Spider-Men pointing at each other, labelled Dask task graph, FSSpec cache and Zarr concurrency" />
</div>

---

<span class="eyebrow">Powered by Zarr</span>

# Zarr's active pursuit of <span class="accent">a rich ecosystem</span>

<p>A harmonized API and fast core for software, web applications, GUIs, humans, and agents.</p>

<div class="cards">
  <div class="card"><b>Modular, pluggable</b>Rust components underneath a Zarr-Python API.</div>
  <div class="card"><b>Async and sync</b>Async for high-latency storage, sync for low latency.</div>
  <div class="card"><b>Lazy primitives</b>Built for the libraries downstream.</div>
  <div class="card"><b>Measured</b>An ongoing focus on monitoring, systems performance evaluation and correctness.</div>
</div>

---
layout: cover
class: gradient text-center
---

# Thank you
