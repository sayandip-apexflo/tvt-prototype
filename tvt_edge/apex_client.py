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

    def post(self, path: str, body: dict[str, Any]) -> ApexResponse:
        url = f"{self.apex_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Accept": "*/*", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return ApexResponse(
                    status=response.status,
                    body=response.read(),
                    content_type=response.headers.get_content_type(),
                )
        except urllib.error.HTTPError as error:
            body_bytes = error.read()
            content_type = (
                error.headers.get_content_type() if error.headers else "application/json"
            )
            return ApexResponse(status=error.code, body=body_bytes, content_type=content_type)
        except urllib.error.URLError as error:
            raise ApexUnavailableError(str(error.reason)) from error
        except TimeoutError as error:
            raise ApexUnavailableError("apex request timed out") from error

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST and parse a JSON body. A 5xx (or unreachable) response raises
        ApexUnavailableError; apex maps a rejected request (bad input,
        unknown ID) to 400/404, which is surfaced as ValueError so callers
        can return it to the operator instead of treating it as an outage."""

        result = self.post(path, body)
        parsed: dict[str, Any] = {}
        if result.body:
            try:
                parsed = json.loads(result.body)
            except json.JSONDecodeError:
                parsed = {}
        if result.status >= 500:
            raise ApexUnavailableError(f"apex returned HTTP {result.status} for {path}")
        if result.status >= 400:
            raise ValueError(str(parsed.get("error") or f"apex rejected request ({result.status})"))
        return parsed

    def attendance_report(
        self, *, person_id: str | None = None, date: str | None = None
    ) -> dict[str, Any]:
        return self.get_json("/api/reports/attendance", {"person_id": person_id, "date": date})

    def vehicle_traffic_report(
        self, *, date: str | None = None, gate: str | None = None
    ) -> dict[str, Any]:
        return self.get_json("/api/reports/vehicle-traffic", {"date": date, "gate": gate})

    def recent_events(self, deployment_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Non-sensitive event envelopes for correlation only. Callers must
        read only the outer envelope fields (event_id, event_type,
        camera_id, application, occurred_at) -- payload["payload"] carries
        embeddings and must never be logged, stored, or returned to a
        browser (see tvt_edge/enrollment.py)."""

        result = self.get_json(
            "/api/telemetry/events", {"deployment_id": deployment_id, "limit": str(limit)}
        )
        return list(result.get("events") or [])

    def list_persons(self, *, status: str | None = None) -> list[dict[str, Any]]:
        result = self.get_json("/api/persons", {"status": status})
        return list(result.get("persons") or [])

    def rename_person(self, person_id: str, display_name: str) -> None:
        self.post_json("/api/persons/rename", {"person_id": person_id, "display_name": display_name})
