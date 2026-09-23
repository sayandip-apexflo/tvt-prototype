"""Strict environment-backed settings for host services."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def require_loopback_ip(value: str, setting: str = "TVT_LISTEN_HOST") -> str:
    """Require an explicit loopback address; hostnames are not a bind policy."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"{setting} must be an explicit loopback IP address") from error
    if not address.is_loopback:
        raise ValueError(f"{setting} must bind to loopback")
    return value


def require_loopback_http_url(value: str, setting: str) -> str:
    """Require an explicit loopback HTTP URL with no path/query/credentials.

    Same policy tvt_edge/reporting/settings.py applies to TVT_REPORT_APEX_URL --
    both reach the same apexfabric-control HTTP API on the loopback interface.
    """

    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError(f"{setting} must be an explicit loopback HTTP URL")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{setting} must contain only scheme, host, and port")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg:///tvt"
    credential_key_dir: Path = Path("/etc/tvt/credential-keys")
    listen_host: str = "127.0.0.1"
    listen_port: int = 8089
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9108
    kubeconfig: str | None = None
    sync_namespace: str = "apexfabric"
    sync_worker_id: str = "tvt-edge"
    rollout_timeout: int = 180
    runtime_reload_timeout: int = 15
    apex_url: str = "http://127.0.0.1:8088"
    enrollment_reconcile_interval_seconds: float = 1.0

    @classmethod
    def from_environment(cls) -> "Settings":
        port = int(os.getenv("TVT_LISTEN_PORT", "8089"))
        metrics_port = int(os.getenv("TVT_METRICS_LISTEN_PORT", "9108"))
        timeout = int(os.getenv("TVT_ROLLOUT_TIMEOUT", "180"))
        runtime_reload_timeout = int(os.getenv("TVT_RUNTIME_RELOAD_TIMEOUT", "15"))
        enrollment_interval = float(
            os.getenv("TVT_ENROLLMENT_RECONCILE_INTERVAL_SECONDS", "1.0")
        )
        if not 1 <= port <= 65535:
            raise ValueError("TVT_LISTEN_PORT must be between 1 and 65535")
        if not 1 <= metrics_port <= 65535 or metrics_port == port:
            raise ValueError("TVT_METRICS_LISTEN_PORT must be a distinct valid port")
        if not 1 <= timeout <= 3600:
            raise ValueError("TVT_ROLLOUT_TIMEOUT must be between 1 and 3600")
        if not 0 <= runtime_reload_timeout <= 60:
            raise ValueError("TVT_RUNTIME_RELOAD_TIMEOUT must be between 0 and 60")
        if not 0.5 <= enrollment_interval <= 60:
            raise ValueError(
                "TVT_ENROLLMENT_RECONCILE_INTERVAL_SECONDS must be between 0.5 and 60"
            )
        host = os.getenv("TVT_LISTEN_HOST", "127.0.0.1")
        require_loopback_ip(host)
        metrics_host = os.getenv("TVT_METRICS_LISTEN_HOST", "127.0.0.1")
        try:
            metrics_address = ipaddress.ip_address(metrics_host)
        except ValueError as error:
            raise ValueError("TVT_METRICS_LISTEN_HOST must be an explicit IP address") from error
        if metrics_address.is_unspecified:
            raise ValueError("TVT_METRICS_LISTEN_HOST must not expose every host interface")
        apex_url = require_loopback_http_url(
            os.getenv("TVT_APEX_URL", cls.apex_url).rstrip("/"), "TVT_APEX_URL"
        )
        return cls(
            database_url=os.getenv("TVT_DATABASE_URL", cls.database_url),
            credential_key_dir=Path(
                os.getenv("TVT_CREDENTIAL_KEY_DIR", str(cls.credential_key_dir))
            ),
            listen_host=host,
            listen_port=port,
            metrics_host=metrics_host,
            metrics_port=metrics_port,
            kubeconfig=os.getenv("TVT_KUBECONFIG") or None,
            sync_namespace=os.getenv("TVT_SYNC_NAMESPACE", "apexfabric"),
            sync_worker_id=os.getenv("TVT_SYNC_WORKER_ID", "tvt-edge"),
            rollout_timeout=timeout,
            runtime_reload_timeout=runtime_reload_timeout,
            apex_url=apex_url,
            enrollment_reconcile_interval_seconds=enrollment_interval,
        )
