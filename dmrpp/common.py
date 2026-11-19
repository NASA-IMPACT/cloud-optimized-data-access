"""What the survey, the listing and the verifier share, so their outputs can be compared."""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

CMR = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
# Matched against the lower-cased name, so ``.HDF5`` and ``.NC4`` count too.
DATA_SUFFIXES = (".nc", ".nc4", ".nc3", ".h5", ".hdf", ".hdf5", ".he5", ".h4", ".he4")
SIDECAR_SUFFIX = ".dmrpp"
DAY = 86400
# (label, at least, less than) in seconds: how much newer a data file is than its sidecar
GAPS = (
    ("under 1 hour", 0, 3600),
    ("1 hour to 1 day", 3600, DAY),
    ("1 to 30 days", DAY, 30 * DAY),
    ("30 days to 1 year", 30 * DAY, 365 * DAY),
    ("over 1 year", 365 * DAY, float("inf")),
)
CREDENTIALS_RENEW_AFTER_S = 45 * 60  # a DAAC's temporary S3 credentials last an hour


def is_data_file(name: str) -> bool:
    """True for a name with an HDF or netCDF suffix, in any case."""
    return name.lower().endswith(DATA_SUFFIXES)


def gap_label(seconds: float) -> str:
    """The :data:`GAPS` bucket a gap falls in."""
    return next(
        (label for label, low, high in GAPS if low <= seconds < high), GAPS[0][0] if seconds < 0 else GAPS[-1][0]
    )


# Buckets whose credentials do not come from their provider's usual endpoint, by the HTTPS
# host that serves them and by bucket. PO.DAAC keeps SWOT in a bucket of its own: the POCLOUD
# credentials are refused there (403), and CMR names this endpoint in each SWOT collection's
# DirectDistributionInformation.
SWOT_CREDENTIALS = "https://archive.swot.podaac.earthdata.nasa.gov/s3credentials"
CREDENTIAL_ENDPOINTS = {
    "archive.swot.podaac.earthdata.nasa.gov": SWOT_CREDENTIALS,
    "podaac-swot-ops-cumulus-protected": SWOT_CREDENTIALS,
}


def credentials_endpoint(*locations: str) -> str | None:
    """The credentials endpoint to use instead of the provider's, for a bucket name or a URL's host."""
    for location in locations:
        host = urllib.parse.urlparse(location).netloc if "://" in location else location
        if host in CREDENTIAL_ENDPOINTS:
            return CREDENTIAL_ENDPOINTS[host]
    return None


class ProviderCredentials:
    """Temporary S3 credentials per CMR provider, or per credentials endpoint
    where a bucket has its own: fetched once, and renewed before their hour is up."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[float, dict[str, str]]] = {}

    def get(self, provider: str, endpoint: str | None = None) -> dict[str, str]:
        import earthaccess

        key = endpoint or provider
        fetched, credentials = self._held.get(key, (0.0, {}))
        if not credentials or time.monotonic() - fetched > CREDENTIALS_RENEW_AFTER_S:
            auth = earthaccess.login()
            if endpoint:
                credentials = auth.get_s3_credentials(endpoint=endpoint)
            else:
                credentials = earthaccess.get_s3_credentials(provider=provider)
            self._held[key] = (time.monotonic(), credentials)
        return credentials


def s3_store(bucket: str, region: str = "us-west-2", credentials: dict[str, str] | None = None) -> Any:
    """An obstore S3 store for ``bucket``; unsigned requests when no credentials are given."""
    from obstore.store import S3Store

    if credentials is None:
        return S3Store(bucket, region=region, skip_signature=True)
    return S3Store(
        bucket,
        region=region,
        access_key_id=credentials["accessKeyId"],
        secret_access_key=credentials["secretAccessKey"],
        token=credentials["sessionToken"],
    )
