# CODA presentation

Slidev deck skeleton with a placeholder title.

```sh
npm install
npm run dev        # http://localhost:3030
npm run build      # static site in dist/
npm run export     # slides.pdf, needs `npx playwright install chromium` once
```

The result slides read `public/results/summary.json` and the figures in
`public/figures/`, both written by `benchmarks/tempo_no2/analyze.py`. Without
them the slides show a "Benchmark not yet run" panel. Once
`results/tempo_no2/latest.json` exists, the build refuses a deck whose copy
names a different run.
