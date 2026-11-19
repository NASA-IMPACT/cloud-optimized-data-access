"""One verdict per granule from several runs of the verifier.

A granule verified more than once (a first run, then ``--redo`` after a fix,
then a ``--values`` check) has a row in each run's file. This keeps the row
from the last file named, adds which file it came from, and writes one CSV:

    uv run python -m dmrpp.verify.merge results/dmrpp/verify/verdicts.csv first.csv redo.csv values.csv

Files are read in the order given, so name them oldest first. The output may
itself be the first input when later runs are folded in.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from dmrpp.verify.cli import FIELDS

RUN = "run"  # the file a row's verdict came from


def merge(paths: list[Path]) -> list[dict[str, str]]:
    """The last row for each ``data_url`` across ``paths``, sorted by provider, collection and URL."""
    latest: dict[str, dict[str, str]] = {}
    for path in paths:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                latest[row["data_url"]] = {**row, RUN: row.get(RUN) or path.stem}
    return sorted(latest.values(), key=lambda r: (r.get("provider") or "", r.get("short_name") or "", r["data_url"]))


def kind(verdict: str) -> str:
    """A verdict without its detail: every error is "error", every skip "skipped"."""
    return verdict.split(":")[0].split(" ")[0] if verdict.startswith(("error", "skipped")) else verdict


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out", type=Path, help="the merged CSV to write")
    p.add_argument("inputs", type=Path, nargs="+", help="verdict files, oldest first")
    args = p.parse_args(argv)
    rows = merge(args.inputs)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[*FIELDS, RUN], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tally = Counter((row.get("provider") or "", kind(row["verdict"])) for row in rows)
    for (provider, verdict), n in sorted(tally.items()):
        print(f"{provider:11s} {verdict:20s} {n:5d}")
    print(f"{len(rows):,} granules in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
