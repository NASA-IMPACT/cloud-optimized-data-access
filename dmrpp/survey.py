"""Sampled inventory: data files modified in AWS after their DMR++ sidecar was built.

A DMR++ sidecar records byte offsets into one version of a file. If the file
is replaced afterwards and the sidecar is not rebuilt, every reader of the
sidecar (Hyrax, a virtual store built from it) reads the wrong bytes.

For each cloud-hosted collection in an HDF-family format this script:

1. asks CMR for a sample of granules: the oldest, the newest, and the most
   recently revised (the ones most likely to have been replaced);
2. finds each granule's HTTPS data URL and its sidecar (the ``.dmrpp`` URL
   CMR advertises, else ``<data url>.dmrpp``);
3. reads one byte of each through the DAAC's distribution endpoint to get
   the object's S3 ``Last-Modified`` and size;
4. calls a granule **stale** when the data file's Last-Modified is more than
   ``--tolerance-s`` after the sidecar's, and then reads the sidecar's own
   ``build_dmrpp_metadata/created`` attribute to confirm.

It is a sample: it finds collections where stale sidecars exist, not every
stale file. An exhaustive inventory needs S3 listings from inside us-west-2.

Needs ``EARTHDATA_TOKEN`` in the environment. Requests are one-byte ranged
GETs, at most ``--per-host`` in flight per DAAC host.

    EARTHDATA_TOKEN=... uv run --extra dmrpp python -m dmrpp.survey run --inventory benchmarks/cost/data/cmr_cloud_inventory.csv
    uv run python -m dmrpp.survey report
    uv run python -m dmrpp.survey sample
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from dmrpp.common import CMR, GAPS, is_data_file

RESULTS = "results/dmrpp/survey"
HDF_FAMILIES = ("netCDF", "HDF5", "HDF4")
SAMPLE = (("oldest", "start_date", 7), ("newest", "-start_date", 7), ("revised", "-revision_date", 6))
VERSION = re.compile(rb'dmrpp:version="([^"]+)"')
SIDECAR_PREFIX_BYTES = 512  # enough for the XML declaration and the Dataset element's attributes
MAGIC = {
    b"\x89HDF": "HDF5",
    b"CDF\x01": "netCDF classic",
    b"CDF\x02": "netCDF 64-bit offset",
    b"CDF\x05": "netCDF CDF-5",
    b"\x0e\x03\x13\x01": "HDF4",
}
CREATED = re.compile(rb'name="created"[^>]*>\s*<Value>([^<]+)</Value>')
SIDECAR_HEAD_BYTES = 512 * 2**10
USER_AGENT = "dmrpp-staleness-inventory/0.1"


def data_and_sidecar(umm: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """(HTTPS data URL, sidecar URL, whether CMR advertises the sidecar) from a granule's UMM-G."""
    data = sidecar = None
    for related in umm.get("RelatedUrls", []):
        url = related.get("URL", "")
        if not url.startswith("https://"):
            continue
        if url.endswith(".dmrpp"):
            sidecar = sidecar or url
        elif related.get("Type") == "GET DATA" and is_data_file(url):
            data = data or url
    if data is None:
        return None, None, False
    return data, sidecar or data + ".dmrpp", sidecar is not None


def is_sidecar(prefix: bytes) -> bool:
    """True when the bytes start a DMR++ document rather than, say, an HTML error page."""
    return prefix.lstrip().startswith(b"<?xml") and b"<Dataset" in prefix


def file_format(prefix: bytes) -> str | None:
    return next((name for magic, name in MAGIC.items() if prefix.startswith(magic)), None)


def classify(data: dict[str, Any], sidecar: dict[str, Any], tolerance_s: float) -> tuple[str, float | None]:
    """("fresh" | "stale" | "no_dmrpp" | "unreadable", seconds the data file is newer than its sidecar)."""
    if sidecar["status"] == 404:
        return "no_dmrpp", None
    if data.get("last_modified") is None or sidecar.get("last_modified") is None:
        return "unreadable", None
    delta = (
        datetime.fromisoformat(data["last_modified"]) - datetime.fromisoformat(sidecar["last_modified"])
    ).total_seconds()
    return ("stale" if delta > tolerance_s else "fresh"), delta


