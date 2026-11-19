"""One figure per case: end to end for each arm, split into setting up and reading.

``run.py report`` writes these beside the summary, as SVG (text stays text,
so a slide deck can restyle it) and PNG. The CODA deck's benchmark figures
are these files, copied into ``decks/coda-presentation/public/figures/``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

from benchmarks.trade_study.scope import ARM_INFO

# One colour per kind of library (validated as a pair); the part of the time is a texture.
VIRTUAL_COLOR, OTHER_COLOR = "#952f7c", "#d97e6a"
INK, MUTED = "#3f3f48", "#6b6b76"
# svg.fonttype none keeps text as text. Inter is the deck's font; it is often not installed where this runs.
STYLE = {"font.family": ["Inter", "DejaVu Sans", "sans-serif"], "font.size": 13, "svg.fonttype": "none"}
HATCH = {"hatch.color": "white", "hatch.linewidth": 1.5}
ARM_ORDER = list(ARM_INFO)  # bars run top to bottom in the scope's order
VARIANT_WORDS = {"max_workers": ("process", "processes")}


def bar_label(arm: str, variant: str) -> str:
    """ "Tuned h5py", or "Tuned h5py, 1 process" for the variant ``{"max_workers": 1}``."""
    title = ARM_INFO[arm].title if arm in ARM_INFO else arm
    if not variant:
        return title
    parts = []
    for key, value in json.loads(variant).items():
        if key in VARIANT_WORDS:
            one, many = VARIANT_WORDS[key]
            parts.append(f"{value} {one if value == 1 else many}")
        else:
            parts.append(f"{key}={value}")
    return f"{title}, {', '.join(parts)}"


def caption(cell: dict[str, Any]) -> str:
    """What the case asks, in words, for the axis label."""
    size, n = cell["case_size"], cell["observation_count"]
    granules = f"{n} granule{'' if n == 1 else 's'}"
    return {
        "pancake": f"whole map of {granules}",
        "churro": f"{size} points over {granules}",
        "box": f"{size} km box over {granules}",
        "batch": f"every chunk of {granules}",
    }[cell["workload"]]


def setup_and_read(cell: dict[str, Any]) -> tuple[float, float]:
    """(seconds authenticating and opening, the rest of end to end)."""
    setup = (cell["auth_seconds"] or 0.0) + (cell["manifest_seconds"] or 0.0)
    return setup, cell["end_to_end_seconds"] - setup


def draw_case(cells: list[dict[str, Any]], path: Path) -> Path:
    """One horizontal bar per cell: setup hatched, reading solid, the total at
    the bar's end. Writes ``path`` (SVG) and a PNG beside it."""
    cells = sorted(cells, key=lambda c: (ARM_ORDER.index(c["arm"]) if c["arm"] in ARM_ORDER else 99, c["variant"]))
    cells = cells[::-1]  # matplotlib draws the first bar at the bottom
    labels = [bar_label(cell["arm"], cell["variant"]) for cell in cells]
    setup, read = (np.array(part) for part in zip(*(setup_and_read(cell) for cell in cells), strict=True))
    totals = setup + read
    colors = [VIRTUAL_COLOR if cell["arm"].startswith("virtual") else OTHER_COLOR for cell in cells]
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    with plt.rc_context({**STYLE, **HATCH}):
        fig, ax = plt.subplots(figsize=(11, 0.75 * len(cells) + 1.2))
        gap = 0.04 * totals.max() / 30  # a sliver of white between the two segments
        ax.barh(labels, np.maximum(setup - gap, 0), color=colors, height=0.62, hatch="//", linewidth=0)
        ax.barh(labels, read, left=setup, color=colors, height=0.62, linewidth=0)
        for y, total in enumerate(totals):
            ax.text(total + totals.max() * 0.012, y, f"{total:.1f} s", va="center", color=INK)
        ax.set_xlabel(f"seconds, {caption(cells[0])} (median of cold runs)", color=MUTED)
        ax.set_xlim(0, totals.max() * 1.12)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(MUTED)
        ax.tick_params(axis="y", length=0, colors=INK)
        ax.tick_params(axis="x", colors=MUTED)
        legend = [
            Patch(facecolor=VIRTUAL_COLOR, label="Virtual Zarr"),
            Patch(facecolor=OTHER_COLOR, label="Other libraries"),
            Patch(facecolor=MUTED, hatch="//", linewidth=0, label="Setup + open"),
            Patch(facecolor=MUTED, label="Read + decode"),
        ]
        # above the plot: the longest bar runs into any corner inside it
        ax.legend(handles=legend, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=4, frameon=False, labelcolor=INK)
        fig.tight_layout()
        fig.savefig(path, format="svg")
        fig.savefig(path.with_suffix(".png"), format="png", dpi=160)
        plt.close(fig)
    return path


def figure_name(cell: dict[str, Any]) -> str:
    """``tempo_no2_box_100_1d.svg``; the transport is added when it is not direct S3."""
    transport = "" if cell["transport"] == "s3" else f"-{cell['transport']}"
    return f"{cell['dataset']}{transport}_{cell['workload']}_{cell['case_size']}_{cell['window']}.svg"


def write_figures(summary: dict[str, Any], out_dir: Path) -> list[Path]:
    """A figure for every whole-question case with at least two arms to compare.
    In-region and out-of-region cells never share a figure: when a run holds
    both for one case, each figure's name says which it is."""
    by_case: dict[str, dict[bool, list[dict[str, Any]]]] = {}
    for cell in summary["cells"]:
        if cell["measure"] == "full" and cell["end_to_end_seconds"] is not None:
            by_case.setdefault(figure_name(cell), {}).setdefault(bool(cell["in_region"]), []).append(cell)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, by_region in sorted(by_case.items()):
        for in_region, cells in sorted(by_region.items()):
            if len(cells) < 2:
                continue
            path = out_dir / name
            if len(by_region) > 1:
                path = path.with_name(f"{path.stem}_{'in-region' if in_region else 'out-of-region'}.svg")
            written.append(draw_case(cells, path))
    return written
