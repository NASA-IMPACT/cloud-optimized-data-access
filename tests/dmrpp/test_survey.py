"""Offline tests for the DMR++ staleness inventory's pure pieces."""

from __future__ import annotations

from pathlib import Path

from dmrpp import survey as stale

INVENTORY = Path(__file__).parents[2] / "benchmarks" / "cost" / "data" / "cmr_cloud_inventory.csv"


def _umm(*urls):
    return {"RelatedUrls": [{"URL": u, "Type": t} for u, t in urls]}


def test_sidecar_is_the_advertised_one_or_the_data_url_plus_suffix():
    data = "https://d.example.nasa.gov/a/b.nc"
    assert stale.data_and_sidecar(_umm((data, "GET DATA"))) == (data, data + ".dmrpp", False)
    advertised = "https://d.example.nasa.gov/meta/b.nc.dmrpp"
    assert stale.data_and_sidecar(_umm((advertised, "EXTENDED METADATA"), (data, "GET DATA"))) == (
        data,
        advertised,
        True,
    )
    assert stale.data_and_sidecar(
        _umm(("s3://bucket/a/b.nc", "GET DATA VIA DIRECT ACCESS"), ("https://x/b.tif", "GET DATA"))
    ) == (None, None, False)


def test_classify_uses_the_tolerance_and_reports_the_gap():
    def at(t):
        return {"status": 206, "last_modified": f"2026-04-07T02:{t}+00:00"}

    assert stale.classify(at("02:27"), at("02:36"), 60) == ("fresh", -9.0)
    assert stale.classify(at("03:30"), at("02:36"), 60) == ("fresh", 54.0)
    assert stale.classify(at("10:00"), at("02:36"), 60) == ("stale", 444.0)
    assert stale.classify(at("10:00"), {"status": 404, "last_modified": None}, 60) == ("no_dmrpp", None)
    assert stale.classify({"status": "login-redirect", "last_modified": None}, at("02:36"), 60) == ("unreadable", None)


def test_an_html_not_found_page_is_not_a_sidecar():
    assert stale.is_sidecar(
        b'<?xml version="1.0"?>\n<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#" dmrpp:version="3.21.1-367">'
    )
    assert not stale.is_sidecar(b"<!DOCTYPE html PUBLIC ...><html>404 - Not Found</html>")
    assert stale.VERSION.search(b'<Dataset dmrpp:version="3.21.1-367">').group(1) == b"3.21.1-367"
    assert (
        stale.CREATED.search(b'<Attribute name="created" type="String"> <Value>2026-04-07T02:02:35Z</Value>').group(1)
        == b"2026-04-07T02:02:35Z"
    )


def test_file_format_from_magic_bytes():
    assert stale.file_format(b"\x89HDF\r\n\x1a\n") == "HDF5"
    assert stale.file_format(b"CDF\x01\x00\x00\x00\x00") == "netCDF classic"
    assert stale.file_format(b"\x0e\x03\x13\x01abcd") == "HDF4"
    assert stale.file_format(b"<!DOCTYP") is None


def test_summary_is_one_row_per_collection_with_stale_first():
    base = {"short_name": "A", "version": "1", "provider": "P"}
    rows = [
        {**base, "concept_id": "C1", "verdict": "fresh", "data_newer_by_s": -9},
        {**base, "concept_id": "C1", "verdict": "stale", "data_newer_by_s": 172800, "dmrpp_version": "3.21"},
        {**base, "concept_id": "C2", "verdict": "no_dmrpp"},
        {**base, "concept_id": "C3", "verdict": "unreadable", "dmrpp_status": "login-redirect"},
        {**base, "concept_id": "C4", "verdict": "fresh", "data_newer_by_s": 0},
    ]
    out = stale.summarize(rows)
    assert out[0]["concept_id"] == "C1"
    by = {c["concept_id"]: c for c in out}
    assert (
        by["C1"]["status"] == "stale sidecars found"
        and by["C1"]["stale"] == 1
        and by["C1"]["max_data_newer_by_days"] == 2.0
    )
    assert (
        by["C2"]["status"] == "no sidecars"
        and by["C3"]["status"] == "could not read"
        and by["C3"]["unreadable_statuses"] == "login-redirect"
    )
    assert by["C4"]["status"] == "all sampled sidecars fresh"


def test_the_verifiers_sample_is_the_oldest_and_newest_granule_with_a_sidecar():
    def row(concept_id, short_name, sample, verdict, granule):
        return {"concept_id": concept_id, "short_name": short_name, "provider": "P", "version": "1", "sample": sample,
                "verdict": verdict, "granule_ur": granule}  # fmt: skip

    rows = [
        row("C2", "b", "oldest", "fresh", "b-old-1"), row("C2", "b", "oldest", "fresh", "b-old-2"),
        row("C2", "b", "newest", "stale", "b-new-1"), row("C2", "b", "revised", "fresh", "b-rev"),
        row("C1", "a", "oldest", "no_dmrpp", "a-old"), row("C1", "a", "revised", "fresh", "a-rev"),  # only a revised one has a sidecar
        row("C3", "c", "oldest", "no_dmrpp", "c-old"),  # no sidecars: left out
    ]  # fmt: skip
    assert [r["granule_ur"] for r in stale.with_sidecars(rows)] == ["a-rev", "b-new-1", "b-old-1"]


def test_the_committed_inventory_is_the_one_the_survey_samples_from():
    todo = stale.candidates(INVENTORY, None)
    assert len(todo) == 5617 and {"concept_id", "provider", "short_name", "version"} <= set(todo[0])
    assert len(stale.candidates(INVENTORY, {"POCLOUD"})) < len(todo)
