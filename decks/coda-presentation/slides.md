---
theme: default
title: CODA presentation
info: Placeholder deck. The result slides read public/results/summary.json, written by benchmarks/tempo_no2/analyze.py.
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

# CODA presentation

Placeholder title

---

# Results: where the seconds go

<ResultsPanel kind="phases" />

---

# Results: requests and floor

<ResultsPanel kind="requests" />