class Prober:
    def __init__(self, session: Any, token: str, per_host: int) -> None:
        self.session, self.token, self.per_host = session, token, per_host
        self.gates: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(per_host))
        self.cmr_gate = asyncio.Semaphore(4)

    async def cmr(self, concept_id: str, sort_key: str, page_size: int) -> list[dict[str, Any]]:
        params = {"collection_concept_id": concept_id, "sort_key": sort_key, "page_size": str(page_size)}
        async with self.cmr_gate:
            for attempt in range(3):
                try:
                    async with self.session.get(CMR, params=params, headers={"User-Agent": USER_AGENT}) as response:
                        if response.status == 200:
                            return (await response.json()).get("items", [])
                        if response.status < 500 and response.status != 429:
                            return []
                except Exception:  # noqa: BLE001, S110 -- retried below; a collection that never answers is recorded as such
                    pass
                await asyncio.sleep(2**attempt)
        return []

    async def stat(self, url: str, nbytes: int = 1) -> dict[str, Any]:
        """Status, Last-Modified and size of an object from a ranged GET of its
        first bytes. Redirects are followed by hand so the bearer token only
        ever goes to the host that was asked."""
        origin = urlparse(url).netloc
        out: dict[str, Any] = {"status": None, "last_modified": None, "size": None, "body": b""}
        async with self.gates[origin]:
            for attempt in range(3):
                try:
                    current = url
                    for _ in range(6):
                        headers = {"Range": f"bytes=0-{nbytes - 1}", "User-Agent": USER_AGENT}
                        if urlparse(current).netloc == origin:
                            headers["Authorization"] = f"Bearer {self.token}"
                        async with self.session.get(current, headers=headers, allow_redirects=False) as response:
                            if response.status in (301, 302, 303, 307, 308):
                                current = urljoin(current, response.headers["Location"])
                                if "urs.earthdata.nasa.gov" in urlparse(current).netloc:
                                    out["status"] = "login-redirect"  # the endpoint did not accept the bearer token
                                    return out
                                continue
                            out["status"] = response.status
                            if response.status in (200, 206):
                                modified = response.headers.get("Last-Modified")
                                out["last_modified"] = (
                                    parsedate_to_datetime(modified).astimezone(UTC).isoformat() if modified else None
                                )
                                total = response.headers.get("Content-Range", "").rpartition("/")[2]
                                out["size"] = int(total) if total.isdigit() else None
                                out["body"] = await response.read() if nbytes > 1 else b""
                            return out
                    out["status"] = "too-many-redirects"
                    return out
                except Exception as error:  # noqa: BLE001 -- a network failure is a recorded status, after retries
                    out["status"] = f"error: {type(error).__name__}"
                    await asyncio.sleep(2**attempt)
        return out

    async def granule(
        self, collection: dict[str, str], item: dict[str, Any], kind: str, tolerance_s: float
    ) -> dict[str, Any]:
        umm, meta = item["umm"], item["meta"]
        data_url, sidecar_url, advertised = data_and_sidecar(umm)
        row: dict[str, Any] = {
            "concept_id": collection["concept_id"],
            "short_name": collection["short_name"],
            "version": collection["version"],
            "provider": collection["provider"],
            "granule_ur": umm.get("GranuleUR"),
            "granule_concept_id": meta.get("concept-id"),
            "sample": kind,
            "cmr_revision_id": meta.get("revision-id"),
            "cmr_revision_date": meta.get("revision-date"),
            "data_url": data_url,
            "dmrpp_url": sidecar_url,
            "dmrpp_advertised_in_cmr": advertised,
        }
        if data_url is None:
            return {**row, "verdict": "no_https_hdf_link"}
        sidecar = await self.stat(sidecar_url, SIDECAR_PREFIX_BYTES)
        row.update(
            dmrpp_status=sidecar["status"], dmrpp_last_modified=sidecar["last_modified"], dmrpp_size=sidecar["size"]
        )
        if sidecar["status"] not in (200, 206):
            return {**row, "verdict": "no_dmrpp" if sidecar["status"] == 404 else "unreadable"}
        if not is_sidecar(sidecar["body"]):
            # some endpoints answer a missing object with 200 and an HTML "not found" page
            return {
                **row,
                "verdict": "no_dmrpp",
                "dmrpp_status": f"{sidecar['status']}, not a DMR++ document",
                "dmrpp_last_modified": None,
            }
        version = VERSION.search(sidecar["body"])
        row["dmrpp_version"] = version.group(1).decode() if version else None
        data = await self.stat(data_url, 8)
        row.update(
            data_status=data["status"],
            data_last_modified=data["last_modified"],
            data_size=data["size"],
            data_format=file_format(data["body"]),
        )
        if data["status"] in (200, 206) and row["data_format"] is None:
            return {**row, "verdict": "unreadable", "data_status": f"{data['status']}, not an HDF or netCDF file"}
        row["verdict"], row["data_newer_by_s"] = classify(data, sidecar, tolerance_s)
        if row["verdict"] == "stale":
            head = await self.stat(sidecar_url, SIDECAR_HEAD_BYTES)
            match = CREATED.search(head["body"])
            row["dmrpp_created"] = match.group(1).decode() if match else None
        return row

    async def collection(self, collection: dict[str, str], tolerance_s: float) -> list[dict[str, Any]]:
        items: dict[str, tuple[dict[str, Any], str]] = {}
        for kind, sort_key, n in SAMPLE:
            for item in await self.cmr(collection["concept_id"], sort_key, n):
                items.setdefault(item["meta"]["concept-id"], (item, kind))
        if not items:
            return [
                {
                    "concept_id": collection["concept_id"],
                    "short_name": collection["short_name"],
                    "version": collection["version"],
                    "provider": collection["provider"],
                    "verdict": "no_granules_from_cmr",
                }
            ]
        ordered = list(items.values())
        # one granule first: a collection with no sidecar there is not worth twenty requests
        first = await self.granule(collection, *ordered[0], tolerance_s)
        if first["verdict"] in ("no_dmrpp", "no_https_hdf_link"):
            second = await self.granule(collection, *ordered[-1], tolerance_s) if len(ordered) > 1 else first
            if second["verdict"] in ("no_dmrpp", "no_https_hdf_link"):
                return [first, second] if second is not first else [first]
            rest = ordered[1:-1]
            return [
                first,
                second,
                *await asyncio.gather(*(self.granule(collection, i, k, tolerance_s) for i, k in rest)),
            ]
        return [first, *await asyncio.gather(*(self.granule(collection, i, k, tolerance_s) for i, k in ordered[1:]))]


