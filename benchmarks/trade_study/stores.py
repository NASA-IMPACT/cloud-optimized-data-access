"""Object stores: building one from a plain description, and reading through it.

Arms read granules through obstore. A store is described by a *spec*, a plain
dict, because the tuned arm hands it to worker processes and a live store
cannot be pickled:

- ``{"kind": "s3", "bucket", "region", "prefix", "credentials"}``; anonymous
  when ``credentials`` is missing or None;
- ``{"kind": "http", "base_url", "client_options"}``;
- ``{"kind": "local"}``, optionally with a ``prefix``. Tests use this one.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse


def build_store(spec: dict[str, Any]) -> Any:
    """The obstore store a spec describes."""
    if spec["kind"] == "local":
        from obstore.store import LocalStore

        return LocalStore(prefix=spec["prefix"]) if spec.get("prefix") else LocalStore()
    if spec["kind"] == "http":
        from obstore.store import HTTPStore

        return HTTPStore(spec["base_url"], client_options=spec.get("client_options") or None)
    from obstore.store import S3Store

    common = {"prefix": spec.get("prefix") or None, "region": spec["region"]}
    credentials = spec.get("credentials")
    if not credentials:
        return S3Store(spec["bucket"], skip_signature=True, **common)
    return S3Store(
        spec["bucket"],
        access_key_id=credentials["accessKeyId"],
        secret_access_key=credentials["secretAccessKey"],
        token=credentials["sessionToken"],
        **common,
    )


def store_path(spec: dict[str, Any], url: str) -> str:
    """A granule's URL → the path of that object inside the store ``spec`` builds."""
    if spec["kind"] == "local":
        return urlparse(url).path
    if spec["kind"] == "http":
        base = spec["base_url"].rstrip("/") + "/"
        if url.startswith(base):
            return url[len(base) :]
        parsed = urlparse(url)  # an s3:// URL: the endpoint serves it as bucket/key
        return f"{parsed.netloc}{parsed.path}"
    path = urlparse(url).path.lstrip("/")
    prefix = spec.get("prefix") or ""
    return path[len(prefix) :].lstrip("/") if prefix and path.startswith(prefix) else path


async def fetch_object(store: Any, path: str, *, part_bytes: int = 16 * 2**20, concurrency: int = 16) -> bytearray:
    """The whole object in memory: one HEAD, then ``part_bytes`` ranges with
    ``concurrency`` in flight. Uses the store's own methods, so a tracing
    wrapper counts every request. Network."""
    size = (await store.head_async(path))["size"]
    data = bytearray(size)
    in_flight = asyncio.Semaphore(concurrency)

    async def fetch_part(start: int) -> None:
        async with in_flight:
            part = memoryview(await store.get_range_async(path, start=start, end=min(start + part_bytes, size)))
            data[start : start + len(part)] = part

    await asyncio.gather(*(fetch_part(start) for start in range(0, size, part_bytes)))
    return data


def icechunk_config(repo: Any, settings: dict[str, Any] | None) -> Any:
    """The repository's config with the Icechunk tuning factors in ``settings`` applied:
    ``icechunk_concurrency`` (partial-value and request concurrency) and
    ``icechunk_decodes`` (concurrent manifest decodes)."""
    config = repo.config
    settings = settings or {}
    if "icechunk_concurrency" in settings:
        config.get_partial_values_concurrency = int(settings["icechunk_concurrency"])
        config.max_concurrent_requests = int(settings["icechunk_concurrency"])
    if "icechunk_decodes" in settings:
        config.max_concurrent_decodes = int(settings["icechunk_decodes"])
    return config
