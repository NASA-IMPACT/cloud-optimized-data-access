"""Sidecar against file: the two chunk indexes and the two descriptions of each array."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

FILTER_NAMES = {
    "zlib": "deflate",
    "gzip": "deflate",
    "deflate": "deflate",
    "shuffle": "shuffle",
    "fletcher32": "fletcher32",
    "zstd": "zstd",
    "blosc": "blosc",
    "lz4": "lz4",
}


def manifests(group: Any, prefix: str = "", recurse: bool = True) -> dict[str, dict[str, dict[str, Any]]]:
    """{variable path: {chunk key: {path, offset, length}}} for every array under a ManifestGroup."""
    out = {f"{prefix}/{name}": array.manifest.dict() for name, array in group.arrays.items()}
    for name, child in group.groups.items() if recurse else ():
        out.update(manifests(child, f"{prefix}/{name}"))
    return out


def group_paths(group: Any, prefix: str = "") -> list[str]:
    """Paths of every group that holds arrays ("" is the root)."""
    out = [prefix] if group.arrays else []
    for name, child in group.groups.items():
        out += group_paths(child, f"{prefix}/{name}")
    return out


def filters(metadata: Any) -> list[str]:
    """The array's filters in order, by common name; the bytes (endianness) codec is left out."""
    names = []
    for codec in metadata.codecs:
        config = getattr(codec, "codec_config", None) or {}
        name = (
            str(config.get("id") or getattr(codec, "codec_name", "") or type(codec).__name__).rsplit(".", 1)[-1].lower()
        )
        if name in ("bytes", "bytescodec"):
            continue
        names.append(FILTER_NAMES.get(name, name))
    return names


def describe(group: Any, prefix: str = "", recurse: bool = True) -> dict[str, dict[str, Any]]:
    """{variable path: shape, data type, chunk shape and filters} for every array under a ManifestGroup."""
    out = {}
    for name, array in group.arrays.items():
        m = array.metadata
        out[f"{prefix}/{name}"] = {
            "shape": tuple(m.shape),
            "dtype": str(m.data_type),
            "chunks": tuple(m.chunk_grid.chunk_shape),
            "filters": filters(m),
        }
    for name, child in group.groups.items() if recurse else ():
        out.update(describe(child, f"{prefix}/{name}"))
    return out


def compare_metadata(sidecar: dict[str, dict[str, Any]], file: dict[str, dict[str, Any]]) -> tuple[list[str], int]:
    """(differences that change what the bytes mean, variables whose filters agree as a set but not in order).

    Fill values and compression levels are left out: the two parsers report
    them differently for the same file, and neither changes which bytes are read.
    Filter order is counted but not failed for the same reason."""
    differences, reordered = [], 0
    for name in sorted(set(sidecar) & set(file)):
        a, b = sidecar[name], file[name]
        for key in ("shape", "dtype", "chunks"):
            if a[key] != b[key] and not (key == "chunks" and clamped(a, b)):
                differences.append(f"{name} {key}: sidecar {short(a[key])}, file {short(b[key])}")
        if sorted(a["filters"]) != sorted(b["filters"]):
            differences.append(f"{name} filters: sidecar {a['filters']}, file {b['filters']}")
        elif a["filters"] != b["filters"]:
            reordered += 1
    return differences, reordered


def clamped(sidecar: dict[str, Any], file: dict[str, Any]) -> bool:
    """True when the two chunk shapes differ only because the file-side parser cut the chunk down to the array.

    An unfiltered HDF5 chunk is stored whole even when the array is smaller
    than it (a length-1 time axis in a 512-element chunk is 4096 bytes on
    disk). The sidecar reports the chunk as stored; the file-side parser
    reports the part the array uses. Both read the same values."""
    a, b = tuple(sidecar["chunks"]), tuple(file["chunks"])
    return (
        not sidecar["filters"]
        and not file["filters"]
        and len(a) == len(b)
        and b == tuple(file["shape"])
        and all(x >= y for x, y in zip(a, b, strict=True))
    )


def short(value: Any) -> str:
    """Float64(endianness='little') → Float64 little; anything else as it is."""
    text = str(value)
    return (
        text.replace("(endianness='", " ").replace("')", "").replace("()", "")
        if "endianness" in text or text.endswith("()")
        else text
    )


