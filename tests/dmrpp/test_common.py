"""What the survey, the listing and the verifier share."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from dmrpp import common, list_collection, survey
from dmrpp.verify import access


def test_a_data_file_is_named_the_same_way_by_the_survey_and_the_listing():
    for name in ("a.nc", "a.NC4", "a.HDF5", "a.he5", "a.h4", "a.nc3"):
        assert common.is_data_file(name), name
    for name in ("a.nc.dmrpp", "a.tif", "readme.txt"):
        assert not common.is_data_file(name), name
    umm = {"RelatedUrls": [{"URL": "https://d.example.nasa.gov/a/b.HDF5", "Type": "GET DATA"}]}
    assert survey.data_and_sidecar(umm)[0] == "https://d.example.nasa.gov/a/b.HDF5"
    assert survey.GAPS is common.GAPS is list_collection.GAPS


def test_every_gap_falls_in_exactly_one_bucket():
    assert [common.gap_label(s) for s in (61, 3600, 86399, 86400, 40 * 86400, 400 * 86400)] == [
        "under 1 hour", "1 hour to 1 day", "1 hour to 1 day", "1 to 30 days", "30 days to 1 year", "over 1 year",
    ]  # fmt: skip
    for (_, _, high), (_, low, _) in zip(common.GAPS, common.GAPS[1:], strict=False):
        assert high == low  # no gap between buckets and no overlap


@pytest.fixture
def earthdata(monkeypatch):
    """A stand-in for earthaccess that hands out numbered credentials and counts the requests."""
    calls = []

    def get_s3_credentials(provider):
        calls.append(provider)
        n = len(calls)
        return {"accessKeyId": f"key{n}", "secretAccessKey": "secret", "sessionToken": "token"}

    def from_endpoint(endpoint):
        calls.append(endpoint)
        return {"accessKeyId": f"key{len(calls)}", "secretAccessKey": "secret", "sessionToken": "token"}

    auth = SimpleNamespace(token={"access_token": "t"}, get_s3_credentials=from_endpoint)
    fake = SimpleNamespace(login=lambda: auth, get_s3_credentials=get_s3_credentials)
    monkeypatch.setitem(sys.modules, "earthaccess", fake)
    return calls


def test_credentials_are_fetched_once_per_provider_and_renewed_before_they_expire(earthdata, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(common.time, "monotonic", lambda: now[0])
    credentials = common.ProviderCredentials()
    first = credentials.get("POCLOUD")
    assert credentials.get("POCLOUD") is first and earthdata == ["POCLOUD"]
    credentials.get("GES_DISC")
    assert earthdata == ["POCLOUD", "GES_DISC"]
    now[0] += common.CREDENTIALS_RENEW_AFTER_S + 1
    assert credentials.get("POCLOUD")["accessKeyId"] == "key3"


def test_the_verifier_rebuilds_a_buckets_store_when_its_credentials_are_renewed(earthdata, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(common.time, "monotonic", lambda: now[0])
    built = []
    monkeypatch.setattr(
        access, "s3_store", lambda bucket, region, c: built.append((bucket, c["accessKeyId"])) or object()
    )
    reach = access.Access("s3")
    row = {"data_url": "s3://bucket/dir/f.nc", "provider": "POCLOUD"}
    url, (root, store) = reach.locate(row)
    assert (url, root) == ("s3://bucket/dir/f.nc", "s3://bucket")
    assert reach.locate(row)[1][1] is store and built == [("bucket", "key1")]  # one store per bucket
    now[0] += common.CREDENTIALS_RENEW_AFTER_S + 1
    assert reach.locate(row)[1][1] is not store and built[-1] == ("bucket", "key2")  # a long run outlives one hour


def test_the_listing_reads_a_public_bucket_without_credentials(earthdata, monkeypatch):
    built = []
    monkeypatch.setattr(list_collection, "s3_store", lambda bucket, region, c: built.append((bucket, region, c)))
    list_collection.Stores("us-east-1", anonymous=True).get("noaa-goes19", "")
    list_collection.Stores("us-west-2", anonymous=False).get("podaac", "POCLOUD")
    assert built[0] == ("noaa-goes19", "us-east-1", None) and built[1][2]["accessKeyId"] == "key1"
    assert earthdata == ["POCLOUD"]


def test_swot_granules_get_credentials_from_swots_own_endpoint(earthdata, monkeypatch):
    """PO.DAAC keeps SWOT in a separate bucket that refuses the POCLOUD credentials (403)."""
    monkeypatch.setattr(access, "s3_store", lambda bucket, region, c: object())
    assert (
        common.credentials_endpoint("https://archive.swot.podaac.earthdata.nasa.gov/a/b.nc") == common.SWOT_CREDENTIALS
    )
    assert common.credentials_endpoint("https://archive.podaac.earthdata.nasa.gov/a/b.nc") is None
    reach = access.Access("s3")
    # the verifier's rows carry the HTTPS URL and the bucket comes from CMR, so the endpoint is chosen by host
    swot = {
        "provider": "POCLOUD",
        "data_url": "https://archive.swot.podaac.earthdata.nasa.gov/podaac-swot-ops-cumulus-protected/x/f.nc",
    }
    usual = {
        "provider": "POCLOUD",
        "data_url": "https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-protected/x/f.nc",
    }
    assert reach.s3_credentials(swot) is not reach.s3_credentials(usual)
    assert reach.s3_credentials(swot) is reach.s3_credentials(swot)  # fetched once
    assert earthdata == [common.SWOT_CREDENTIALS, "POCLOUD"]
    list_collection.Stores("us-west-2", anonymous=False).get("podaac-swot-ops-cumulus-protected", "POCLOUD")
    list_collection.Stores("us-west-2", anonymous=False).get("podaac-ops-cumulus-protected", "POCLOUD")
    assert earthdata[-2:] == [common.SWOT_CREDENTIALS, "POCLOUD"]  # the listing chooses it by bucket
