"""Proof of concept: find every data file newer than its DMR++ sidecar by listing S3.

One S3 listing request returns up to 1,000 objects with their sizes and
Last-Modified times. Listing a collection's prefix and pairing each
``<file>`` with ``<file>.dmrpp`` gives a complete answer for that prefix,
with no sampling and no per-file requests.

Run in us-west-2 with an Earthdata login (EARTHDATA_TOKEN, username and
password, or netrc):

    uv run --extra dmrpp python -m dmrpp.list_collection C2129195053-POCLOUD --out stale.csv
    uv run --extra dmrpp python -m dmrpp.list_collection --prefix s3://podaac-ops-cumulus-protected/SOME_COLLECTION/ --provider POCLOUD
    uv run --extra dmrpp python -m dmrpp.list_collection --collections collections.csv --only-flagged --summary summary.csv --out stale.csv

Given a collection concept id, the script asks CMR for one granule, takes
its ``s3://`` link, and lists that file's directory. Layouts differ by DAAC:
directories that look like dates or numbers (``2026.11.13``, ``2024``,
``045``) are climbed automatically so the listing covers the collection, and
``--up N`` climbs further. The prefix is printed before anything is listed;
check it is what you meant.

``--collections`` takes the sampled inventory's ``collections.csv`` and
lists each collection that has sidecars (``--only-flagged``: only those the
sampler found stale), one summary row per collection. A collection that
fails (no listing permission, no s3 link) is a row with its error; the run
goes on.

Reading the result: a bulk rewrite touches files and sidecars in arbitrary
order within a day or so, which makes about half the files "newer" without
any byte changing. ``max_gap_days`` and ``stale_over_1_day`` are the columns
that separate that from a sidecar left behind for months.

What this PoC is for: finding out whether the DAAC's temporary credentials
allow listing at all (an AccessDenied here is the answer), and how long a
whole collection takes. ``--anonymous`` lists a public bucket instead, to
try the script without credentials.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

from dmrpp.common import CMR, GAPS, ProviderCredentials, credentials_endpoint, gap_label, is_data_file, s3_store
from dmrpp.common import SIDECAR_SUFFIX as SIDECAR

DATE_LIKE = re.compile(r"^(\d{1,4}([.\-_]?\d{1,3}){0,2}|[A-Za-z]?\d{3,8})$")


def climb(parts: list[str], up: int) -> list[str]:
    """Drop trailing date-like directories, then ``up`` more; never reach the bucket root.
    ``parts`` is an s3 URL split on "/" without the file name: ["s3:", "", bucket, dir, ...]."""
    floor = min(4, len(parts))
    while len(parts) > floor and DATE_LIKE.match(parts[-1]):
        parts = parts[:-1]
    return parts[: max(floor, len(parts) - up)]


def prefix_from_cmr(concept_id: str, up: int) -> str:
    """s3://bucket/dir/ of one of the collection's granules, with date directories climbed."""
    query = urllib.parse.urlencode({"collection_concept_id": concept_id, "page_size": 1, "sort_key": "-start_date"})
    with urllib.request.urlopen(f"{CMR}?{query}", timeout=30) as response:
        items = json.load(response).get("items", [])
    for item in items:
        for related in item["umm"].get("RelatedUrls", []):
            url = related.get("URL", "")
            if url.startswith("s3://") and is_data_file(url):
                return "/".join(climb(url.split("/")[:-1], up)) + "/"
    raise SystemExit(f"CMR lists no s3:// HDF or netCDF link for {concept_id}")


def pair(objects: list[dict[str, Any]], tolerance_s: float) -> dict[str, Any]:
    """Match each data file with ``<file>.dmrpp`` and find the files written after their sidecar.

    ``objects`` are listing entries with ``path``, ``last_modified`` and ``size``.
    """
    by_path = {o["path"]: o for o in objects}
    data = [o for o in objects if is_data_file(o["path"])]
    stale, with_sidecar = [], 0
    for o in data:
        sidecar = by_path.get(o["path"] + SIDECAR)
        if sidecar is None:
            continue
        with_sidecar += 1
        gap = (o["last_modified"] - sidecar["last_modified"]).total_seconds()
        if gap > tolerance_s:
            stale.append(
                {
                    "path": o["path"],
                    "data_last_modified": o["last_modified"].isoformat(),
                    "dmrpp_last_modified": sidecar["last_modified"].isoformat(),
                    "data_newer_by_s": gap,
                    "data_size": o["size"],
                    "dmrpp_size": sidecar["size"],
                }
            )
    sidecars = [p for p in by_path if p.endswith(SIDECAR)]
    return {
        "objects": len(objects),
        "data_files": len(data),
        "with_sidecar": with_sidecar,
        "without_sidecar": len(data) - with_sidecar,
        "orphan_sidecars": sum(p[: -len(SIDECAR)] not in by_path for p in sidecars),
        "stale": stale,
    }


