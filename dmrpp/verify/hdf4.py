"""HDF4 files: corrections to kerchunk's decoder, without which the file side would be the wrong one."""

from __future__ import annotations

from typing import Any

HDF4_MAGIC = b"\x0e\x03\x13\x01"
HDF4_UNSUPPORTED: list[str] = []


def patch_hdf4_decoding() -> None:
    """Fill gaps in kerchunk 0.2.10's HDF4 decoder and VirtualiZarr 2.8.0's use of it, once per process.

    Each was found on real granules; without them the file side, not the
    sidecar, would be wrong:

    - Floats: kerchunk lists number types 5 and 6 as native-order ``f4``/``f8``.
      HDF4 stores them big-endian, so every float decoded as garbage
      (MOD11_L2 latitude -7.5e12 instead of -15.99, Hyrax agreeing with the swap).
    - Contiguous datasets: kerchunk gives a plain, uncompressed SDS no byte
      reference at all (``KeyError: 'data'``, e.g. AIRS); it is one chunk at
      the tag's offset and length.
    - Compression: kerchunk records every compressed chunk as DEFLATE without
      reading the type, and VirtualiZarr marks every variable zlib. The real
      type is read here; uncompressed variables get no codec, and variables
      with another codec (SZIP, RLE, ...) or with linked blocks are listed in
      ``HDF4_UNSUPPORTED`` and left out of the comparison.
    - Version tag: kerchunk reads the library version string as the tag's
      length minus 10, but three 4-byte integers come first, so it read 2 bytes
      past the tag and failed to decode them (``UnicodeDecodeError``, CALIPSO).
    - Dimensions: VirtualiZarr names at most two (``name_x``, ``name_y``), so
      any 3-D variable failed the whole file; extra dimensions get names here.
    """
    import kerchunk.hdf4 as k
    import numpy as np
    import virtualizarr.parsers.hdf4 as v

    if getattr(k, "_verify_dmrpp_patched", False):
        return
    k._verify_dmrpp_patched = True
    k.dtypes.update({5: ">f4", 6: ">f8"})

    original_dec = k.HDF4ToZarr._dec

    def _dec(self: Any, tag: str, ref: int) -> dict[str, Any]:
        info = original_dec(self, tag, ref)
        if tag == "SD" and "data" not in info and not info.get("extended") and info.get("length"):
            info["data"] = ("NONE", info["offset"], info["length"])
        return info

    def _dec_comp(self: Any) -> tuple[str, int, int]:
        self.f.seek(6, 1)  # version, uncompressed length
        data_ref = self.read_int(2)
        self.read_int(2)  # model type
        ctype = k.comp.get(self.read_int(2), "UNKNOWN")
        tag = self.tags[("COMPRESSED", data_ref)]
        return ctype, tag["offset"], tag["length"]

    def _dec_version(self: Any, info: dict[str, Any]) -> dict[str, Any]:
        numbers = {"major": self.read_int(4), "minor": self.read_int(4), "release": self.read_int(4)}
        return {**numbers, "string:": self.f.read(info["length"] - 12).split(b"\x00", 1)[0].decode("latin-1")}

    k.decoders["VERSION"] = _dec_version
    k.HDF4ToZarr._dec = _dec
    k.HDF4ToZarr._dec_comp = _dec_comp
    original_array = v._manifestarray

    def _manifestarray(name: str, variable: dict[str, Any], url: str, fs_root: str) -> Any:
        refs = variable.get("refs", [])
        if any(not isinstance(r, list | tuple) or len(r) < 3 or not isinstance(r[1], int | np.integer) for r in refs):
            HDF4_UNSUPPORTED.append(f"{name}: linked blocks")
            return original_array(name, {**variable, "refs": []}, url, fs_root)
        codecs = {r[3] if len(r) > 3 else "NONE" for r in refs}
        if codecs - {"DEFLATE", "NONE"}:
            HDF4_UNSUPPORTED.append(f"{name}: {'/'.join(sorted(codecs - {'DEFLATE', 'NONE'}))}")
        if refs and codecs == {"NONE"}:
            saved, v._ZLIB = v._ZLIB, None
            try:
                return original_array(name, variable, url, fs_root)
            finally:
                v._ZLIB = saved
        return original_array(name, variable, url, fs_root)

    v._manifestarray = _manifestarray
    original_from_refs = v.from_kerchunk_refs

    def from_kerchunk_refs(zarray: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        names, shape = zarray.get("dimension_names") or [], zarray.get("shape") or ()
        if names != ["0"] and len(names) != len(shape):
            zarray = {
                **zarray,
                "dimension_names": [
                    *names,
                    *(f"{names[0].rsplit('_', 1)[0]}_d{i}" for i in range(len(names), len(shape))),
                ][: len(shape)],
            }
        return original_from_refs(zarray, *args, **kwargs)

    v.from_kerchunk_refs = from_kerchunk_refs
