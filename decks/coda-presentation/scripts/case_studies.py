"""Redraw case-study figures from other teams' published numbers in the deck's style.

    uv run python decks/coda-presentation/scripts/case_studies.py

Only numbers the authors published are used; nothing is read off a plot.
NLDAS-3: the timing table in the benchmark notebook of virtual-zarr/nldas-icechunk PR #5
(Siddharth Chaudhary, head 01c719c), one run each, one month of daily files.
GPM IMERG: the bar labels of the benchmark chart in earthmover.io/blog/nasa-icechunk (Ryan Abernathey,
27 March 2025), one-month time series from 1,488 granules.
MUR SST: the description written into images/mur-sst-throughput.svg of
nasa-impact.github.io/esdis-spotlight-virtualization (Harkins & Barciauskas).
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
from matplotlib.patches import Patch

OUT = Path(__file__).resolve().parents[1] / "public" / "figures" / "external"
# The deck's colours: Icechunk / virtual Zarr magenta, every other library coral (as in analyze.py)
VIRTUAL, OTHER, INK, MUTED = "#952f7c", "#d97e6a", "#3f3f48", "#6b6b76"
HATCH = {"hatch.color": "white", "hatch.linewidth": 1.5}
STYLE = {"font.family": ["Inter", "DejaVu Sans", "sans-serif"], "font.size": 13, "svg.fonttype": "none"}

# (case, library, open s, query s, end to end s)
NLDAS = (
    ("1 point", "Original NetCDF", 762.72, 42.63, 805.35),
    ("1 point", "Virtual Zarr", 1.93, 2.19, 4.11),
    ("5 points", "Original NetCDF", 762.72, 60.16, 822.88),
    ("5 points", "Virtual Zarr", 1.93, 1.99, 3.92),
)

# (label, seconds) top to bottom
GPM = (
    ("Giovanni · on premises", 45),
    ("Xarray + h5py · cloud", 307),
    ("Xarray + Icechunk · cloud", 3),
)
# (label, points read per second) top to bottom
MUR = (
    ("Icechunk store · zarr-python", 2_157_000),
    ("Original NetCDF · open_mfdataset", 25_000),
)


def _axes_style(ax) -> None:
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(MUTED)
    ax.tick_params(axis="y", length=0, colors=INK)
    ax.tick_params(axis="x", colors=MUTED)


def nldas(path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(11, 3.9))
    rows = NLDAS[::-1]
    labels = [f"{library} · {case}" for case, library, *_ in rows]
    opens = [r[2] for r in rows]
    queries = [r[3] for r in rows]
    colors = [VIRTUAL if library == "Virtual Zarr" else OTHER for _, library, *_ in rows]
    with plt.rc_context(HATCH):
        ax.barh(labels, opens, color=colors, height=0.62, hatch="//", linewidth=0)
    ax.barh(labels, queries, left=opens, color=colors, height=0.62, linewidth=0)
    top = max(r[4] for r in rows)
    netcdf = {case: total for case, library, _, _, total in NLDAS if library == "Original NetCDF"}
    for y, (case, library, _, _, total) in enumerate(rows):
        label = f"{total:,.0f} s" if total >= 10 else f"{total:.1f} s"
        if library == "Virtual Zarr":
            label += f" · {netcdf[case] / total:,.0f}× faster"
        ax.text(total + top * 0.012, y, label, va="center", color=INK)
    ax.set_xlabel("seconds, one month of daily files, time-series extraction (one run each)", color=MUTED)
    ax.set_xlim(0, top * 1.12)
    _axes_style(ax)
    with plt.rc_context(HATCH):
        handles = [
            Patch(facecolor=VIRTUAL, label="Virtual Zarr"), Patch(facecolor=OTHER, label="Other libraries"),
            Patch(facecolor=MUTED, hatch="//", linewidth=0, label="Open the dataset"), Patch(facecolor=MUTED, label="Query"),
        ]
        ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=4, frameon=False, labelcolor=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def _bars(rows, labels_for, xlabel: str, path: Path, headroom: float = 1.3, ticks=None) -> Path:
    """One bar per row (these studies publish totals, not phases): Icechunk magenta, others coral, direct labels."""
    fig, ax = plt.subplots(figsize=(11, 0.75 * len(rows) + 1.2))
    rows = rows[::-1]
    colors = [VIRTUAL if "Icechunk" in r[0] else OTHER for r in rows]
    ax.barh([r[0] for r in rows], [r[1] for r in rows], color=colors, height=0.62)
    top = max(r[1] for r in rows)
    for y, row in enumerate(rows):
        ax.text(row[1] + top * 0.012, y, labels_for(row), va="center", color=INK)
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_xlim(0, top * headroom)
    if ticks:
        ax.xaxis.set_major_formatter(ticks)
    _axes_style(ax)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def gpm(path: Path) -> Path:
    seconds = dict(GPM)

    def label(row):
        name, value = row
        if "Icechunk" not in name:
            return f"{value} s"
        cloud, onprem = seconds["Xarray + h5py · cloud"], seconds["Giovanni · on premises"]
        return f"{value} s · {cloud / value:.0f}× faster than h5py, {onprem / value:.0f}× than Giovanni"

    return _bars(GPM, label, "seconds to extract a one-month time series from 1,488 granules", path)


def mur(path: Path) -> Path:
    netcdf = dict(MUR)["Original NetCDF · open_mfdataset"]

    def label(row):
        name, value = row
        return f"{value:,} points/s" + (f" · {value / netcdf:.0f}×" if "Icechunk" in name else "")

    millions = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1e6:g}M")
    return _bars(MUR, label, "points read per second, one 1° × 1° box", path, headroom=1.5, ticks=millions)


def main() -> None:
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # Inter is often not installed where this runs
    OUT.mkdir(parents=True, exist_ok=True)
    with plt.rc_context(STYLE):
        for draw, name in ((nldas, "nldas-redrawn"), (gpm, "gpm-imerg-redrawn"), (mur, "mur-sst-redrawn")):
            print(draw(OUT / f"{name}.svg"))


if __name__ == "__main__":
    main()