def list_prefix(store: Any, prefix: str, max_objects: int) -> tuple[list[dict[str, Any]], int, bool]:
    """(objects, listing requests, whether the listing was cut off at ``max_objects``)."""
    import obstore

    objects: list[dict[str, Any]] = []
    requests = 0
    for page in obstore.list(store, prefix=prefix or None, chunk_size=1000):
        requests += 1
        objects += [{"path": str(m["path"]), "last_modified": m["last_modified"], "size": m["size"]} for m in page]
        if len(objects) >= max_objects:
            return objects, requests, True
    return objects, requests, False


class Stores:
    """S3 stores per bucket; DAAC credentials are fetched per provider and renewed before their hour is up."""

    def __init__(self, region: str, anonymous: bool) -> None:
        self.region, self.anonymous = region, anonymous
        self._credentials = ProviderCredentials()

    def get(self, bucket: str, provider: str) -> Any:
        if self.anonymous:
            return s3_store(bucket, self.region, None)
        return s3_store(bucket, self.region, self._credentials.get(provider, credentials_endpoint(bucket)))


def summary_row(result: dict[str, Any], requests: int, seconds: float, truncated: bool) -> dict[str, Any]:
    stale = result["stale"]
    gaps = [row["data_newer_by_s"] for row in stale]
    months = Counter(row["data_last_modified"][:7] for row in stale).most_common(1)
    return {
        "objects": result["objects"],
        "data_files": result["data_files"],
        "with_sidecar": result["with_sidecar"],
        "without_sidecar": result["without_sidecar"],
        "orphan_sidecars": result["orphan_sidecars"],
        "stale": len(stale),
        "stale_over_1_day": sum(g >= 86400 for g in gaps),
        "stale_over_30_days": sum(g >= 30 * 86400 for g in gaps),
        "max_gap_days": round(max(gaps, default=0) / 86400, 2),
        "busiest_month": months[0][0] if months else "",
        "stale_in_other_months": len(stale) - (months[0][1] if months else 0),
        "listing_requests": requests,
        "seconds": round(seconds, 1),
        "truncated": truncated,
    }