def compare(
    sidecar: dict[str, dict[str, dict[str, Any]]],
    file: dict[str, dict[str, dict[str, Any]]],
    file_size: int | None,
    sidecar_meta: dict[str, dict[str, Any]] | None = None,
    file_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The verdict for one granule from its two chunk indexes."""
    shared = sorted(set(sidecar) & set(file))
    chunks = mismatched = 0
    bad_variables, first = [], None
    for name in shared:
        a, b = sidecar[name], file[name]
        whole = (
            name in (sidecar_meta or {}) and name in (file_meta or {}) and clamped(sidecar_meta[name], file_meta[name])
        )
        differing = [
            k
            for k in sorted(set(a) & set(b))
            if (a[k]["offset"], a[k]["length"]) != (b[k]["offset"], b[k]["length"])
            and not (whole and a[k]["offset"] == b[k]["offset"] and a[k]["length"] >= b[k]["length"])
        ]
        chunks += len(set(a) & set(b))
        mismatched += len(differing)
        if differing:
            bad_variables.append(name)
            k = differing[0]
            first = (
                first
                or f"{name}[{k}]: sidecar {a[k]['offset']}+{a[k]['length']}, file {b[k]['offset']}+{b[k]['length']}"
            )
    max_byte = max((c["offset"] + c["length"] for m in sidecar.values() for c in m.values()), default=0)
    differences, reordered = compare_metadata(sidecar_meta or {}, file_meta or {})
    if file_size is not None and max_byte > file_size:
        verdict = "SIDECAR_PAST_END"
    elif mismatched:
        verdict = "MISMATCH"
    elif differences:
        verdict = "METADATA_MISMATCH"
    elif chunks == 0:
        verdict = "nothing_to_compare"
    else:
        verdict = "match"
    return {
        "verdict": verdict,
        "variables_compared": len(shared),
        "chunks_compared": chunks,
        "chunks_mismatched": mismatched,
        "variables_mismatched": len(bad_variables),
        "only_in_sidecar": len(set(sidecar) - set(file)),
        "only_in_file": len(set(file) - set(sidecar)),
        "file_size": file_size,
        "sidecar_max_byte": max_byte,
        "first_mismatch": first,
        "metadata_differences": "; ".join(differences[:5])
        + (f"; and {len(differences) - 5} more" if len(differences) > 5 else ""),
        "filter_order_differs": reordered,
        # for the value check: variables whose bytes are wrong first, then those the sidecar describes differently
        "mismatched_variables": bad_variables
        + [n for n in dict.fromkeys(d.split(" ", 1)[0] for d in differences) if n not in bad_variables],
    }


def unflatten(sidecar_names: set[str], file_names: set[str]) -> dict[str, str]:
    """{sidecar name: file name} where a sidecar without groups names a grouped
    variable by joining its path with underscores. Empty unless that explains
    names the two sides do not already share."""
    if sidecar_names & file_names:
        return {}
    flat = {"/" + name.strip("/").replace("/", "_"): name for name in file_names if name.count("/") > 1}
    return {name: flat[name] for name in sidecar_names if name in flat}


def spelled(name: str) -> str:
    """A path as a sidecar spells it: anything in a name but letters, digits and
    underscores becomes an underscore (``Data Fields`` → ``Data_Fields``)."""
    import re

    return re.sub(r"[^\w/]", "_", name)


def match_respelled(sidecar_names: set[str], file_names: set[str]) -> dict[str, str]:
    """{sidecar name: file name} where the two differ only in how they are
    spelled (see :func:`spelled`) and the pairing is unambiguous. HDF-EOS5 files
    name a group ``Data Fields``; their sidecars call it ``Data_Fields``."""
    candidates: dict[str, list[str]] = defaultdict(list)
    for name in file_names - sidecar_names:
        candidates[spelled(name)].append(name)
    return {
        name: candidates[spelled(name)][0]
        for name in sidecar_names - file_names
        if len(candidates.get(spelled(name), [])) == 1
    }


def match_by_leaf(sidecar_names: set[str], file_names: set[str]) -> dict[str, str]:
    """{sidecar name: file name} by the last part of the name, for HDF4 files.

    kerchunk lists every HDF4 variable at the root (``/ET_500m``) while the
    sidecar keeps the HDF-EOS2 path (``/MOD_Grid_MOD16A2/Data_Fields/ET_500m``).
    A pair is made only when the last name, with anything but letters, digits
    and underscores read as an underscore, is unique on both sides: two grids
    with the same field names stay unmatched and are counted, not guessed."""
    import re

    def leaf(name: str) -> str:
        return re.sub(r"\W", "_", name.rsplit("/", 1)[-1])

    by_leaf_file: dict[str, list[str]] = defaultdict(list)
    by_leaf_sidecar: dict[str, list[str]] = defaultdict(list)
    for name in file_names:
        by_leaf_file[leaf(name)].append(name)
    for name in sidecar_names:
        by_leaf_sidecar[leaf(name)].append(name)
    out = {}
    for name in sidecar_names - file_names:
        files, sidecars = by_leaf_file.get(leaf(name), []), by_leaf_sidecar[leaf(name)]
        if len(files) == 1 and len(sidecars) == 1 and files[0] not in sidecar_names:
            out[name] = files[0]
    return out
