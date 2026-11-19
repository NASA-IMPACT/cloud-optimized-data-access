"""Offline test for the listing proof of concept's pairing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dmrpp import list_collection as listing

T0 = datetime(2024, 7, 17, tzinfo=UTC)


def obj(path, minutes):
    return {"path": path, "last_modified": T0 + timedelta(minutes=minutes), "size": 10}


def test_pairs_files_with_sidecars_and_finds_the_ones_written_later():
    objects = [
        obj("c/a.nc", 0),
        obj("c/a.nc.dmrpp", 1),  # sidecar built after the file: fresh
        obj("c/b.nc", 600),
        obj("c/b.nc.dmrpp", 1),  # file rewritten ten hours later: stale
        obj("c/c.h5", 0),  # no sidecar
        obj("c/d.nc.dmrpp", 0),  # sidecar with no file
        obj("c/e.NC4", 1),
        obj("c/e.NC4.dmrpp", 0.5),  # 30 s newer: inside the tolerance
        obj("c/readme.txt", 0),
    ]
    out = listing.pair(objects, tolerance_s=60)
    assert (out["objects"], out["data_files"], out["with_sidecar"], out["without_sidecar"], out["orphan_sidecars"]) == (
        9,
        4,
        3,
        1,
        1,
    )
    assert [r["path"] for r in out["stale"]] == ["c/b.nc"]
    assert out["stale"][0]["data_newer_by_s"] == 599 * 60


def test_date_like_directories_are_climbed_but_never_to_the_bucket_root():
    def climbed(url, up=0):
        return "/".join(listing.climb(url.split("/")[:-1], up))

    assert climbed("s3://b/TEMPO/TEMPO_NO2_L3_V04/2026.11.13/f.nc") == "s3://b/TEMPO/TEMPO_NO2_L3_V04"
    assert climbed("s3://b/GPM_L3/GPM_3IMERGHH.07/2025/045/f.HDF5") == "s3://b/GPM_L3/GPM_3IMERGHH.07"
    assert climbed("s3://b/ECCO_L4_OBP_LLC0090GRID_DAILY_V4R4B/f.nc") == "s3://b/ECCO_L4_OBP_LLC0090GRID_DAILY_V4R4B"
    assert climbed("s3://b/COLL/sub/f.nc", up=1) == "s3://b/COLL"
    assert climbed("s3://b/2024/01/f.nc") == "s3://b/2024"
    assert climbed("s3://b/COLL/f.nc", up=9) == "s3://b/COLL"


def test_summary_row_separates_a_rewrite_window_from_a_sidecar_left_behind():
    objects = [
        obj("c/a.nc", 600),
        obj("c/a.nc.dmrpp", 0),
        obj("c/b.nc", 60 * 24 * 45),
        obj("c/b.nc.dmrpp", 0),
        obj("c/c.nc", 0),
        obj("c/c.nc.dmrpp", 5),
    ]
    row = listing.summary_row(listing.pair(objects, 60), requests=1, seconds=0.5, truncated=False)
    assert (row["stale"], row["stale_over_1_day"], row["stale_over_30_days"], row["max_gap_days"]) == (2, 1, 1, 45.0)
    assert row["busiest_month"] in ("2024-07", "2024-08")


def test_targets_keep_collections_with_sidecars_largest_gap_first(tmp_path):
    import argparse

    path = tmp_path / "collections.csv"
    path.write_text(
        "concept_id,short_name,provider,with_sidecar,stale,max_data_newer_by_days\n"
        "C1-P,a,P,20,0,0\nC2-P,b,P,0,0,0\nC3-Q,c,Q,20,5,0.9\nC4-P,d,P,20,20,480\n"
    )
    args = argparse.Namespace(
        collections=str(path), only_flagged=False, providers=None, limit=None, concept_id=None, provider=None
    )
    assert [r["concept_id"] for r in listing.targets(args)] == ["C4-P", "C3-Q", "C1-P"]
    args.only_flagged, args.providers = True, ["P"]
    assert [r["concept_id"] for r in listing.targets(args)] == ["C4-P"]