def survey(
    stores: Stores, prefix: str, provider: str, args: argparse.Namespace
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """List one prefix. Returns (summary row, stale files, bucket)."""
    parsed = urllib.parse.urlparse(prefix)
    bucket, key_prefix = parsed.netloc, parsed.path.lstrip("/")
    print(f"listing s3://{bucket}/{key_prefix}", file=sys.stderr)
    start = time.perf_counter()
    objects, requests, truncated = list_prefix(stores.get(bucket, provider), key_prefix, args.max_objects)
    result = pair(objects, args.tolerance_s)
    return summary_row(result, requests, time.perf_counter() - start, truncated), result["stale"], bucket


def describe(row: dict[str, Any], stale: list[dict[str, Any]], args: argparse.Namespace) -> None:
    print(
        f"{row['objects']:,} objects in {row['listing_requests']:,} listing requests, {row['seconds']} s"
        + (f" (stopped at --max-objects {args.max_objects:,})" if row["truncated"] else "")
    )
    print(
        f"{row['data_files']:,} data files: {row['with_sidecar']:,} with a sidecar, {row['without_sidecar']:,} without; {row['orphan_sidecars']:,} sidecars with no data file"
    )
    print(f"{len(stale):,} data files newer than their sidecar by more than {args.tolerance_s:.0f} s")
    if stale:
        buckets: Counter[str] = Counter()
        for item in stale:
            buckets[gap_label(item["data_newer_by_s"])] += 1
        for label, *_ in GAPS:
            if buckets[label]:
                print(f"  {label}: {buckets[label]:,}")
        months = Counter(item["data_last_modified"][:7] for item in stale).most_common(5)
        print("  data files last modified in: " + ", ".join(f"{month} ({n:,})" for month, n in months))


def targets(args: argparse.Namespace) -> list[dict[str, str]]:
    """The collections to list: from --collections (those with sidecars, flagged ones first), else the one given."""
    if not args.collections:
        return [
            {
                "concept_id": args.concept_id or "",
                "short_name": "",
                "provider": args.provider or (args.concept_id or "").rpartition("-")[2],
            }
        ]
    with open(args.collections, newline="") as f:
        rows = [r for r in csv.DictReader(f) if int(r.get("with_sidecar") or 0) > 0]
    if args.only_flagged:
        rows = [r for r in rows if int(r.get("stale") or 0) > 0]
    if args.providers:
        rows = [r for r in rows if r["provider"] in args.providers]
    rows.sort(key=lambda r: -float(r.get("max_data_newer_by_days") or 0))
    return rows[: args.limit] if args.limit else rows


STALE_FIELDS = [
    "concept_id",
    "short_name",
    "bucket",
    "path",
    "data_last_modified",
    "dmrpp_last_modified",
    "data_newer_by_s",
    "data_size",
    "dmrpp_size",
]
SUMMARY_FIELDS = [
    "concept_id",
    "short_name",
    "provider",
    "prefix",
    "error",
    "objects",
    "data_files",
    "with_sidecar",
    "without_sidecar",
    "orphan_sidecars",
    "stale",
    "stale_over_1_day",
    "stale_over_30_days",
    "max_gap_days",
    "busiest_month",
    "stale_in_other_months",
    "listing_requests",
    "seconds",
    "truncated",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("concept_id", nargs="?", help="a collection concept id, e.g. C2129195053-POCLOUD")
    p.add_argument("--prefix", help="list this s3://bucket/prefix/ instead of asking CMR")
    p.add_argument("--provider", help="CMR provider whose S3 credentials to fetch (default: the concept id's suffix)")
    p.add_argument(
        "--collections", help="the sampled inventory's collections.csv: list every collection in it that has sidecars"
    )
    p.add_argument(
        "--only-flagged", action="store_true", help="with --collections: only collections the sampler found stale"
    )
    p.add_argument("--providers", nargs="*", help="with --collections: only these providers")
    p.add_argument("--limit", type=int, help="with --collections: only the first N, largest sampled gap first")
    p.add_argument(
        "--up", type=int, default=0, help="climb this many more directories after date-like ones are dropped"
    )
    p.add_argument(
        "--tolerance-s", type=float, default=60.0, help="a data file this much newer than its sidecar is stale"
    )
    p.add_argument("--max-objects", type=int, default=200_000, help="stop listing a prefix after this many objects")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--anonymous", action="store_true", help="unsigned requests, for a public bucket")
    p.add_argument("--out", help="write the stale files to this CSV")
    p.add_argument("--summary", help="write one row per collection to this CSV")
    args = p.parse_args(argv)
    if not (args.concept_id or args.prefix or args.collections):
        p.error("give a collection concept id, --prefix or --collections")
    if args.prefix and not (args.anonymous or args.provider or args.concept_id):
        p.error("--provider is needed with --prefix")

    stores = Stores(args.region, args.anonymous)
    todo = targets(args)
    out = open(args.out, "w", newline="") if args.out else None  # noqa: SIM115 -- closed below; rows are flushed per collection
    summary = open(args.summary, "w", newline="") if args.summary else None  # noqa: SIM115
    stale_writer = csv.DictWriter(out, fieldnames=STALE_FIELDS) if out else None
    summary_writer = csv.DictWriter(summary, fieldnames=SUMMARY_FIELDS) if summary else None
    for writer in (stale_writer, summary_writer):
        if writer:
            writer.writeheader()
    failed = 0
    for index, target in enumerate(todo, 1):
        identity = {
            "concept_id": target["concept_id"],
            "short_name": target.get("short_name", ""),
            "provider": target["provider"],
        }
        if len(todo) > 1:
            print(f"\n[{index}/{len(todo)}] {identity['short_name'] or identity['concept_id']}")
        prefix = ""
        try:
            prefix = args.prefix or prefix_from_cmr(target["concept_id"], args.up)
            row, stale, bucket = survey(stores, prefix, target["provider"], args)
            describe(row, stale, args)
        except (Exception, SystemExit) as error:  # noqa: BLE001 -- one collection's failure is a row, not the end of the run
            failed += 1
            row, stale, bucket = {"error": " ".join(f"{type(error).__name__}: {error}".split())[:300]}, [], ""
            print(f"failed: {row['error']}")
            if len(todo) == 1:
                return 1
        if summary_writer and summary:
            summary_writer.writerow({**identity, "prefix": prefix, **row})
            summary.flush()
        if stale_writer and out:
            stale_writer.writerows(
                {"concept_id": identity["concept_id"], "short_name": identity["short_name"], "bucket": bucket, **item}
                for item in sorted(stale, key=lambda r: -r["data_newer_by_s"])
            )
            out.flush()
    for handle, name in ((out, args.out), (summary, args.summary)):
        if handle:
            handle.close()
            print(f"wrote {name}")
    if len(todo) > 1:
        print(f"{len(todo) - failed} of {len(todo)} collections listed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
