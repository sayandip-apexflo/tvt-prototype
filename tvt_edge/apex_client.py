"""Thin loopback HTTP client for apexfabric-control's read-only API.

The management API's camera-snapshot and report proxies both talk to the
same loopback service (`apex_url`, default `http://127.0.0.1:8088` -- the
same address `tvt_edge/reporting/settings.py` already uses to poll
`/api/telemetry/events`). This is the one place that builds those requests
so every caller agrees on timeouts and error handling.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode


class ApexUnavailableError(RuntimeError):
    """apexfabric-control could not be reached at all (connection refused,
    DNS failure, timeout) -- distinct from a well-formed non-200 response."""


@dataclass(frozen=True)
class ApexResponse:
    status: int
    body: bytes
    content_type: str


class ApexClient:
    def __init__(self, apex_url: str, timeout: float = 10.0) -> None:
        self.apex_url = apex_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, query: dict[str, str | None] | None = None) -> ApexResponse:
        url = f"{self.apex_url}{path}"
        if query:
            filtered = {key: value for key, value in query.items() if value is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered)}"
        request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return ApexResponse(
                    status=response.status,
                    body=response.read(),
                    content_type=response.headers.get_content_type(),
                )
        except urllib.error.HTTPError as error:
            body = error.read()
            content_type = (
                error.headers.get_content_type() if error.headers else "application/json"
            )
            return ApexResponse(status=error.code, body=body, content_type=content_type)
        except urllib.error.URLError as error:
            raise ApexUnavailableError(str(error.reason)) from error
        except TimeoutError as error:
            raise ApexUnavailableError("apex request timed out") from error

    def get_json(self, path: str, query: dict[str, str | None] | None = None) -> dict[str, Any]:
        """GET and parse a JSON body, raising ApexUnavailableError on any
        non-200 response (not just a connection failure) -- callers that
        need to distinguish should use `get` directly."""

        result = self.get(path, query)
        if result.status != 200:
            raise ApexUnavailableError(f"apex returned HTTP {result.status} for {path}")
        return json.loads(result.body)

    def attendance_report(
        self, *, person_id: str | None = None, date: str | None = None
    ) -> dict[str, Any]:
        return self.get_json("/api/reports/attendance", {"person_id": person_id, "date": date})

    def vehicle_traffic_report(
        self, *, date: str | None = None, gate: str | None = None
    ) -> dict[str, Any]:
        return self.get_json("/api/reports/vehicle-traffic", {"date": date, "gate": gate})
