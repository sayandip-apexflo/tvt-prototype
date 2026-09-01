"""Minimal in-cluster Kubernetes API client."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ApiError(RuntimeError):
    pass


class KubeApi:
    def __init__(self, base_url: str | None = None, token_path: Path | None = None, ca_path: Path | None = None):
        host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.base_url = (base_url or f"https://{host}:{port}").rstrip("/")
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        token_path = token_path or service_account / "token"
        ca_path = ca_path or service_account / "ca.crt"
        self.token = token_path.read_text(encoding="utf-8").strip()
        self.context = ssl.create_default_context(cafile=str(ca_path))

    def request(self, method: str, path: str, payload: Any | None = None, content_type: str = "application/json") -> Any:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=15) as response:
                data = response.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise ApiError(f"Kubernetes API {method} {path}: HTTP {error.code}: {detail[-2000:]}") from error

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, payload)

    def patch(self, path: str, payload: Any, content_type: str = "application/merge-patch+json") -> Any:
        return self.request("PATCH", path, payload, content_type)
