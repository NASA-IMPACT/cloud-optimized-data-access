"""The value check: does Hyrax return the numbers a direct read of the file returns?"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dmrpp.verify.access import granule_identity

if TYPE_CHECKING:
    from dmrpp.verify.access import Access

HYRAX = "https://opendap.earthdata.nasa.gov"
HYRAX_TIMEOUT_S = 120  # per request; a slow or failing Hyrax answer is recorded, not waited on


def value_candidates(
    file_format: str, file_meta: dict[str, dict[str, Any]], served_names: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """The variables the value check may ask Hyrax for: for HDF4 only those matched to a sidecar name, since Hyrax knows no other."""
    return {n: m for n, m in file_meta.items() if n in served_names} if file_format == "HDF4" else file_meta


def slab(shape: tuple[int, ...], edge: int) -> tuple[slice, ...]:
    """A block of at most ``edge`` elements per dimension from the middle of an array.

    The value check reads this block, not the whole variable: it is one small
    request to Hyrax and a few chunks from the file, however large the variable is."""
    return tuple(slice((n - min(n, edge)) // 2, (n - min(n, edge)) // 2 + min(n, edge)) for n in shape)


def constraint(name: str, block: tuple[slice, ...]) -> str:
    """The DAP4 constraint expression for ``block`` of ``name``: /v[10:1:19][0:1:4]."""
    return name + "".join(f"[{s.start}:1:{s.stop - 1}]" for s in block)


def direct_values(
    store: Any,
    key: str,
    size: int,
    file_format: str,
    blocks: dict[str, tuple[slice, ...]],
    file_store: Any = None,
) -> dict[str, Any]:
    """The stored (unscaled) values of each variable's block, read from the file itself.

    HDF4 has no reader here independent of the index being checked, so its
    blocks are read through the HDF4 parser's own store: kerchunk's offsets,
    decoded by zarr. That is still independent of the sidecar."""
    import io

    import numpy as np

    if file_format == "HDF4":
        import zarr

        group = zarr.open_group(file_store, mode="r", zarr_format=3)
        return {name: np.asarray(group[name.lstrip("/")][block]) for name, block in blocks.items()}

    if file_format == "HDF5":
        import h5py
        from obspec_utils.readers import BlockStoreReader

        with BlockStoreReader(store, key, block_size=4 * 2**20) as reader, h5py.File(reader, "r") as f:
            return {name: np.asarray(f[name][block]) for name, block in blocks.items()}
    import obstore
    from scipy.io import netcdf_file

    if size > 256 * 2**20:
        raise ValueError("netCDF classic file over 256 MiB: not read whole")
    with netcdf_file(io.BytesIO(bytes(obstore.get(store, key).bytes())), "r", mmap=False, maskandscale=False) as f:
        return {name: np.array(f.variables[name.lstrip("/")][block]) for name, block in blocks.items()}


def hyrax_values(
    token: str, concept_id: str, granule_ur: str, name: str, block: tuple[slice, ...] | None = None
) -> Any:
    """One variable's stored values (or one block of them) as Hyrax serves them, in a
    netCDF-4 response. Raises RuntimeError with Hyrax's message."""
    import re
    import tempfile
    import urllib.error

    import h5py
    import numpy as np

    url = f"{HYRAX}/collections/{concept_id}/granules/{urllib.parse.quote(granule_ur)}.dap.nc4?dap4.ce={urllib.parse.quote(constraint(name, block) if block else name)}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": "verify-dmrpp/0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HYRAX_TIMEOUT_S) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        text = re.sub(r"<[^>]+>|\s+", " ", error.read().decode(errors="replace"))
        message = re.search(r"associated with your request was:\s*(.*?)\s*If you think", text)
        if error.code == 504:
            raise RuntimeError("504 gateway timeout") from None
        raise RuntimeError(f"{error.code} {message.group(1) if message else ' '.join(text.split())[-200:]}") from None
    except (TimeoutError, urllib.error.URLError) as error:
        raise RuntimeError(f"no answer within {HYRAX_TIMEOUT_S} s ({type(error).__name__})") from None
    if not body.startswith(b"\x89HDF"):
        raise RuntimeError(f"not a netCDF-4 response: {body[:80]!r}")
    with tempfile.NamedTemporaryFile(suffix=".nc4") as scratch:
        scratch.write(body)
        scratch.flush()
        with h5py.File(scratch.name, "r") as f:
            return np.asarray(f[name][...])


def same_values(a: Any, b: Any) -> bool:
    import numpy as np

    if a.shape != b.shape:
        return False
    if a.dtype.kind in "fc" or b.dtype.kind in "fc":
        return bool(np.array_equal(a, b, equal_nan=True))
    return bool(np.array_equal(a, b))


def choose_variables(
    file_meta: dict[str, dict[str, Any]], mismatched: list[str], limit: int, max_bytes: int | None = None
) -> list[str]:
    """Numeric arrays: the mismatched ones first, then the largest. ``max_bytes``
    leaves out larger variables; None keeps them all (the check reads a block, not the whole)."""
    import math
    import re

    def nbytes(meta: dict[str, Any]) -> int:
        bits = re.search(r"(\d+)", meta["dtype"])
        return math.prod(meta["shape"]) * (int(bits.group(1)) // 8 if bits else 8)

    numeric = {
        n: nbytes(m)
        for n, m in file_meta.items()
        if re.match(r"(U?Int|Float)\d+", m["dtype"])
        and m["shape"]
        and 0 < nbytes(m)
        and (max_bytes is None or nbytes(m) <= max_bytes)
    }
    ordered = [n for n in mismatched if n in numeric] + sorted(
        (n for n in numeric if n not in mismatched), key=lambda n: -numeric[n]
    )
    return ordered[:limit]


def check_values(
    row: dict[str, str],
    access: Access,
    store: Any,
    key: str,
    size: int,
    file_format: str,
    file_meta: dict[str, dict[str, Any]],
    mismatched: list[str],
    limit: int,
    edge: int,
    served_names: dict[str, str] | None = None,
    file_store: Any = None,
) -> tuple[str, str]:
    """(hyrax_values, detail): compare what Hyrax serves with a direct read, one block per variable.

    ``served_names`` maps a file-side name to the name Hyrax knows it by, where they differ (HDF4)."""
    names = choose_variables(file_meta, mismatched, limit)
    if not names:
        return "skipped: no numeric variable to compare", ""
    concept_id, granule_ur = granule_identity(row)
    blocks = {name: slab(file_meta[name]["shape"], edge) for name in names}
    truth = direct_values(store, key, size, file_format, blocks, file_store)
    results = {}
    for name in names:
        try:
            served = hyrax_values(
                access.token(), concept_id, granule_ur, (served_names or {}).get(name, name), blocks[name]
            )
            results[name] = (
                "identical"
                if same_values(
                    served.reshape(truth[name].shape) if served.size == truth[name].size else served, truth[name]
                )
                else "DIFFERENT"
            )
        except RuntimeError as error:
            results[name] = f"hyrax error: {error}"
    detail = "; ".join(f"{name}: {result}" for name, result in results.items())
    if any(r == "DIFFERENT" for r in results.values()):
        return "DIFFERENT", detail
    errors = [r for r in results.values() if r.startswith("hyrax error")]
    if errors:
        return errors[0][:160], detail
    return "identical", detail
