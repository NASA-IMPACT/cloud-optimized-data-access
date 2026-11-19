"""Is a DMR++ sidecar still right for the file beside it?

A sidecar lists a byte offset and length for every chunk. This command reads
the same list a second way, from the file's own metadata, and compares the
two chunk by chunk. Only metadata is read; no science data moves. HDF5 and
netCDF-4 files are indexed by VirtualiZarr's HDF parser, netCDF classic by its
netCDF3 parser, and HDF4 by its HDF4 parser (which decodes the file with
kerchunk).

    uv run --extra dmrpp python -m dmrpp.verify stale_granules.csv --out verdicts.csv            # in us-west-2, direct S3
    uv run --extra dmrpp python -m dmrpp.verify stale_granules.csv --transport https             # anywhere, slower
    uv run --extra dmrpp python -m dmrpp.verify https://archive.podaac.earthdata.nasa.gov/.../file.nc --transport https

The CSV is either inventory's list of stale files: the sampler's
``stale_granules.csv`` or the listing script's ``stale.csv``. ``--collection``
keeps only the named collections. A bare URL is a data file's; its sidecar
is ``<url>.dmrpp``.

    uv run --extra dmrpp python -m dmrpp.verify stale.csv --collection L3S_LEO_PM-STAR-v2.81 --min-gap-days 1 --per-collection 10

Needs an Earthdata login: ``EARTHDATA_TOKEN``, or ``EARTHDATA_USERNAME`` and
``EARTHDATA_PASSWORD``, or a netrc entry.

Verdicts, one row per granule:

- ``match``: every chunk both sides list has the same offset and length, and
  every shared variable has the same shape, data type, chunk shape and filters.
- ``MISMATCH``: at least one chunk differs. The sidecar points at the wrong bytes.
- ``METADATA_MISMATCH``: the bytes line up but the sidecar describes them
  differently (shape, data type, chunk shape or the set of filters).
- ``SIDECAR_PAST_END``: the sidecar references bytes beyond the end of the file.
- ``nothing_to_compare``: no chunked variable appears on both sides.
- ``unsupported_format``: no file-side parser here can index the file. HDF5,
  netCDF-4, netCDF classic and HDF4 files are all judged.
- ``error <stage>: ...``: a failure, with the step it happened in (reading the
  sidecar, or reading the file's own index). ``--traceback`` prints it in full.

Variables only one side lists are counted, not failed: a sidecar may inline
small variables and omit chunks that are all fill value.

HDF4 needs three allowances. kerchunk puts every variable at the root, so a
sidecar's ``/Grid/Data_Fields/ET`` is matched to the file's ``/ET`` by its last
name when that name is unique on both sides. Variables whose chunks the
sidecar keeps outside the data file (``href``, e.g. the latitude and longitude
Hyrax computes for an HDF-EOS2 grid) are left out and counted in
``sidecar_external_variables``. And the sidecar's ``byteOrder`` is read from
the XML, because VirtualiZarr 2.8.0's DMR++ parser ignores it and would call a
big-endian file's sidecar wrong. The last two apply to every format.

kerchunk's HDF4 decoder is patched where it was found wrong on real granules
(see ``hdf4.patch_hdf4_decoding``). Known limits remain: it reads only datasets
inside the file's last root Vgroup, so files that keep datasets outside one
(CALIPSO) come back ``nothing_to_compare``; and data stored as several
compressed blocks (``<dmrpp:block>`` in the sidecar, AIRS Level 3) is read by
neither parser, so those variables are counted in ``only_in_file``. Check
``variables_compared`` against the two ``only_in_*`` columns before reading an
HDF4 ``match`` as covering the whole file. The value check reads HDF4 blocks
through the same patched index, so it is independent of the sidecar but not
of kerchunk.

``--values`` then asks the question a user cares about: does the service
return the right numbers? For each chosen granule it requests a block from
the middle of up to ``--value-variables`` variables from Hyrax (OPeNDAP) and
compares it with a direct read of the same block from the file. A block is
at most ``--value-edge`` elements per dimension (256 by default), so the
check costs the same for a small variable and a global grid. Variables the
sidecar describes differently from the file are checked first. The ``hyrax_values`` column is one of:

- ``identical``: every variable checked equals the direct read.
- ``DIFFERENT``: Hyrax answered with other numbers and no error. This is
  silent wrong data.
- ``hyrax error: ...``: the request failed, with Hyrax's message. Loud.
- ``skipped: ...``: nothing could be compared, with the reason.

``--values mismatch`` (the default when the flag is given bare) checks only
granules that did not come back ``match``; ``--values all`` checks every
granule, which is how a random sample of healthy files becomes a control.

The exit code is 2 if any granule is ``DIFFERENT``, else 1 if any sidecar
is wrong, else 0.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from collections import defaultdict

from dmrpp.verify.access import Access
from dmrpp.verify.granule import verify

FIELDS = [
    "verdict",
    "provider",
    "short_name",
    "granule_ur",
    "file_format",
    "data_newer_by_days",
    "variables_compared",
    "chunks_compared",
    "chunks_mismatched",
    "variables_mismatched",
    "only_in_sidecar",
    "only_in_file",
    "file_size",
    "sidecar_max_byte",
    "first_mismatch",
    "metadata_differences",
    "filter_order_differs",
    "int8_declared_int16",
    "file_int8_variables",
    "sidecar_version",
    "sidecar_cf_markers",
    "sidecar_hdf5_global",
    "sidecar_int8_variables",
    "sidecar_flattened",
    "sidecar_external_variables",
    "sidecar_groups_unread",
    "file_variables_unsupported",
    "hyrax_values",
    "hyrax_detail",
    "data_url",
]


def pick(
    rows: list[dict[str, str]], per_collection: int, min_gap_days: float | None, limit: int | None, seed: int = 0
) -> list[dict[str, str]]:
    """Up to ``per_collection`` granules from each collection, largest gaps first across collections."""
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if min_gap_days is None or float(row.get("data_newer_by_s") or 0) >= min_gap_days * 86400:
            by[row["concept_id"]].append(row)
    rng = random.Random(seed)
    chosen = []
    for group in sorted(by.values(), key=lambda g: -max(float(r.get("data_newer_by_s") or 0) for r in g)):
        chosen += rng.sample(group, min(per_collection, len(group)))
    return chosen[:limit] if limit else chosen


def normalise(row: dict[str, str]) -> dict[str, str]:
    """Accept the listing script's stale.csv too: its rows carry ``bucket`` and ``path`` instead of ``data_url``."""
    if not row.get("data_url") and row.get("bucket") and row.get("path"):
        row = {**row, "data_url": f"s3://{row['bucket']}/{row['path']}"}
    if not row.get("provider"):
        row = {**row, "provider": (row.get("concept_id") or "").rpartition("-")[2]}
    return row


