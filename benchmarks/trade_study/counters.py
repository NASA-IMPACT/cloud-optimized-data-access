"""Counting the requests an arm makes.

A count means one of three things, and every row says which
(``request_count_kind``):

- ``client-ranges``: obspec-utils' tracer records Python calls into obstore,
  one per byte range. obstore's Rust side may coalesce, split or retry them,
  so the number of HTTP requests can differ (developmentseed/obstore#105).
- ``http-calls``: a botocore hook on an s3fs filesystem sees completed S3
  operations, which are HTTP requests.
- ``static-minimum``: nothing was counted; the number is the fewest requests
  the selection can take, worked out from the chunk grid
  (:func:`benchmarks.trade_study.measure.static_minimum_requests`).
"""

from __future__ import annotations

from typing import Any

from obspec_utils.wrappers import RequestTrace, TracingReadableStore

CLIENT_RANGES = "client-ranges"
HTTP_CALLS = "http-calls"
STATIC_MINIMUM = "static-minimum"


def traced_store(store: Any) -> tuple[TracingReadableStore, RequestTrace]:
    """``store`` wrapped so every read is recorded, and the record."""
    trace = RequestTrace()
    return TracingReadableStore(store, trace), trace


def empty_counts() -> dict[str, Any]:
    return {"request_count": 0, "bytes_requested": 0, "requests_by_method": {}, "kind": CLIENT_RANGES}


def trace_counts(trace: RequestTrace) -> dict[str, Any]:
    """What a trace recorded, in the shape every arm reports."""
    frame = trace.to_dataframe()
    if frame.empty:
        return empty_counts()
    return {
        "request_count": len(frame),
        "bytes_requested": int(frame["length"].sum()),
        "requests_by_method": {str(method): int(n) for method, n in frame["method"].value_counts().items()},
        "kind": CLIENT_RANGES,
    }


def add_counts(total: dict[str, Any], counts: dict[str, Any]) -> None:
    """Add ``counts`` into ``total`` in place."""
    total["request_count"] += counts["request_count"]
    total["bytes_requested"] += counts["bytes_requested"]
    for method, n in counts["requests_by_method"].items():
        total["requests_by_method"][method] = total["requests_by_method"].get(method, 0) + n


class S3fsCounter:
    """Counts botocore calls on an s3fs filesystem's client.

    Bytes are counted for GetObject only: a HeadObject reply carries the
    whole object's size, not bytes moved. Retries come from each reply's
    ``ResponseMetadata.RetryAttempts``; botocore's ``needs-retry`` event
    fires on successes too, so it is not used.
    """

    def __init__(self) -> None:
        self.request_count = 0
        self.bytes_requested = 0
        self.by_method: dict[str, int] = {}
        self.retries = 0

    def attach(self, filesystem: Any) -> None:
        filesystem.s3.meta.events.register("after-call", self._after_call)

    def _after_call(self, parsed: Any = None, model: Any = None, **_: Any) -> None:
        self.request_count += 1
        name = getattr(model, "name", "unknown")
        self.by_method[name] = self.by_method.get(name, 0) + 1
        if not isinstance(parsed, dict):
            return
        if name == "GetObject" and isinstance(parsed.get("ContentLength"), int):
            self.bytes_requested += parsed["ContentLength"]
        attempts = parsed.get("ResponseMetadata", {}).get("RetryAttempts")
        if isinstance(attempts, int):
            self.retries += attempts

    def counts(self) -> dict[str, Any]:
        return {
            "request_count": self.request_count,
            "bytes_requested": self.bytes_requested,
            "requests_by_method": dict(self.by_method),
            "retries": self.retries,
            "kind": HTTP_CALLS,
        }
