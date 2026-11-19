"""Offline tests for the DMR++ verifier's comparison and sampling."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dmrpp.verify import access, cli, compare, sidecar, values


def chunk(offset, length):
    return {"path": "s3://b/f.nc", "offset": offset, "length": length}


def test_matching_indexes_match_and_extra_variables_are_only_counted():
    file = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(150, 50)}, "/small": {"0": chunk(10, 4)}}
    sidecar = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(150, 50)}, "/inlined": {}}
    out = compare.compare(sidecar, file, file_size=1000)
    assert out["verdict"] == "match" and out["chunks_compared"] == 2 and out["chunks_mismatched"] == 0
    assert out["only_in_file"] == 1 and out["only_in_sidecar"] == 1


def test_a_moved_chunk_is_a_mismatch_with_an_example():
    file = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(150, 50)}}
    sidecar = {"/a": {"0.0": chunk(100, 50), "0.1": chunk(160, 50)}}
    out = compare.compare(sidecar, file, file_size=1000)
    assert out["verdict"] == "MISMATCH" and out["chunks_mismatched"] == 1 and out["variables_mismatched"] == 1
    assert out["first_mismatch"] == "/a[0.1]: sidecar 160+50, file 150+50"


def test_a_sidecar_pointing_past_the_end_of_the_file_is_its_own_verdict():
    index = {"/a": {"0": chunk(900, 200)}}
    assert compare.compare(index, index, file_size=1000)["verdict"] == "SIDECAR_PAST_END"
    assert compare.compare(index, index, file_size=1100)["verdict"] == "match"
    assert compare.compare({"/a": {}}, {"/b": {}}, file_size=10)["verdict"] == "nothing_to_compare"


def test_manifests_walks_nested_groups():
    array = SimpleNamespace(manifest=SimpleNamespace(dict=lambda: {"0": chunk(1, 2)}))
    inner = SimpleNamespace(arrays={"v": array}, groups={})
    root = SimpleNamespace(arrays={"t": array}, groups={"product": inner})
    assert set(compare.manifests(root)) == {"/t", "/product/v"}


def test_pick_takes_a_few_per_collection_largest_gaps_first():
    rows = [{"concept_id": "C1", "data_newer_by_s": str(86400 * d), "granule_ur": f"a{d}"} for d in (1, 2, 3)]
    rows += [{"concept_id": "C2", "data_newer_by_s": str(86400 * 400), "granule_ur": "b"}]
    rows += [{"concept_id": "C3", "data_newer_by_s": "600", "granule_ur": "c"}]
    chosen = cli.pick(rows, per_collection=2, min_gap_days=0.5, limit=None)
    assert chosen[0]["granule_ur"] == "b" and len(chosen) == 3 and all(r["concept_id"] != "C3" for r in chosen)
    assert len(cli.pick(rows, per_collection=2, min_gap_days=0, limit=2)) == 2


def test_the_listing_scripts_rows_are_accepted():
    row = cli.normalise(
        {"concept_id": "C1-POCLOUD", "short_name": "X", "bucket": "b", "path": "X/f.nc", "data_newer_by_s": "90000"}
    )
    assert row["data_url"] == "s3://b/X/f.nc" and row["provider"] == "POCLOUD"
    assert cli.pick([row], per_collection=1, min_gap_days=1, limit=None) == [row]
    kept = cli.normalise({"provider": "P", "data_url": "https://h/f.nc", "concept_id": "C2-Q"})
    assert kept["data_url"] == "https://h/f.nc" and kept["provider"] == "P"


def meta(shape=(1, 4, 4), dtype="Float32(endianness='little')", chunks=(1, 2, 2), filters=("shuffle", "deflate")):
    return {"shape": shape, "dtype": dtype, "chunks": chunks, "filters": list(filters)}


def test_metadata_that_changes_what_the_bytes_mean_is_a_mismatch_but_filter_order_is_only_counted():
    same, reordered = compare.compare_metadata({"/a": meta(filters=("deflate", "shuffle"))}, {"/a": meta()})
    assert same == [] and reordered == 1
    differences, _ = compare.compare_metadata(
        {"/a": meta(dtype="Float64(endianness='little')"), "/b": meta()},
        {"/a": meta(dtype="Float64(endianness='big')", filters=()), "/b": meta()},
    )
    assert len(differences) == 2 and "/a dtype" in differences[0] and "/a filters" in differences[1]
    index = {"/a": {"0": chunk(0, 10)}}
    out = compare.compare(index, index, 100, {"/a": meta(shape=(1, 8, 4))}, {"/a": meta()})
    assert out["verdict"] == "METADATA_MISMATCH" and "/a shape" in out["metadata_differences"]
    assert (
        compare.compare(index, index, 100, {"/a": meta(filters=("deflate", "shuffle"))}, {"/a": meta()})["verdict"]
        == "match"
    )


def test_filters_are_named_in_order_without_the_bytes_codec():
    codecs = [
        SimpleNamespace(codec_name="bytes"),
        SimpleNamespace(codec_config={"id": "shuffle"}),
        SimpleNamespace(codec_config={"id": "zlib", "level": 5}),
    ]
    assert compare.filters(SimpleNamespace(codecs=codecs)) == ["shuffle", "deflate"]


def test_value_check_picks_mismatched_variables_first_and_skips_large_or_non_numeric_ones():
    file_meta = {
        "/big": meta(shape=(1000, 1000, 100)),
        "/sst": meta(shape=(1, 100, 100)),
        "/lat": meta(shape=(100,), dtype="Float64(endianness='big')"),
        "/name": meta(shape=(10,), dtype="FixedLengthUTF32(length=4)"),
        "/scalar": meta(shape=()),
    }
    assert values.choose_variables(file_meta, ["/lat"], limit=2, max_bytes=64 * 2**20) == ["/lat", "/sst"]
    assert values.choose_variables(file_meta, [], limit=5, max_bytes=64 * 2**20) == ["/sst", "/lat"]
    assert values.choose_variables(file_meta, ["/lat"], limit=2) == [
        "/lat",
        "/big",
    ]  # no size limit: a block is read, not the whole


def test_the_value_check_reads_a_block_from_the_middle_of_each_variable():
    block = values.slab((1, 9000, 18000), edge=256)
    assert block == (slice(0, 1), slice(4372, 4628), slice(8872, 9128))
    assert values.slab((10,), edge=256) == (slice(0, 10),)
    assert values.constraint("/sea_ice_fraction", block) == "/sea_ice_fraction[0:1:0][4372:1:4627][8872:1:9127]"


def test_same_values_treats_nan_as_equal_and_shape_as_part_of_the_answer():
    import numpy as np

    a = np.array([1.0, np.nan])
    assert values.same_values(a, a.copy()) and not values.same_values(a, np.array([1.0, 2.0]))
    assert not values.same_values(np.zeros((2, 1)), np.zeros(2))
    assert values.same_values(np.array([1, 2], dtype="int16"), np.array([1, 2], dtype="int32"))


def test_groups_are_listed_and_metadata_mismatches_are_value_checked_before_other_variables():
    array = SimpleNamespace(manifest=SimpleNamespace(dict=lambda: {"0": chunk(1, 2)}))
    leaf = SimpleNamespace(arrays={"v": array}, groups={})
    root = SimpleNamespace(arrays={}, groups={"data_01": SimpleNamespace(arrays={"t": array}, groups={"ku": leaf})})
    assert compare.group_paths(root) == ["/data_01", "/data_01/ku"]
    assert set(compare.manifests(root.groups["data_01"], "/data_01", recurse=False)) == {"/data_01/t"}
    index = {"/sst": {"0": chunk(0, 10)}, "/mask": {"0": chunk(10, 10)}}
    described = {"/sst": meta(), "/mask": meta(dtype="Int8()")}
    out = compare.compare(
        index, index, 100, {"/sst": meta(), "/mask": meta(dtype="Int16(endianness='little')")}, described
    )
    assert out["verdict"] == "METADATA_MISMATCH" and out["mismatched_variables"] == ["/mask"]
    assert "/mask dtype: sidecar Int16 little, file Int8" in out["metadata_differences"]


def test_sidecar_signs_are_read_from_the_text():
    old = b'<Dataset name="a.nc"><Int16 name="mask"><Attribute name="fullnamepath" type="String"/></Int16><Attribute name="HDF5_GLOBAL" type="Container"/></Dataset>'
    new = b'<Dataset name="a.nc" dmrpp:version="3.20.9-91"><Int8 name="mask"/><Int8 name="ice"/></Dataset>'
    assert sidecar.sidecar_signs(old) == {
        "sidecar_version": "",
        "sidecar_cf_markers": 1,
        "sidecar_hdf5_global": True,
        "sidecar_int8_variables": 0,
    }
    assert sidecar.sidecar_signs(new) == {
        "sidecar_version": "3.20.9-91",
        "sidecar_cf_markers": 0,
        "sidecar_hdf5_global": False,
        "sidecar_int8_variables": 2,
    }


def test_pick_without_a_gap_filter_keeps_files_older_than_their_sidecar():
    rows = [
        {"concept_id": "C1", "data_newer_by_s": "-9", "granule_ur": "fresh"},
        {"concept_id": "C2", "data_newer_by_s": "", "granule_ur": "unknown"},
    ]
    assert len(cli.pick(rows, per_collection=1, min_gap_days=None, limit=None)) == 2
    assert cli.pick(rows, per_collection=1, min_gap_days=0, limit=None) == [rows[1]]


def test_flattened_sidecar_names_are_matched_to_grouped_file_names():
    file_names = {"/data_01/time", "/data_01/ku/range", "/lat"}
    assert compare.unflatten({"/data_01_time", "/data_01_ku_range", "/other"}, file_names) == {
        "/data_01_time": "/data_01/time",
        "/data_01_ku_range": "/data_01/ku/range",
    }
    assert compare.unflatten({"/data_01/time"}, file_names) == {}  # names already shared: nothing to do
    assert compare.unflatten({"/lat_x"}, {"/lat"}) == {}


def test_an_unfiltered_chunk_stored_whole_for_a_smaller_array_is_not_a_mismatch():
    sidecar_meta = {"/time": meta(shape=(1,), dtype="Float64(endianness='little')", chunks=(512,), filters=())}
    file_meta = {"/time": meta(shape=(1,), dtype="Float64(endianness='little')", chunks=(1,), filters=())}
    out = compare.compare(
        {"/time": {"0": chunk(49243, 4096)}}, {"/time": {"0": chunk(49243, 8)}}, 10**6, sidecar_meta, file_meta
    )
    assert out["verdict"] == "match" and out["chunks_mismatched"] == 0
    moved = compare.compare(
        {"/time": {"0": chunk(100, 4096)}}, {"/time": {"0": chunk(49243, 8)}}, 10**6, sidecar_meta, file_meta
    )
    assert moved["verdict"] == "MISMATCH"
    packed = {"/time": dict(sidecar_meta["/time"], filters=["deflate"])}
    assert (
        compare.compare(
            {"/time": {"0": chunk(49243, 4096)}}, {"/time": {"0": chunk(49243, 8)}}, 10**6, packed, file_meta
        )["verdict"]
        == "MISMATCH"
    )


SIDECAR_XML = b"""<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#" xmlns:dmrpp="http://xml.opendap.org/dap/dmrpp/1.0.0#" name="f.hdf">
  <Group name="Grid">
    <Float64 name="Latitude">
      <dmrpp:chunks byteOrder="LE"><dmrpp:chunk offset="8068541" nBytes="13572" href="OPeNDAP_DMRpp_SC_DATA_ACCESS_URL"/></dmrpp:chunks>
    </Float64>
    <Group name="Data_Fields">
      <Int16 name="ET"><dmrpp:chunks byteOrder="BE"><dmrpp:chunk offset="3622" nBytes="32"/></dmrpp:chunks></Int16>
      <Byte name="QC"><Attribute name="_FillValue" type="String"><Value>NA</Value></Attribute><dmrpp:chunks byteOrder="BE"><dmrpp:chunk offset="20398" nBytes="26"/></dmrpp:chunks></Byte>
    </Group>
  </Group>
