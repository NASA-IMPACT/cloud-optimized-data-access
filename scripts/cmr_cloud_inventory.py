"""Build benchmarks/cost/data/cmr_cloud_inventory.csv from CMR.

Three stages, all against the CMR metadata catalog only (no data-server traffic, no EDL):

  collections  every cloud_hosted=true collection as UMM-C with granule counts (5 requests)
  resample     three pages through the record for the EMS top products (about 60 requests)
  dmrpp        DMR++ sidecar coverage for the largest and graded collections (about 350 requests; after merge)
  sample       for collections whose UMM-C has no AverageFileSize, one UMM-G page of 20 granules
               (about 2,000 requests at ~2/s; resumable; covers 99.9% of unsized granules at --min-granules 1000)
  merge        join sizes and formats (and virtualizability grades, when that file is present) into the CSV

    uv run python scripts/cmr_cloud_inventory.py collections work/
    uv run python scripts/cmr_cloud_inventory.py sample work/ --min-granules 1000
    uv run python scripts/cmr_cloud_inventory.py resample work/
    uv run python scripts/cmr_cloud_inventory.py merge work/

Be considerate: this is a single-worker, rate-limited survey. Do not raise the rate.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path

CMR = "https://cmr.earthdata.nasa.gov/search"
UA = "coda-cost-inventory/0.1 (metadata survey; single pass, rate limited)"
REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "benchmarks" / "cost" / "data"

UNIT = {
    "B": 1e-9,
    "BYTE": 1e-9,
    "BYTES": 1e-9,
    "KB": 1e-6,
    "MB": 1e-3,
    "GB": 1.0,
    "TB": 1e3,
    "PB": 1e6,
}
PROVIDER_TO_DAAC = {
    "LPCLOUD": "LP DAAC",
    "GES_DISC": "GES DISC",
    "LAADS": "LAADS DAAC",
    "ASF": "ASF",
    "POCLOUD": "PO.DAAC",
    "NSIDC_CPRD": "NSIDC",
    "CSDA": "CSDA",
    "LARC_CLOUD": "ASDC",
    "OB_CLOUD": "OB.DAAC",
    "OB_DAAC": "OB.DAAC",
    "GHRC_DAAC": "GHRC",
    "ORNL_CLOUD": "ORNL",
    "SCIOPS": "other",
}
FORMAT_HINTS = [  # (substring of lower-cased format string, family); first match wins
    ("hdf5", "HDF5"),
    ("hdf-5", "HDF5"),
    ("he5", "HDF5"),
    ("hdf-eos5", "HDF5"),
    ("hdfeos5", "HDF5"),
    ("hdf4", "HDF4"),
    ("hdf-4", "HDF4"),
    ("hdf-eos2", "HDF4"),
    ("hdfeos", "HDF4"),
    ("hdf-eos", "HDF4"),
    ("hdf eos", "HDF4"),
    ("netcdf", "netCDF"),
    ("net-cdf", "netCDF"),
    ("cog", "COG"),
    ("cloud optimized", "COG"),
    ("cloud-optimized", "COG"),
    ("geotiff", "GeoTIFF"),
    ("tiff", "GeoTIFF"),
    ("zarr", "Zarr"),
    ("grib", "GRIB"),
    ("parquet", "Parquet"),
    ("safe", "zip"),
    ("zip", "zip"),
    ("csv", "text"),
    ("ascii", "text"),
    ("icartt", "text"),
    ("text", "text"),
    ("ames", "text"),
    ("json", "JSON"),
    ("shapefile", "vector"),
    ("kml", "vector"),
    ("kmz", "vector"),
    ("jpeg", "image"),
    ("jpg", "image"),
    ("png", "image"),
    ("jp2", "image"),
    ("binary", "binary"),
    ("envi", "binary"),
    ("pdf", "document"),
    ("xml", "document"),
]
EXACT = {
    "hdf": "HDF4",
    "h5": "HDF5",
    "nc": "netCDF",
    "nc4": "netCDF",
    "tif": "GeoTIFF",
    "txt": "text",
    "bin": "binary",
    "raw": "binary",
}
# When a collection lists several formats, the array or raster container decides its class; sidecar
# formats (browse PNG, XML, PDF, JSON) come last.
FAMILY_PRIORITY = [
    "Zarr",
    "COG",
    "GeoTIFF",
    "netCDF",
    "HDF5",
    "HDF4",
    "GRIB",
    "Parquet",
    "zip",
    "binary",
    "vector",
    "text",
    "JSON",
    "image",
    "document",
    "other",
    "unknown",
]


def primary_family(families) -> str:
    fams = [f for f in families if f != "unknown"]
    if not fams:
        return "unknown"
    return min(
        fams,
        key=lambda f: FAMILY_PRIORITY.index(f) if f in FAMILY_PRIORITY else len(FAMILY_PRIORITY),
    )


def norm_format(fmt: str | None) -> str:
    f = (fmt or "").strip().lower()
    if not f or f in (
        "not provided",
        "not applicable",
        "n/a",
        "na",
        "various",
        "multiple",
    ):
        return "unknown"
    if f in EXACT:
        return EXACT[f]
    for hint, family in FORMAT_HINTS:
        if hint in f:
            return family
    return "other"


def to_gb(value, unit):
    f = UNIT.get((unit or "").upper().strip())
    if value is None or f is None:
        return None
    try:
        return float(value) * f
    except (TypeError, ValueError):
        return None


def fetch(url: str, headers: dict | None = None):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": e.code}, {}
        except Exception:  # noqa: BLE001
            time.sleep(5 * (attempt + 1))
    return {"error": "gave up"}, {}


# ---------------------------------------------------------------------------------------------


def stage_collections(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    url = f"{CMR}/collections.umm_json?cloud_hosted=true&page_size=2000&include_granule_counts=true"
    search_after, page = None, 0
    while True:
        d, h = fetch(url, {"CMR-Search-After": search_after} if search_after else None)
        items = d.get("items", [])
        page += 1
        (work / f"collections-{page:02d}.json").write_text(json.dumps(d))
        print(f"page {page}: {len(items)} items (hits={h.get('CMR-Hits')})", flush=True)
        search_after = h.get("CMR-Search-After")
        if not items or not search_after or len(items) < 2000:
            break
        time.sleep(1.0)


def parse_collections(work: Path) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(str(work / "collections-*.json"))):
        for it in json.loads(Path(path).read_text())["items"]:
            m, u = it["meta"], it["umm"]
            adi = u.get("ArchiveAndDistributionInformation") or {}
            entries = (adi.get("FileArchiveInformation") or []) + (adi.get("FileDistributionInformation") or [])
            avg_gb = None
            fmts = []
            for e in entries:
                fmts.append(e.get("Format"))
                if avg_gb is None:
                    avg_gb = to_gb(e.get("AverageFileSize"), e.get("AverageFileSizeUnit"))
            families = sorted({norm_format(f) for f in fmts if f} - {"unknown"})
            te = (u.get("TemporalExtents") or [{}])[0]
            rdt = (te.get("RangeDateTimes") or [{}])[0]
            rows.append(
                {
                    "concept_id": m["concept-id"],
                    "provider": m.get("provider-id"),
                    "short_name": u.get("ShortName"),
                    "version": u.get("Version"),
                    "granules": m.get("granule-count") or 0,
                    "level": (u.get("ProcessingLevel") or {}).get("Id"),
                    "formats": families or ["unknown"],
                    "formats_raw": sorted({f for f in fmts if f}),
                    "avg_gb": avg_gb,
                    "begin": rdt.get("BeginningDateTime"),
                    "ends_at_present": bool(te.get("EndsAtPresentFlag")),
                }
            )
    return rows


def stage_sample(
    work: Path,
    min_granules: int,
    sized_top: int = 0,
    delay: float = 0.4,
    page: int = 20,
) -> None:
    """Sample unsized collections with at least ``min_granules`` granules, plus (``--sized-top N``) the N
    catalog-sized collections with the most declared bytes, since AverageFileSize is wrong by orders of
    magnitude for some records (unit slips) and is per file rather than per granule for multi-file products."""
    out_path = work / "granule_samples.jsonl"
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            done.add(json.loads(line)["concept_id"])
    allrows = parse_collections(work)
    rows = [r for r in allrows if not r["avg_gb"] and r["granules"] >= min_granules]
    rows.sort(key=lambda r: -r["granules"])
    if sized_top:
        sized = sorted(
            [r for r in allrows if r["avg_gb"] and r["granules"] > 0],
            key=lambda r: -(r["avg_gb"] * r["granules"]),
        )
        rows += sized[:sized_top]
    rows = [r for r in rows if r["concept_id"] not in done]
    print(f"{len(rows)} collections to sample ({len(done)} done)", flush=True)
    with open(out_path, "a") as out:
        for i, r in enumerate(rows, 1):
            d, _ = fetch(f"{CMR}/granules.umm_json?collection_concept_id={r['concept_id']}&page_size={page}")
            rec = {
                "concept_id": r["concept_id"],
                "short_name": r["short_name"],
                "provider": r["provider"],
                "granules": r["granules"],
            }
            if "error" in d:
                rec["error"] = d["error"]
            else:
                sizes, fmts = [], set()
                for it in d.get("items", []):
                    dg = it["umm"].get("DataGranule") or {}
                    total, found = 0.0, False
                    for e in dg.get("ArchiveAndDistributionInformation") or []:
                        if e.get("Format"):
                            fmts.add(e["Format"])
                        for f in e.get("Files") or []:
                            if f.get("Format"):
                                fmts.add(f["Format"])
                        if e.get("SizeInBytes") is not None:
                            total += float(e["SizeInBytes"]) * 1e-9
                            found = True
                        elif e.get("Size") is not None and to_gb(e["Size"], e.get("SizeUnit")) is not None:
                            total += to_gb(e["Size"], e.get("SizeUnit"))
                            found = True
                    if found:
                        sizes.append(total)
                rec.update(
                    {
                        "sampled": len(d.get("items", [])),
                        "sized": len(sizes),
                        "mean_gb": sum(sizes) / len(sizes) if sizes else None,
                        "min_gb": min(sizes) if sizes else None,
                        "max_gb": max(sizes) if sizes else None,
                        "formats": sorted(fmts),
                    }
                )
            out.write(json.dumps(rec) + "\n")
            out.flush()
            if i % 100 == 0:
                print(f"{i}/{len(rows)}", flush=True)
            time.sleep(delay)


def _finite(value) -> float:
    """A float from a CSV cell, or 0.0 for blank, non-numeric, NaN, or infinite values."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def stage_resample(work: Path, delay: float = 0.5) -> None:
    """Deeper size samples for the EMS top products (by volume and by file count): three pages of 20
    granules spread through each collection's record, sorted by start date. The merge prefers these
    records over the first-page sample and over the catalog figure. About 60 requests."""
    ems = json.loads((DATA / "ems_fy2026.json").read_text())
    names = {str(r[1]).upper() for r in ems["top10_products"][:20]}
    targets = [r for r in parse_collections(work) if r["short_name"] and r["short_name"].upper() in names]
    print(f"{len(targets)} anchor collections", flush=True)
    with open(work / "granule_samples.jsonl", "a") as out:
        for r in targets:
            n = r["granules"]
            pages = sorted({1, max(1, min(500, n // 40)), max(1, min(500, n // 20))})  # page_num <= 500 at page_size 20
            sizes = []
            for pn in pages:
                d, _ = fetch(
                    f"{CMR}/granules.umm_json?collection_concept_id={r['concept_id']}&page_size=20&page_num={pn}&sort_key=start_date"
                )
                for it in d.get("items", []):
                    dg = it["umm"].get("DataGranule") or {}
                    total, found = 0.0, False
                    for e in dg.get("ArchiveAndDistributionInformation") or []:
                        if e.get("SizeInBytes") is not None:
                            total += float(e["SizeInBytes"]) * 1e-9
                            found = True
                        elif e.get("Size") is not None and to_gb(e["Size"], e.get("SizeUnit")) is not None:
                            total += to_gb(e["Size"], e.get("SizeUnit"))
                            found = True
                    if found:
                        sizes.append(total)
                time.sleep(delay)
            rec = {
                "concept_id": r["concept_id"],
                "short_name": r["short_name"],
                "granules": n,
                "kind": "anchor-resample",
                "sampled": len(sizes),
                "sized": len(sizes),
                "mean_gb": sum(sizes) / len(sizes) if sizes else None,
                "min_gb": min(sizes) if sizes else None,
                "max_gb": max(sizes) if sizes else None,
                "formats": [],
            }
            out.write(json.dumps(rec) + "\n")
            print(
                f"  {r['short_name']:30} pages={pages} sampled={len(sizes)} mean={rec['mean_gb']}",
                flush=True,
            )


def stage_dmrpp(work: Path, top: int = 300, delay: float = 0.5) -> None:
    """DMR++ sidecar coverage probe: one UMM-G page of 10 granules for the ``top`` largest
    catalog-sized collections plus every graded one; records whether any RelatedUrl ends in .dmrpp.
    Writes benchmarks/cost/data/dmrpp_probe.jsonl (resumable). About 350 requests. Run after merge."""
    with open(DATA / "cmr_cloud_inventory.csv") as fh:
        inv = list(csv.DictReader(fh))
    by_bytes = sorted(inv, key=lambda r: -float(r["archive_tb"] or 0))[:top]
    chosen = {r["concept_id"]: r for r in by_bytes + [r for r in inv if r["grade"]]}
    out_path = DATA / "dmrpp_probe.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(line)["concept_id"] for line in out_path.read_text().splitlines()}
    todo = [r for r in chosen.values() if r["concept_id"] not in done]
    print(f"{len(todo)} collections to probe ({len(done)} done)", flush=True)
    with open(out_path, "a") as out:
        for r in todo:
            d, _ = fetch(f"{CMR}/granules.umm_json?collection_concept_id={r['concept_id']}&page_size=10")
            rec = {
                "concept_id": r["concept_id"],
                "short_name": r["short_name"],
                "provider": r["provider"],
            }
            if "error" in d:
                rec["error"] = d["error"]
            else:
                items = d.get("items", [])
                urls = [[u.get("URL", "") for u in it["umm"].get("RelatedUrls", [])] for it in items]
                rec.update(
                    {
                        "granules_checked": len(items),
                        "with_dmrpp": sum(any(u.endswith(".dmrpp") for u in us) for us in urls),
                        "with_s3_link": sum(any(u.startswith("s3://") for u in us) for us in urls),
                    }
                )
            out.write(json.dumps(rec) + "\n")
            out.flush()
            time.sleep(delay)


def norm_level(level) -> str:
    if not level:
        return ""
    s = str(level).upper().replace("LEVEL", "").replace(" ", "").replace("L", "")
    for key in ("4", "3", "2", "1", "0"):
        if s.startswith(key):
            return "L" + key
    return "NA" if "N" in s else ""


def stage_merge(work: Path, out_path: Path) -> None:
    samples = {}
    sp = work / "granule_samples.jsonl"
    if sp.exists():
        for line in sp.read_text().splitlines():
            rec = json.loads(line)
            if (
                rec.get("kind") == "anchor-resample"
                or samples.get(rec["concept_id"], {}).get("kind") != "anchor-resample"
            ):
                samples[rec["concept_id"]] = rec
    # Optional: grades from alex-s-gardner/EarthDataVirtualizabilityScore. The file is not kept in this
    # repository; without it no collection is graded and none takes its size from the grades.
    grades = {}
    scores = DATA / "virtualizability_scores.csv"
    if scores.exists():
        with open(scores) as fh:
            for r in csv.DictReader(fh):
                grades[r["short_name"]] = r
    rows = []
    for r in parse_collections(work):
        gb, source = r["avg_gb"], "umm-c"
        s = samples.get(r["concept_id"])
        sampled = s["mean_gb"] if s and s.get("mean_gb") else None
        if s and s.get("kind") == "anchor-resample" and sampled:
            # three pages spread through the record beat both the catalog and the first-page sample
            gb, source = sampled, "granule-sample (3 pages)"
            r["avg_gb"] = None
        g = grades.get(r["short_name"])
        if not gb:
            if sampled:
                gb, source = sampled, "granule-sample"
            elif g and _finite(g.get("volume_tb")) > 0 and _finite(g.get("n_granules")) > 0:
                # EarthDataVirtualizabilityScore: median sampled object size x CMR granule count
                gb, source = (
                    _finite(g["volume_tb"]) * 1000 / _finite(g["n_granules"]),
                    "virtualizability-score",
                )
            else:
                gb, source = None, "none"
        elif sampled and not (0.5 <= sampled / gb <= 2.0):
            # the catalog's AverageFileSize disagrees with the granules by more than 2x: trust the granules
            gb, source = (
                sampled,
                "granule-sample (catalog off by %.0fx)" % (gb / sampled if gb > sampled else sampled / gb),
            )
        fmt = primary_family(r["formats"])
        if fmt == "unknown" and s:
            fmt = primary_family([norm_format(f) for f in (s.get("formats") or [])])
        rows.append(
            {
                "concept_id": r["concept_id"],
                "provider": r["provider"],
                "daac": PROVIDER_TO_DAAC.get(r["provider"], r["provider"]),
                "short_name": r["short_name"],
                "version": r["version"],
                "level": norm_level(r["level"]),
                "format_family": fmt,
                "formats_raw": "+".join(r["formats_raw"])[:80],
                "granules": r["granules"],
                "gb_per_granule": f"{gb:.6f}" if gb else "",
                "size_source": source,
                "archive_tb": f"{gb * r['granules'] / 1000:.3f}" if gb else "",
                "ends_at_present": int(r["ends_at_present"]),
                "begin": (r["begin"] or "")[:10],
                "grade": g["grade"] if g else "",
                "grade_blocker": g["blocker"][:60] if g else "",
            }
        )
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    sized = [r for r in rows if r["gb_per_granule"]]
    print(
        f"{len(rows)} collections; sized {len(sized)} covering "
        f"{sum(r['granules'] for r in sized) / sum(r['granules'] for r in rows):.1%} of granules; "
        f"{sum(float(r['archive_tb']) for r in sized) / 1000:.1f} PB estimated; "
        f"graded {sum(1 for r in rows if r['grade'])}"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stage", choices=["collections", "sample", "resample", "merge", "dmrpp"])
    ap.add_argument("work", type=Path, help="working directory for raw CMR responses")
    ap.add_argument("--min-granules", type=int, default=1000)
    ap.add_argument(
        "--sized-top",
        type=int,
        default=300,
        help="also sample the N catalog-sized collections with the most declared bytes",
    )
    ap.add_argument("--out", type=Path, default=DATA / "cmr_cloud_inventory.csv")
    a = ap.parse_args(argv)
    if a.stage == "collections":
        stage_collections(a.work)
    elif a.stage == "resample":
        stage_resample(a.work)
    elif a.stage == "dmrpp":
        stage_dmrpp(a.work)
    elif a.stage == "sample":
        stage_sample(a.work, a.min_granules, a.sized_top)
    else:
        stage_merge(a.work, a.out)


if __name__ == "__main__":
    main()
