"""What the sidecar's XML says that VirtualiZarr's DMR++ parser does not pass on."""

from __future__ import annotations

from typing import Any

DAP4 = "{http://xml.opendap.org/ns/DAP/4.0#}"
DMRPP = "{http://xml.opendap.org/dap/dmrpp/1.0.0#}"


def sidecar_group_paths(element: Any, prefix: str = "") -> list[str]:
    """Paths of every group in the sidecar XML ("" for the root is left to the caller)."""
    out = []
    for group in element.findall(f"{DAP4}Group"):
        path = f"{prefix}/{group.get('name')}"
        out += [path, *sidecar_group_paths(group, path)]
    return out


def sidecar_notes(element: Any, prefix: str = "") -> dict[str, dict[str, bool]]:
    """{variable path: {big_endian, external, text_fill, checksum_at}} read from the sidecar XML itself.

    VirtualiZarr 2.8.0's DMR++ parser drops the first two and the last: it
    reads every variable as little-endian whatever ``byteOrder`` says, it reads
    every chunk from the data file even when the chunk's ``href`` puts it
    elsewhere, and it leaves ``fletcher32`` out of the filters although
    ``compressionType`` lists it (``checksum_at`` is its position there).
    ``text_fill`` marks a numeric variable whose ``_FillValue`` attribute is a
    string, and ``scalar_text`` a string variable with no dimensions; that
    parser fails the whole group on either."""
    out = {}
    for child in element:
        if child.tag == f"{DAP4}Group":
            out.update(sidecar_notes(child, f"{prefix}/{child.get('name')}"))
            continue
        chunks = child.find(f"{DMRPP}chunks")
        if chunks is None or child.get("name") is None:
            continue
        hrefs = {c.get("href") for c in chunks.iter(f"{DMRPP}chunk")} - {None, "OPeNDAP_DMRpp_DATA_ACCESS_URL"}
        text_fill = any(
            a.get("name") == "_FillValue" and a.get("type") == "String" for a in child.findall(f"{DAP4}Attribute")
        )
        declared = (chunks.get("compressionType") or "").split()
        out[f"{prefix}/{child.get('name')}"] = {
            "checksum_at": declared.index("fletcher32") if "fletcher32" in declared else None,
            "big_endian": chunks.get("byteOrder") == "BE",
            "external": bool(hrefs),
            "text_fill": text_fill and child.tag != f"{DAP4}String",
            # a string variable with no dimensions, such as HDF-EOS5's StructMetadata_0
            "scalar_text": child.tag == f"{DAP4}String" and child.find(f"{DAP4}Dim") is None,
        }
    return out


def apply_sidecar_notes(
    index: dict[str, Any], meta: dict[str, dict[str, Any]], notes: dict[str, dict[str, bool]]
) -> int:
    """Drop variables stored outside the data file, and set the declared byte
    order and the checksum filter the parser leaves out; returns how many were dropped."""
    external = [name for name, note in notes.items() if note["external"] and (name in index or name in meta)]
    for name in external:
        index.pop(name, None)
        meta.pop(name, None)
    for name, note in notes.items():
        if note["big_endian"] and name in meta:
            meta[name] = {**meta[name], "dtype": meta[name]["dtype"].replace("endianness='little'", "endianness='big'")}
        at = note.get("checksum_at")
        if at is not None and name in meta and "fletcher32" not in meta[name]["filters"]:
            # compressionType="shuffle deflate fletcher32": the checksum goes where the sidecar lists it
            filters = list(meta[name]["filters"])
            filters.insert(min(at, len(filters)), "fletcher32")
            meta[name] = {**meta[name], "filters": filters}
    return len(external)


def sidecar_signs(text: bytes) -> dict[str, Any]:
    """How a sidecar's variable descriptions appear to have been produced, from its text alone.

    ``sidecar_cf_markers`` counts the ``fullnamepath`` attributes: older sidecars
    carry one per variable, newer ones none. The hypothesis under test is that
    those older sidecars are the ones that declare signed 8-bit data as Int16."""
    import re

    version = re.search(rb'dmrpp:version="([^"]+)"', text)
    return {
        "sidecar_version": version.group(1).decode() if version else "",
        "sidecar_cf_markers": text.count(b'name="fullnamepath"'),
        "sidecar_hdf5_global": b'name="HDF5_GLOBAL"' in text,
        "sidecar_int8_variables": len(re.findall(rb"<Int8 name=", text)),
    }
