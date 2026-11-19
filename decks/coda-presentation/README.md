# CODA presentation

"One file is easy": the talk given to the CODA working group on 3 November
2026, as a [Slidev](https://sli.dev) deck.

```sh
npm ci
npm run dev        # http://localhost:3030
npm run build      # static site in dist/
npm run export     # slides.pdf, needs `npx playwright install chromium` once
```

## What is where

| File | What |
|---|---|
| `slides.md` | Every slide, with speaker notes in HTML comments. The notes say where each number comes from. |
| `styles/index.css` | The look: gradient, cards, figure sizes. |
| `components/Figure.vue` | An image from `public/`. A missing file shows its alt text. |
| `public/figures/` | Figures from this repository's benchmark. |
| `public/figures/external/` | Other teams' published numbers, redrawn in the deck's style by `scripts/case_studies.py`. |

## The figures

**Case studies** (`public/figures/external/*-redrawn.svg`): the numbers are
written out at the top of `scripts/case_studies.py`, each with its source.
To change one, edit it there and run, from the repository root:

```sh
uv run python decks/coda-presentation/scripts/case_studies.py
```

**This repository's benchmark** (three slides): `run.py report` draws one
figure per question into the run's `figures/` directory. Copy the three the
deck uses:

```sh
run_dir=results/trade_study/<run id>
uv run python -m benchmarks.trade_study.run report $run_dir
cp $run_dir/figures/tempo_no2_{pancake_1,pancake_15,box_100}_1d.svg decks/coda-presentation/public/figures/
```

> **These three files are not in the repository yet.** The talk's numbers
> come from a run called "hub", made in us-west-2 with the first version of
> the benchmark, and its `results.jsonl` was never committed. Put that file
> in a directory under `results/trade_study/` and run the two commands
> above: `report` reads the old format. Until then those three slides show
> their alt text.

## Changing a number

The numbers in slide text and speaker notes are typed by hand; a figure does
not update them. After a new run, search `slides.md` for the old value (for
example `26.4 s`) and change the slide text, the figure's alt text and the
speaker note together. The medians and speedups to copy from are in the
run's `summary.md`.