def to_redo(verdict_files: list[str]) -> set[str]:
    """The data URLs that earlier runs gave anything but ``match`` or ``skipped``:
    errors, mismatches and granules with nothing to compare. Those are the rows
    worth a second verdict after the verifier has been fixed."""
    urls = set()
    for path in verdict_files:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row["verdict"] != "match" and not row["verdict"].startswith("skipped"):
                    urls.add(row["data_url"])
    return urls


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m dmrpp.verify", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("inputs", nargs="+", help="stale_granules.csv, or data file URLs")
    p.add_argument(
        "--transport",
        choices=("s3", "https"),
        default="s3",
        help="s3: direct, from us-west-2 (default); https: the DAAC endpoint, from anywhere",
    )
    p.add_argument("--per-collection", type=int, default=2, help="granules to check per collection (CSV input)")
    p.add_argument(
        "--min-gap-days",
        type=float,
        default=None,
        help="only granules whose data file is at least this much newer than its sidecar (default: no filter)",
    )
    p.add_argument("--provider", nargs="*", help="only these providers from the CSV, e.g. POCLOUD")
    p.add_argument("--collection", nargs="*", help="only these collections (short names or concept ids) from the CSV")
    p.add_argument("--limit", type=int, help="stop after this many granules")
    p.add_argument(
        "--values",
        nargs="?",
        const="mismatch",
        default="none",
        choices=("none", "mismatch", "all"),
        help="also compare what Hyrax serves with a direct read: for granules that did not match (default when given), or for all",
    )
    p.add_argument("--value-variables", type=int, default=3, help="variables to compare per granule")
    p.add_argument(
        "--value-edge",
        type=int,
        default=256,
        help="compare a block of at most this many elements per dimension, from the middle of each variable",
    )
    p.add_argument(
        "--max-gb",
        type=float,
        default=20,
        help="skip files larger than this; reading the index of a very large file can take hours (0: no limit)",
    )
    p.add_argument(
        "--start", type=int, default=1, help="begin at this granule number (as printed), to continue an interrupted run"
    )
    p.add_argument(
        "--redo",
        nargs="+",
        metavar="VERDICTS.csv",
        help="only granules that these earlier verdict files did not call match (errors, mismatches, nothing to compare); "
        "write the new verdicts to a file of their own with --out",
    )
    p.add_argument("--traceback", action="store_true", help="print the full traceback when a granule errors")
    p.add_argument("--out", help="write verdicts to this CSV as well as the screen")
    args = p.parse_args(argv)

    redo = to_redo(args.redo) if args.redo else None
    rows: list[dict[str, str]] = []
    for item in args.inputs:
        if item.endswith(".csv"):
            with open(item, newline="") as f:
                found = [normalise(r) for r in csv.DictReader(f)]
            if args.provider:
                found = [r for r in found if r.get("provider") in args.provider]
            if args.collection:
                found = [
                    r for r in found if r.get("short_name") in args.collection or r.get("concept_id") in args.collection
                ]
            if redo is not None:
                found = [r for r in found if r["data_url"] in redo]
            rows += pick(found, args.per_collection, args.min_gap_days, None)
        else:
            rows.append({"data_url": item.strip(), "provider": os.environ.get("CMR_PROVIDER", "")})
    rows = rows[: args.limit] if args.limit else rows
    print(f"{len(rows)} granules to verify over {args.transport}", file=sys.stderr)
    access = Access(args.transport)
    sink = open(args.out, "w", newline="") if args.out else None  # noqa: SIM115 -- closed below; rows are flushed as they arrive
    writer = csv.DictWriter(sink, fieldnames=FIELDS, extrasaction="ignore") if sink else None
    if writer:
        writer.writeheader()
    tally: dict[str, int] = defaultdict(int)
    hyrax: dict[str, int] = defaultdict(int)
    cross: dict[tuple[str, str], int] = defaultdict(int)
    for index, row in enumerate(rows, 1):
        if index < args.start:
            continue
        result = verify(
            row, access, args.traceback, args.values, args.value_variables, args.value_edge, args.max_gb or None
        )
        tally["error" if result["verdict"].startswith("error") else result["verdict"]] += 1
        detail = (
            f"{result.get('chunks_mismatched')} of {result.get('chunks_compared')} chunks differ"
            if "chunks_compared" in result
            else ""
        )
        print(
            f"[{index}/{len(rows)}] {result['verdict']:<18} {result.get('short_name') or ''} {row['data_url'].rsplit('/', 1)[-1]} {detail}",
            flush=True,
        )
        if "sidecar_cf_markers" in result and result.get("file_int8_variables") is not None:
            if not result.get("variables_compared"):
                wrong = "not compared"
            elif result.get("int8_declared_int16"):
                wrong = "8-bit declared as Int16"
            else:
                wrong = "8-bit declared correctly" if result["file_int8_variables"] else "no signed 8-bit variables"
            cross[
                (
                    "older sidecar (has fullnamepath)"
                    if result["sidecar_cf_markers"]
                    else "newer sidecar (no fullnamepath)",
                    wrong,
                )
            ] += 1
        for note in (result.get("first_mismatch"), result.get("metadata_differences")):
            if note:
                print(f"      {note}", flush=True)
        if result.get("hyrax_values"):
            hyrax[result["hyrax_values"].split(":")[0]] += 1
            print(
                f"      Hyrax values: {result['hyrax_values']}"
                + (f" ({result['hyrax_detail']})" if result.get("hyrax_detail") else ""),
                flush=True,
            )
        if writer and sink:
            writer.writerow(result)
            sink.flush()
    if sink:
        sink.close()
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
    if cross:
        print("\nSidecar kind against how it declares the file's signed 8-bit variables:")
        for (kind, outcome), n in sorted(cross.items()):
            print(f"  {kind:36s} {outcome:28s} {n}")
    if hyrax:
        print("Hyrax values: " + ", ".join(f"{k}: {v}" for k, v in sorted(hyrax.items())))
    if hyrax.get("DIFFERENT"):
        return 2
    return 1 if tally.get("MISMATCH") or tally.get("SIDECAR_PAST_END") or tally.get("METADATA_MISMATCH") else 0
