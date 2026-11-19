"""Reading one open HDF5 granule with h5py: the code the file-reading arms share."""

from __future__ import annotations

from typing import Any

import h5py
import numpy as np

from benchmarks.trade_study.questions import Selection


def is_paged(handle: h5py.File) -> bool:
    """True when the file uses the paged file-space strategy, the only layout
    HDF5's page buffer applies to."""
    strategy = handle.id.get_create_plist().get_file_space_strategy()[0]
    return strategy == h5py.h5f.FSPACE_STRATEGY_PAGE


def decode_cf(dataset: h5py.Dataset, raw: Any) -> np.ndarray:
    """Stored values → physical float64 values: fill becomes NaN, then scale and offset."""
    raw_values = np.asarray(raw)
    fill = dataset.attrs.get("_FillValue")
    is_fill = np.zeros(raw_values.shape, dtype=bool)
    if fill is not None:
        is_fill = raw_values == np.asarray(fill).reshape(-1)[0]
    values = raw_values.astype(np.float64)
    if "scale_factor" in dataset.attrs:
        values *= float(np.asarray(dataset.attrs["scale_factor"]).reshape(-1)[0])
    if "add_offset" in dataset.attrs:
        values += float(np.asarray(dataset.attrs["add_offset"]).reshape(-1)[0])
    return np.where(is_fill, np.nan, values)


def chunk_keys(dataset: h5py.Dataset, selection: Selection) -> list[tuple[int, int]]:
    """(chunk row, chunk column) of every chunk the selection touches."""
    chunk_rows, chunk_cols = dataset.chunks[-2], dataset.chunks[-1]
    if selection.kind == "points":
        pairs = zip(selection.y, selection.x, strict=True)
        return sorted({(int(y) // chunk_rows, int(x) // chunk_cols) for y, x in pairs})
    rows = range(selection.y.start // chunk_rows, (selection.y.stop - 1) // chunk_rows + 1)
    cols = range(selection.x.start // chunk_cols, (selection.x.stop - 1) // chunk_cols + 1)
    return [(row, col) for row in rows for col in cols]


def locate_chunks(handle: h5py.File, variables: tuple[str, ...], selection: Selection) -> int:
    """Look up the byte range of every chunk the selection needs: HDF5's own
    chunk identification (its chunk index), with no data read. Returns the chunk count."""
    found = 0
    for path in variables:
        dataset = handle[path]
        leading = (0,) * (dataset.ndim - 2)
        chunk_rows, chunk_cols = dataset.chunks[-2], dataset.chunks[-1]
        for row, col in chunk_keys(dataset, selection):
            dataset.id.get_chunk_info_by_coord((*leading, row * chunk_rows, col * chunk_cols))
            found += 1
    return found


def _read_points(dataset: h5py.Dataset, selection: Selection) -> np.ndarray:
    """One read per touched chunk, then pick the points inside it."""
    leading = (0,) * (dataset.ndim - 2)
    ys, xs = np.asarray(selection.y), np.asarray(selection.x)
    chunk_rows, chunk_cols = dataset.chunks[-2], dataset.chunks[-1]
    values = np.full(len(ys), np.nan)
    for row, col in chunk_keys(dataset, selection):
        y_block = slice(row * chunk_rows, (row + 1) * chunk_rows)
        x_block = slice(col * chunk_cols, (col + 1) * chunk_cols)
        block = decode_cf(dataset, dataset[(*leading, y_block, x_block)])
        inside = (ys // chunk_rows == row) & (xs // chunk_cols == col)
        values[inside] = block[ys[inside] - y_block.start, xs[inside] - x_block.start]
    return values


def read_selection(
    handle: h5py.File, spec: dict[str, Any], selection: Selection
) -> tuple[np.datetime64, list[np.ndarray]]:
    """The observation time and each variable's decoded values for one open granule.
    ``spec`` is :meth:`benchmarks.trade_study.datasets.base.Dataset.h5_spec`."""
    values = []
    for path in spec["variables"]:
        dataset = handle[path]
        if selection.kind == "window":
            leading = (0,) * (dataset.ndim - 2)
            values.append(decode_cf(dataset, dataset[(*leading, selection.y, selection.x)]))
        else:
            values.append(_read_points(dataset, selection))
    seconds = float(np.asarray(handle[spec["time_path"]]).reshape(-1)[0])
    when = np.datetime64(spec["time_epoch"], "ns") + np.timedelta64(round(seconds * 1_000_000_000), "ns")
    return when, values
