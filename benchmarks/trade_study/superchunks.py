"""What grouping chunks into larger reads can and cannot buy, from layouts alone.

A superchunk (Hyrax) or buffer chunk is several stored chunks fetched as one
byte range. Whoever issues the read, the arithmetic is the same: take the
chunks a question needs, sort them by file offset, and merge neighbours
whose gap is at most ``max_gap`` while the merged read stays within
``max_bytes``. This module does that arithmetic on a granule's real chunk
index and reports requests, bytes and the over-read. It times nothing.

What the arithmetic cannot change, whatever the grouping:

- one read never spans two files, so a question over N granules needs at
  least N requests;
- every chunk is still decompressed on its own, so CPU time is unchanged;
- the first value arrives only when the first grouped read has landed, so
  larger groups trade first byte for fewer requests;
- a group is held in memory whole.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from benchmarks.trade_study.datasets import Dataset
from benchmarks.trade_study.h5 import chunk_keys
from benchmarks.trade_study.questions import Selection, headline_cases
from benchmarks.trade_study.stores import build_store, store_path

GAPS = (0, 64 * 2**10, 2**20, 8 * 2**20)
DEFAULT_MAX_BYTES = 64 * 2**20
ONE_REQUEST_PER_CHUNK = -1  # a Grouping's ``max_gap`` when nothing is merged


@dataclass(frozen=True)
class Grouping:
    max_gap: int
    requests: int
    bytes_read: int
    bytes_needed: int

    @property
    def over_read(self) -> float:
        return self.bytes_read / self.bytes_needed - 1 if self.bytes_needed else 0.0


def group(ranges: list[tuple[int, int]], max_gap: int, max_bytes: int = DEFAULT_MAX_BYTES) -> list[tuple[int, int]]:
    """Merge (offset, length) ranges whose gap is ≤ ``max_gap``, keeping each
    merged read ≤ ``max_bytes`` (a single range larger than that stays whole)."""
    merged: list[list[int]] = []
    for offset, length in sorted(ranges):
        if merged:
            start, end = merged[-1]
            if offset - end <= max_gap and max(end, offset + length) - start <= max_bytes:
                merged[-1][1] = max(end, offset + length)
                continue
        merged.append([offset, offset + length])
    return [(start, end - start) for start, end in merged]


def needed_ranges(handle: Any, variables: tuple[str, ...], sel: Selection) -> list[tuple[int, int]]:
    """(offset, length) of every stored chunk the selection touches in one open granule."""
    out = []
    for path in variables:
        dataset = handle[path]
        lead = (0,) * (dataset.ndim - 2)
        ch, cw = dataset.chunks[-2], dataset.chunks[-1]
        for row, col in chunk_keys(dataset, sel):
            info = dataset.id.get_chunk_info_by_coord((*lead, row * ch, col * cw))
            if info.byte_offset is not None and info.size:
                out.append((int(info.byte_offset), int(info.size)))
    return out


def analyse(
    ranges: list[tuple[int, int]],
    n_granules: int = 1,
    gaps: tuple[int, ...] = GAPS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[Grouping]:
    """One granule's needed ranges, scaled to ``n_granules`` files with the same layout."""
    needed = int(np.sum([length for _, length in ranges])) * n_granules if ranges else 0
    groupings = [Grouping(ONE_REQUEST_PER_CHUNK, len(ranges) * n_granules, needed, needed)]
    for gap in gaps:
        merged = group(ranges, gap, max_bytes)
        bytes_read = sum(length for _, length in merged) * n_granules
        groupings.append(Grouping(gap, len(merged) * n_granules, bytes_read, needed))
    return groupings


def floor(grouping: Grouping, rtt_s: float, bandwidth_bps: float, concurrency: int = 8) -> float:
    """Seconds at best: request rounds at ``concurrency`` in flight, plus the bytes at the measured bandwidth."""
    return -(-grouping.requests // concurrency) * rtt_s + grouping.bytes_read / bandwidth_bps


def analyse_dataset(
    dataset: Dataset, payload: dict[str, Any], window: str, probes: dict[str, Any] | None, max_bytes: int
) -> list[dict[str, Any]]:
    """The grouping table for every headline case, from the first pinned
    granule's chunk index; every granule is assumed to share its layout. Network."""
    import h5py
    from obspec_utils.readers import BlockStoreReader

    spec = dataset.store_spec(dataset.auth())
    path = store_path(spec, payload["granules"][0]["source_url"])
    rows = []
    with BlockStoreReader(build_store(spec), path, block_size=2**20) as reader, h5py.File(reader, "r") as handle:
        for case in headline_cases(window):
            n_observations = len(dataset.observations(case, len(payload["granules"])))
            ranges = needed_ranges(handle, dataset.variables, dataset.selection(case, payload["grid"]))
            for grouping in analyse(ranges, n_observations, max_bytes=max_bytes):
                seconds = None
                if probes:
                    seconds = floor(grouping, probes["rtt_ms_median"] / 1000, probes["bandwidth_mb_s"] * 1e6)
                rows.append(
                    {
                        "case": str(case),
                        "granules": n_observations,
                        "max_gap_bytes": grouping.max_gap,
                        "requests": grouping.requests,
                        "bytes_read": grouping.bytes_read,
                        "bytes_needed": grouping.bytes_needed,
                        "over_read": grouping.over_read,
                        "floor_seconds": seconds,
                    }
                )
    return rows


def render_table(rows: list[dict[str, Any]]) -> str:
    """:func:`analyse_dataset`'s rows as a Markdown table."""
    lines = [
        "| Case | Granules | Grouping | Requests | MB read | Over-read | Floor s |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["max_gap_bytes"] == ONE_REQUEST_PER_CHUNK:
            label = "one request per chunk"
        else:
            label = f"gap ≤ {row['max_gap_bytes'] // 1024:,} KiB"
        seconds = f"{row['floor_seconds']:.2f}" if row["floor_seconds"] is not None else "—"
        megabytes = row["bytes_read"] / 1e6
        lines.append(
            f"| `{row['case']}` | {row['granules']} | {label} | {row['requests']:,} | {megabytes:.2f} "
            f"| {row['over_read']:.0%} | {seconds} |"
        )
    return "\n".join(lines)