def candidates(inventory: Path, providers: set[str] | None) -> list[dict[str, str]]:
    with inventory.open() as f:
        rows = [
            r for r in csv.DictReader(f) if r["format_family"] in HDF_FAMILIES and int(float(r["granules"] or 0)) > 0
        ]
    return [r for r in rows if providers is None or r["provider"] in providers]


async def run(args: argparse.Namespace) -> int:
    import aiohttp

    token = os.environ.get("EARTHDATA_TOKEN")
    if not token:
        print("EARTHDATA_TOKEN is not set", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "granules.jsonl"
    done = (
        {json.loads(line)["concept_id"] for line in rows_path.read_text().splitlines()} if rows_path.exists() else set()
    )
    todo = [
        c
        for c in candidates(Path(args.inventory), set(args.providers) if args.providers else None)
        if c["concept_id"] not in done
    ]
    random.Random(0).shuffle(todo)  # spread the load across DAACs from the start
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} collections to sample ({len(done)} already done)", file=sys.stderr)
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=args.concurrency)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        prober = Prober(session, token, args.per_host)
        gate = asyncio.Semaphore(args.collections_in_flight)
        finished = 0

        async def one(collection: dict[str, str]) -> None:
            nonlocal finished
            async with gate:
                try:
                    rows = await prober.collection(collection, args.tolerance_s)
                except Exception as error:  # noqa: BLE001 -- one collection's failure must not end the inventory
                    rows = [
                        {
                            "concept_id": collection["concept_id"],
                            "short_name": collection["short_name"],
                            "version": collection["version"],
                            "provider": collection["provider"],
                            "verdict": f"error: {type(error).__name__}: {error}"[:300],
                        }
                    ]
            with rows_path.open("a") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            finished += 1
            if finished % 100 == 0:
                print(f"{finished}/{len(todo)} collections", file=sys.stderr, flush=True)

        await asyncio.gather(*(one(c) for c in todo))
    print(f"done: rows in {rows_path}", file=sys.stderr)
    return 0


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per collection: how many sampled granules had a sidecar, and how many were stale."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["concept_id"]].append(r)
    out = []
    for concept_id, group in by.items():
        verdicts = Counter(r["verdict"] if not str(r["verdict"]).startswith("error") else "error" for r in group)
        stale = [r for r in group if r["verdict"] == "stale"]
        with_sidecar = verdicts["fresh"] + verdicts["stale"]
        if with_sidecar:
            status = "stale sidecars found" if stale else "all sampled sidecars fresh"
        elif verdicts["unreadable"]:
            status = "could not read"
        elif verdicts["no_dmrpp"]:
            status = "no sidecars"
        else:
            status = next(iter(verdicts))
        out.append(
            {
                "concept_id": concept_id,
                "short_name": group[0]["short_name"],
                "version": group[0]["version"],
                "provider": group[0]["provider"],
                "status": status,
                "sampled": len(group),
                "with_sidecar": with_sidecar,
                "stale": len(stale),
                "fresh": verdicts["fresh"],
                "no_dmrpp": verdicts["no_dmrpp"],
                "unreadable": verdicts["unreadable"],
                "max_data_newer_by_days": round(max((r["data_newer_by_s"] for r in stale), default=0) / 86400, 2),
                "advertised_in_cmr": sum(bool(r.get("dmrpp_advertised_in_cmr")) for r in group),
                "unreadable_statuses": ", ".join(
                    sorted(
                        {
                            str(r.get("data_status") or r.get("dmrpp_status"))
                            for r in group
                            if r["verdict"] == "unreadable"
                        }
                    )
                ),
                "dmrpp_versions": ", ".join(sorted({r["dmrpp_version"] for r in group if r.get("dmrpp_version")})),
            }
        )
    return sorted(out, key=lambda r: (-r["stale"], r["provider"], r["short_name"]))


