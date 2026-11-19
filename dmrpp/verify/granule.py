"""One granule: index the file, read its sidecar, compare the two, and optionally check Hyrax's values."""

from __future__ import annotations

from typing import Any

from dmrpp.verify.access import Access
from dmrpp.verify.compare import (
    compare,
    describe,
    group_paths,
    manifests,
    match_by_leaf,
    match_respelled,
    spelled,
    unflatten,
)
from dmrpp.verify.hdf4 import HDF4_MAGIC, HDF4_UNSUPPORTED, patch_hdf4_decoding
from dmrpp.verify.sidecar import apply_sidecar_notes, sidecar_group_paths, sidecar_notes, sidecar_signs
from dmrpp.verify.values import check_values, value_candidates


def verify(
    row: dict[str, str],
    access: Access,
    traceback_wanted: bool = False,
    values: str = "none",
    value_variables: int = 3,
    value_edge: int = 256,
    max_gb: float | None = None,
) -> dict[str, Any]:
    from xml.etree import ElementTree

    import obstore
    from obspec_utils.registry import ObjectStoreRegistry
    from virtualizarr.parsers import HDFParser
    from virtualizarr.parsers.dmrpp import DMRParser

    out: dict[str, Any] = {
        "provider": row.get("provider"),
        "short_name": row.get("short_name"),
        "granule_ur": row.get("granule_ur"),
        "data_url": row["data_url"],
        "data_newer_by_days": round(float(row["data_newer_by_s"]) / 86400, 2) if row.get("data_newer_by_s") else None,
    }
    stage = "locating the file"
    try:
        url, (root, store) = access.locate(row)
        registry = ObjectStoreRegistry({root: store})
        key = url[len(root) :].lstrip("/")
        size = int(obstore.head(store, key)["size"])
        if max_gb is not None and size > max_gb * 1e9:
            out["verdict"], out["file_size"] = f"skipped: file is {size / 1e9:,.0f} GB (--max-gb {max_gb:g})", size
            return out
        magic = bytes(obstore.get_range(store, key, start=0, end=8))
        file_meta: dict[str, dict[str, Any]] = {}
        if magic.startswith(b"CDF"):
            from virtualizarr.parsers import NetCDF3Parser

            file_parser, out["file_format"] = NetCDF3Parser(), "netCDF classic"
        elif magic.startswith(b"\x89HDF"):
            file_parser, out["file_format"] = HDFParser(), "HDF5"
        elif magic.startswith(HDF4_MAGIC):
            from virtualizarr.parsers import HDF4Parser

            patch_hdf4_decoding()
            HDF4_UNSUPPORTED.clear()
            # kerchunk opens the file through fsspec, not the object store
            options = {"storage_options": access.fsspec_options(row, url)}
            file_parser, out["file_format"] = HDF4Parser(reader_options=options), "HDF4"
        else:
            out["verdict"] = "unsupported_format"
            return out
        stage = "reading the file's own index"
        file_store = file_parser(url, registry)
        file_group = file_store._group
        file_meta = describe(file_group)
        if out["file_format"] == "HDF4":
            # variables the patched decoder could not index faithfully are listed, not compared
            out["file_variables_unsupported"] = "; ".join(HDF4_UNSUPPORTED)[:200] or None
            for name in {"/" + u.split(":", 1)[0] for u in HDF4_UNSUPPORTED}:
                file_meta.pop(name, None)
        stage = "reading the sidecar"
        sidecar_text = bytes(obstore.get(store, key + ".dmrpp").bytes())
        out.update(sidecar_signs(sidecar_text))
        out["file_int8_variables"] = sum(m["dtype"].startswith("Int8") for m in file_meta.values())
        sidecar_root = ElementTree.fromstring(sidecar_text)
        # the sidecar parser returns one group per call, so ask for each group the file has
        # (an HDF4 file has none as kerchunk reads it, so there ask for each group the sidecar has)
        sidecar_index: dict[str, dict[str, dict[str, Any]]] = {}
        sidecar_meta: dict[str, dict[str, Any]] = {}
        has_groups = b"<Group " in sidecar_text
        if out["file_format"] == "HDF4":
            paths = ["", *sidecar_group_paths(sidecar_root)]
        else:
            # ask for each of the file's groups under the name the sidecar gives it
            in_sidecar = {spelled(p): p for p in sidecar_group_paths(sidecar_root)}
            paths = [in_sidecar.get(spelled(p), p) for p in (group_paths(file_group) or [""])] if has_groups else [""]
        unread = []
        notes = sidecar_notes(sidecar_root)
        for path in paths:
            try:
                part = (
                    DMRParser(root=sidecar_root, data_filepath=url)
                    .parse_dataset(object_store=store, group=path or "/")
                    ._group
                )
            except Exception:  # noqa: BLE001 -- retried below without the variables the parser cannot read
                # VirtualiZarr 2.8.0 fails a whole group on one numeric variable whose _FillValue is text ("NA"),
                # or on one string variable with no dimensions; skip those and retry
                skip = [
                    n.rsplit("/", 1)[-1]
                    for n, note in notes.items()
                    if (note["text_fill"] or note["scalar_text"]) and n.rsplit("/", 1)[0] == path
                ]
                if out["file_format"] != "HDF4" and not skip:
                    raise  # nothing to leave out: the granule is an error, as before
                try:
                    if not skip:
                        raise
                    part = (
                        DMRParser(root=sidecar_root, data_filepath=url, skip_variables=skip)
                        .parse_dataset(object_store=store, group=path or "/")
                        ._group
                    )
                    unread.append(f"{path or '/'}: skipped {', '.join(skip)} (text the parser cannot read)")
                except Exception as retry_error:  # noqa: BLE001 -- for HDF4, one unreadable group is counted, the rest compared
                    if out["file_format"] != "HDF4":
                        raise
                    unread.append(f"{path or '/'}: {type(retry_error).__name__}")
                    continue
            sidecar_index.update(manifests(part, path, recurse=False))
            sidecar_meta.update(describe(part, path, recurse=False))
        out["sidecar_groups_unread"] = "; ".join(unread)[:200] or None
        out["sidecar_external_variables"] = apply_sidecar_notes(sidecar_index, sidecar_meta, notes)
        stage = "comparing"
        file_index = {n: v for n, v in manifests(file_group).items() if n in file_meta or out["file_format"] != "HDF4"}
        renamed = unflatten(set(sidecar_index), set(file_index))
        if renamed:
            # an older sidecar with no groups: /data_01/ku/time is listed as /data_01_ku_time
            out["sidecar_flattened"] = True
        elif out["file_format"] != "HDF4":
            # /HDFEOS/GRIDS/x/Data_Fields/v in the sidecar is the file's /HDFEOS/GRIDS/x/Data Fields/v
            renamed = match_respelled(set(sidecar_index), set(file_index))
        else:
            # kerchunk lists /Grid/Data_Fields/ET as /ET
            renamed = match_by_leaf(set(sidecar_index), set(file_index))
        sidecar_index = {renamed.get(n, n): v for n, v in sidecar_index.items()}
        sidecar_meta = {renamed.get(n, n): v for n, v in sidecar_meta.items()}
        # Hyrax names a variable as the sidecar does: for HDF4 by the sidecar's path, not kerchunk's,
        # and for HDF-EOS5 as .../Data_Fields/v where the file has .../Data Fields/v
        served_names = {f: n for n, f in renamed.items()}
        out.setdefault("sidecar_flattened", False)
        out.update(compare(sidecar_index, file_index, size, sidecar_meta, file_meta))
        out["int8_declared_int16"] = sum(
            file_meta[n]["dtype"].startswith("Int8") and sidecar_meta[n]["dtype"].startswith("Int16")
            for n in set(file_meta) & set(sidecar_meta)
        )
    except Exception as error:  # noqa: BLE001 -- one unreadable granule is a row, not the end of the run
        out["verdict"] = f"error {stage}: {type(error).__name__}: {' '.join(str(error).split())[:200]}"
        if traceback_wanted:
            import traceback

            traceback.print_exc()
    wanted = values == "all" or (values == "mismatch" and out["verdict"] != "match")
    if wanted and "store" in locals() and out.get("file_format"):
        try:
            out["hyrax_values"], out["hyrax_detail"] = check_values(
                row,
                access,
                store,
                key,
                size,
                out["file_format"],
                value_candidates(
                    out["file_format"], locals().get("file_meta") or {}, locals().get("served_names") or {}
                ),
                out.get("mismatched_variables") or [],
                value_variables,
                value_edge,
                served_names if "served_names" in locals() else {},
                file_store if "file_store" in locals() else None,
            )
        except Exception as error:  # noqa: BLE001 -- the value check failing is a result, not a crash
            out["hyrax_values"] = f"skipped: {type(error).__name__}: {' '.join(str(error).split())[:200]}"
            if traceback_wanted:
                import traceback

                traceback.print_exc()
    return out
