"""Where a granule is and how to reach it: CMR lookups, object stores and credentials."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from dmrpp.common import CMR, ProviderCredentials, credentials_endpoint, s3_store


def cmr_items(query: dict[str, Any], attempts: int = 4) -> list[dict[str, Any]]:
    """The granules CMR returns for ``query``. A timeout or a failed connection
    is retried with a growing pause: the verifier asks CMR once per granule, and
    one slow answer in a run of thousands should not cost a granule its verdict."""
    url = f"{CMR}?{urllib.parse.urlencode(query)}"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return json.load(response).get("items", [])
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429 or attempt == attempts - 1:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(2**attempt)
    return []


def s3_url(row: dict[str, str]) -> str:
    """The granule's s3:// link from CMR; the HTTPS URL's first path segment as the bucket if CMR has none."""
    name = row["data_url"].rsplit("/", 1)[-1]
    if row.get("granule_ur") and row.get("concept_id"):
        for item in cmr_items({"collection_concept_id": row["concept_id"], "granule_ur": row["granule_ur"]}):
            for related in item["umm"].get("RelatedUrls", []):
                if related.get("URL", "").startswith("s3://") and related["URL"].endswith(name):
                    return related["URL"]
    parsed = urllib.parse.urlparse(row["data_url"])
    return f"s3:/{parsed.path}"


def granule_identity(row: dict[str, str]) -> tuple[str, str]:
    """(collection concept id, GranuleUR) for Hyrax's URL, from the row or from CMR by file name."""
    if row.get("concept_id") and row.get("granule_ur"):
        return row["concept_id"], row["granule_ur"]
    name = row["data_url"].rsplit("/", 1)[-1]
    scope = (
        {"collection_concept_id": row["concept_id"]}
        if row.get("concept_id")
        else {"provider": row["provider"]}
        if row.get("provider")
        else {}
    )
    items: list[dict[str, Any]] = []
    for candidate in (
        name,
        name.rsplit(".", 1)[0],
    ):  # CMR knows a granule by its file name or by the name without the extension
        items = cmr_items({"readable_granule_name": candidate, "page_size": 5, **scope})
        if items:
            break
    if not items:
        raise LookupError(f"CMR has no granule named {name}")
    return items[0]["meta"]["collection-concept-id"], items[0]["umm"]["GranuleUR"]


class Access:
    """Object stores per bucket or host, built once."""

    def __init__(self, transport: str) -> None:
        import earthaccess

        self.transport = transport
        self.auth = earthaccess.login()
        self._stores: dict[str, Any] = {}
        self._s3_stores: dict[
            str, tuple[dict[str, str], Any]
        ] = {}  # bucket → (the credentials it was built with, store)
        self._credentials = ProviderCredentials()

    def token(self) -> str:
        """An Earthdata bearer token, for Hyrax."""
        token = getattr(self.auth, "token", None) or {}
        if not token.get("access_token"):
            raise RuntimeError("no Earthdata token available for Hyrax; set EARTHDATA_TOKEN")
        return token["access_token"]

    def locate(self, row: dict[str, str]) -> tuple[str, Any]:
        """(the data file's URL on this transport, the store that serves it)."""
        from obstore.store import HTTPStore

        if self.transport == "https":
            url = row["data_url"]
            root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
            if root not in self._stores:
                token = self.auth.token["access_token"]
                self._stores[root] = HTTPStore(
                    root, client_options={"default_headers": {"authorization": f"Bearer {token}"}}
                )
            return url, (root, self._stores[root])
        url = row["data_url"] if row["data_url"].startswith("s3://") else s3_url(row)
        bucket = urllib.parse.urlparse(url).netloc
        credentials = self.s3_credentials(row)
        held = self._s3_stores.get(bucket)
        if held is None or held[0] is not credentials:  # first use, or the credentials were renewed
            held = self._s3_stores[bucket] = (credentials, s3_store(bucket, "us-west-2", credentials))
        return url, (f"s3://{bucket}", held[1])

    def s3_credentials(self, row: dict[str, str]) -> dict[str, str]:
        """Temporary S3 credentials for the row's provider, or for its bucket's own
        endpoint where it has one; renewed before they expire."""
        return self._credentials.get(row.get("provider") or "", credentials_endpoint(row.get("data_url") or ""))

    def fsspec_options(self, row: dict[str, str], url: str) -> dict[str, Any]:
        """Storage options for parsers that open the file through fsspec (HDF4Parser, via kerchunk)."""
        # kerchunk reads an HDF4 file's tags with many small seeks: cache whole 4 MiB blocks
        if url.startswith("s3://"):
            c = self.s3_credentials(row)
            return {
                "key": c["accessKeyId"],
                "secret": c["secretAccessKey"],
                "token": c["sessionToken"],
                "client_kwargs": {"region_name": "us-west-2"},
                "default_block_size": 4 * 2**20,
                "default_cache_type": "blockcache",
            }
        return {
            "headers": {"Authorization": f"Bearer {self.token()}"},
            "block_size": 4 * 2**20,
            "cache_type": "blockcache",
        }