def load_rows(out: Path) -> list[dict[str, Any]]:
    """granules.jsonl, or the committed granules.jsonl.gz when the working file is absent."""
    import gzip

    plain = out / "granules.jsonl"
    if plain.exists():
        text = plain.read_text()
    else:
        with gzip.open(out / "granules.jsonl.gz", "rt") as f:
            text = f.read()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def report(args: argparse.Namespace) -> int:
    out = Path(args.out)
    rows = load_rows(out)
    collections = summarize(rows)
    with (out / "collections.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(collections[0]))
        writer.writeheader()
        writer.writerows(collections)
    stale_rows = [r for r in rows if r["verdict"] == "stale"]
    with (out / "stale_granules.csv").open("w", newline="") as f:
        fields = [
            "provider",
            "short_name",
            "version",
            "concept_id",
            "granule_ur",
            "sample",
            "data_last_modified",
            "dmrpp_last_modified",
            "dmrpp_created",
            "dmrpp_version",
            "data_newer_by_s",
            "data_size",
            "cmr_revision_id",
            "cmr_revision_date",
            "data_url",
            "dmrpp_url",
        ]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(stale_rows, key=lambda r: (r["provider"], r["short_name"], r["granule_ur"] or "")))
    statuses = Counter(c["status"] for c in collections)
    lines = [
        "# DMR++ staleness: sampled inventory",
        "",
        f"{len(collections):,} collections sampled, {len(rows):,} granules checked.",
        "",
        "| Collections | Count |",
        "|---|---:|",
    ] + [f"| {k} | {v:,} |" for k, v in statuses.most_common()]
    lines += [
        "",
        "## By provider",
        "",
        "| Provider | Sampled | With sidecars | With stale sidecars | No sidecars | Could not read |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for provider in sorted({c["provider"] for c in collections}):
        mine = [c for c in collections if c["provider"] == provider]
        lines.append(
            f"| {provider} | {len(mine):,} | {sum(c['with_sidecar'] > 0 for c in mine):,} | {sum(c['stale'] > 0 for c in mine):,} | "
            f"{sum(c['status'] == 'no sidecars' for c in mine):,} | {sum(c['status'] == 'could not read' for c in mine):,} |"
        )
    lines += [
        "",
        "## Stale granules by how much newer the data file is",
        "",
        "| Gap | Granules | Collections |",
        "|---|---:|---:|",
    ]
    for label, low, high in GAPS:
        hits = [r for r in stale_rows if low <= r["data_newer_by_s"] < high]
        lines.append(f"| {label} | {len(hits):,} | {len({r['concept_id'] for r in hits}):,} |")
    lines += [
        "",
        "## Stale granules by the month the data file was last modified",
        "",
        "| Month | Granules | Providers |",
        "|---|---:|---|",
    ]
    months = Counter(r["data_last_modified"][:7] for r in stale_rows)
    for month, n in months.most_common(10):
        lines.append(
            f"| {month} | {n:,} | {', '.join(sorted({r['provider'] for r in stale_rows if r['data_last_modified'][:7] == month}))} |"
        )
    confirmed = sum(bool(r.get("dmrpp_created")) for r in stale_rows)
    built = (
        f"{confirmed:,} of {len(stale_rows):,} stale sidecars carry a `build_dmrpp_metadata/created` time; the rest predate that attribute, "
        "so their build time is the sidecar object's own Last-Modified."
    )
    caveat = (
        "A stale verdict means the data object was written to S3 after its sidecar. It does not show that the bytes changed: "
        "a bulk copy within a bucket rewrites Last-Modified without touching content."
    )
    lines += ["", built, "", caveat]
    lines += [
        "",
        "## Collections with stale sidecars",
        "",
        "| Provider | Collection | Version | Stale / with sidecar | Data newer by, at most (days) |",
        "|---|---|---|---:|---:|",
    ]
    lines += [
        f"| {c['provider']} | {c['short_name']} | {c['version']} | {c['stale']} / {c['with_sidecar']} | {c['max_data_newer_by_days']} |"
        for c in collections
        if c["stale"]
    ]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


SAMPLE_FIELDS = [
    "provider", "short_name", "version", "concept_id", "granule_ur", "sample", "data_format", "data_newer_by_s",
    "data_url", "dmrpp_url",
]  # fmt: skip


def with_sidecars(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The oldest and the newest sampled granule of every collection that has
    sidecars, whether or not a timestamp flagged them: the verifier's input
    for checking sidecars that look fine. A collection with neither an oldest
    nor a newest granule with a sidecar gives its first granule that has one."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("verdict") in ("fresh", "stale"):
            by[row["concept_id"]].append(row)
    chosen = []
    for group in by.values():
        ends = [
            next(r for r in group if r["sample"] == kind)
            for kind in ("oldest", "newest")
            if any(r["sample"] == kind for r in group)
        ]
        chosen += ends or group[:1]
    # The order is part of the file: the verifier picks and numbers granules in
    # the order it reads them, and ``--start`` resumes a run by that number.
    return sorted(chosen, key=lambda r: (r["provider"], r["short_name"], r["sample"]))


def sample(args: argparse.Namespace) -> int:
    out = Path(args.out)
    chosen = with_sidecars(load_rows(out))
    with (out / "sampled_with_sidecars.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(chosen)
    collections = len({row["concept_id"] for row in chosen})
    print(
        f"{len(chosen):,} granules from {collections:,} collections with sidecars in {out / 'sampled_with_sidecars.csv'}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="sample collections and write granules.jsonl (network; resumable)")
    s.add_argument(
        "--inventory",
        required=True,
        help="CSV of cloud collections with concept_id, provider, short_name, version, format_family, granules",
    )
    s.add_argument("--out", default=RESULTS)
    s.add_argument("--providers", nargs="*", help="only these CMR providers")
    s.add_argument("--limit", type=int, help="only the first N collections (after a seeded shuffle)")
    s.add_argument(
        "--tolerance-s", type=float, default=60.0, help="a data file this much newer than its sidecar is stale"
    )
    s.add_argument("--per-host", type=int, default=4, help="requests in flight per DAAC host")
    s.add_argument("--concurrency", type=int, default=32, help="connections in total")
    s.add_argument("--collections-in-flight", type=int, default=24)
    s.set_defaults(fn=lambda a: asyncio.run(run(a)))
    s = sub.add_parser("report", help="collections.csv, stale_granules.csv and summary.md from granules.jsonl")
    s.add_argument("out", nargs="?", default=RESULTS)
    s.set_defaults(fn=report)
    s = sub.add_parser(
        "sample",
        help="sampled_with_sidecars.csv from granules.jsonl: two granules per collection with sidecars, for the verifier",
    )
    s.add_argument("out", nargs="?", default=RESULTS)
    s.set_defaults(fn=sample)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