</Dataset>"""


def test_sidecar_groups_byte_order_and_external_chunks_are_read_from_the_xml():
    from xml.etree import ElementTree

    root = ElementTree.fromstring(SIDECAR_XML)
    assert sidecar.sidecar_group_paths(root) == ["/Grid", "/Grid/Data_Fields"]
    notes = sidecar.sidecar_notes(root)
    assert {name: {k: note[k] for k in ("big_endian", "external", "text_fill")} for name, note in notes.items()} == {
        "/Grid/Latitude": {"big_endian": False, "external": True, "text_fill": False},
        "/Grid/Data_Fields/ET": {"big_endian": True, "external": False, "text_fill": False},
        "/Grid/Data_Fields/QC": {"big_endian": True, "external": False, "text_fill": True},
    }
    assert all(note["checksum_at"] is None and not note["scalar_text"] for note in notes.values())
    index = {"/Grid/Latitude": {"0.0": chunk(8068541, 13572)}, "/Grid/Data_Fields/ET": {"0.0": chunk(3622, 32)}}
    meta = {
        "/Grid/Latitude": {"dtype": "Float64(endianness='little')"},
        "/Grid/Data_Fields/ET": {"dtype": "Int16(endianness='little')"},
        "/Grid/Data_Fields/QC": {"dtype": "UInt8()"},
    }
    assert sidecar.apply_sidecar_notes(index, meta, notes) == 1
    assert "/Grid/Latitude" not in index and "/Grid/Latitude" not in meta
    assert meta["/Grid/Data_Fields/ET"]["dtype"] == "Int16(endianness='big')"
    assert meta["/Grid/Data_Fields/QC"]["dtype"] == "UInt8()"


def test_hdf4_names_are_matched_by_a_last_name_unique_on_both_sides():
    sidecar = {
        "/G1/Data_Fields/ET",
        "/G1/Data_Fields/QC",
        "/G2/Data_Fields/QC",
        "/G1/Data_Fields/Cloud_Top",
        "/inlined",
    }
    file = {"/ET", "/QC", "/Cloud Top"}
    assert compare.match_by_leaf(sidecar, file) == {
        "/G1/Data_Fields/ET": "/ET",
        "/G1/Data_Fields/Cloud_Top": "/Cloud Top",
    }
    assert compare.match_by_leaf({"/ET"}, {"/ET"}) == {}


def test_hdf4_value_check_asks_hyrax_only_for_variables_with_a_sidecar_name():
    meta = {"/ET": {"dtype": "Int16"}, "/QC": {"dtype": "UInt16"}}
    assert values.value_candidates("HDF4", meta, {"/ET": "/Grid/Data_Fields/ET"}) == {"/ET": {"dtype": "Int16"}}
    assert values.value_candidates("HDF5", meta, {}) == meta


# Shapes taken from real sidecars: a Sentinel-5P file with checksummed chunks (GES DISC) and an
# HDF-EOS5 VIIRS file (LP DAAC), whose "Data Fields" group the sidecar calls Data_Fields.
CHECKSUM_AND_EOS5_XML = b"""<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#" xmlns:dmrpp="http://xml.opendap.org/dap/dmrpp/1.0.0#" name="f.h5">
<Float32 name="latitude"><dmrpp:chunks compressionType="fletcher32" fillValue="9.96921e+36" byteOrder="LE"><dmrpp:chunk offset="100" nBytes="44"/></dmrpp:chunks></Float32>
<UByte name="histogram"><dmrpp:chunks compressionType="shuffle deflate fletcher32" deflateLevel="3" fillValue="255" byteOrder="LE"><dmrpp:chunk offset="200" nBytes="30"/></dmrpp:chunks></UByte>
<Group name="HDFEOS_INFORMATION"><String name="StructMetadata_0"><dmrpp:chunks fillValue=""><dmrpp:chunk offset="300" nBytes="32000"/></dmrpp:chunks></String></Group>
<Group name="HDFEOS"><Group name="GRIDS"><Group name="VIIRS_CMG_BRDF"><Group name="Data_Fields">
<Byte name="Snow_Status"><Dim name="/YDim"/><dmrpp:chunks compressionType="deflate" fillValue="0" byteOrder="LE"><dmrpp:chunk offset="400" nBytes="9"/></dmrpp:chunks></Byte>
</Group></Group></Group></Group>
</Dataset>"""


def test_the_checksum_filter_is_read_from_the_xml_because_the_parser_leaves_it_out():
    """Without this every checksummed file read METADATA_MISMATCH: the sidecar was right and the parser dropped fletcher32."""
    from xml.etree import ElementTree

    notes = sidecar.sidecar_notes(ElementTree.fromstring(CHECKSUM_AND_EOS5_XML))
    assert (notes["/latitude"]["checksum_at"], notes["/histogram"]["checksum_at"]) == (0, 2)
    assert notes["/HDFEOS/GRIDS/VIIRS_CMG_BRDF/Data_Fields/Snow_Status"]["checksum_at"] is None
    meta = {
        "/latitude": {"dtype": "Float32(endianness='little')", "filters": []},  # as the parser reports them
        "/histogram": {"dtype": "UInt8()", "filters": ["shuffle", "deflate"]},
    }
    sidecar.apply_sidecar_notes({}, meta, notes)
    assert meta["/latitude"]["filters"] == ["fletcher32"]
    assert meta["/histogram"]["filters"] == ["shuffle", "deflate", "fletcher32"]
    file_meta = {
        "/latitude": {
            "shape": (4,),
            "dtype": "Float32(endianness='little')",
            "chunks": (4,),
            "filters": ["fletcher32"],
        },
        "/histogram": {
            "shape": (9,),
            "dtype": "UInt8()",
            "chunks": (9,),
            "filters": ["shuffle", "deflate", "fletcher32"],
        },
    }
    for name in meta:
        meta[name] = {**file_meta[name], "filters": meta[name]["filters"]}
    assert compare.compare_metadata(meta, file_meta) == ([], 0)
    sidecar.apply_sidecar_notes({}, meta, notes)  # applying twice adds nothing
    assert meta["/histogram"]["filters"].count("fletcher32") == 1


def test_a_string_variable_with_no_dimensions_is_marked_so_its_group_can_be_read_without_it():
    from xml.etree import ElementTree

    notes = sidecar.sidecar_notes(ElementTree.fromstring(CHECKSUM_AND_EOS5_XML))
    assert notes["/HDFEOS_INFORMATION/StructMetadata_0"]["scalar_text"]
    assert not notes["/latitude"]["scalar_text"] and not notes["/histogram"]["text_fill"]


def test_a_group_the_sidecar_spells_with_underscores_is_the_files_group_with_spaces():
    from xml.etree import ElementTree

    assert compare.spelled("/HDFEOS/GRIDS/VIIRS_CMG_BRDF/Data Fields") == "/HDFEOS/GRIDS/VIIRS_CMG_BRDF/Data_Fields"
    in_sidecar = sidecar.sidecar_group_paths(ElementTree.fromstring(CHECKSUM_AND_EOS5_XML))
    assert "/HDFEOS/GRIDS/VIIRS_CMG_BRDF/Data_Fields" in in_sidecar
    sidecar_names = {"/HDFEOS/GRIDS/x/Data_Fields/Snow_Status", "/HDFEOS/GRIDS/x/XDim", "/only/in sidecar"}
    file_names = {"/HDFEOS/GRIDS/x/Data Fields/Snow_Status", "/HDFEOS/GRIDS/x/XDim", "/only_in/file"}
    assert compare.match_respelled(sidecar_names, file_names) == {
        "/HDFEOS/GRIDS/x/Data_Fields/Snow_Status": "/HDFEOS/GRIDS/x/Data Fields/Snow_Status"
    }
    # two file names that respell to the same thing are not guessed between
    assert compare.match_respelled({"/a_b"}, {"/a b", "/a-b"}) == {}
    # names both sides already share are left alone
    assert compare.match_respelled({"/a_b", "/c"}, {"/a_b", "/c"}) == {}


def test_a_slow_cmr_answer_is_retried_and_a_refusal_is_not(monkeypatch):
    import io
    import urllib.error

    outcomes = [TimeoutError("timed out"), urllib.error.URLError("reset"), io.BytesIO(b'{"items": [{"umm": {}}]}')]
    asked = []

    def urlopen(url, timeout):
        asked.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(access.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(access.time, "sleep", lambda seconds: None)
    assert access.cmr_items({"granule_ur": "g"}) == [{"umm": {}}] and len(asked) == 3
    outcomes[:] = [urllib.error.HTTPError("u", 400, "bad request", None, None)]
    with pytest.raises(urllib.error.HTTPError):
        access.cmr_items({"granule_ur": "g"})
    outcomes[:] = [TimeoutError("timed out")] * 4
    with pytest.raises(TimeoutError):
        access.cmr_items({"granule_ur": "g"})
    assert not outcomes  # four attempts, then the error is the granule's verdict


def test_redo_keeps_the_granules_an_earlier_run_did_not_call_match(tmp_path):
    earlier = tmp_path / "survey_x.csv"
    earlier.write_text(
        "verdict,data_url\n"
        "match,https://d/a.nc\n"
        "METADATA_MISMATCH,https://d/b.nc\n"
        "error reading the sidecar: ValueError: Group /x/Data Fields not found,https://d/c.nc\n"
        "nothing_to_compare,https://d/d.nc\n"
        "skipped: file is 135 GB (--max-gb 20),https://d/e.nc\n"
    )
    assert cli.to_redo([str(earlier)]) == {"https://d/b.nc", "https://d/c.nc", "https://d/d.nc"}


def test_merging_runs_keeps_each_granules_newest_verdict_and_says_where_it_came_from(tmp_path):
    from dmrpp.verify import merge

    first, redo = tmp_path / "survey_x.csv", tmp_path / "survey_x_redo.csv"
    first.write_text(
        "verdict,provider,short_name,data_url\n"
        "error reading the sidecar: ValueError: Group not found,P,b,https://d/b.nc\n"
        "match,P,a,https://d/a.nc\n"
    )
    redo.write_text("verdict,provider,short_name,data_url\nmatch,P,b,https://d/b.nc\n")
    rows = merge.merge([first, redo])
    assert [(r["short_name"], r["verdict"], r["run"]) for r in rows] == [
        ("a", "match", "survey_x"),
        ("b", "match", "survey_x_redo"),
    ]
    out = tmp_path / "verdicts.csv"
    assert merge.main([str(out), str(first), str(redo)]) == 0
    again = merge.merge([out])  # a merged file keeps its rows' origins when it is merged again
    assert [r["run"] for r in again] == ["survey_x", "survey_x_redo"]
    assert merge.kind("error reading the sidecar: ValueError") == "error" and merge.kind("MISMATCH") == "MISMATCH"
    assert cli.to_redo([str(out)]) == set()
