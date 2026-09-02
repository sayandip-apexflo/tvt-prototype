"""Strict environment-backed settings for host services."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


def require_loopback_ip(value: str, setting: str = "TVT_LISTEN_HOST") -> str:
    """Require an explicit loopback address; hostnames are not a bind policy."""

    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"{setting} must be an explicit loopback IP address") from error
    if not address.is_loopback:
        raise ValueError(f"{setting} must bind to loopback")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg:///tvt"
    credential_key_dir: Path = Path("/etc/tvt/credential-keys")
    listen_host: str = "127.0.0.1"
    listen_port: int = 8088
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9108
    kubeconfig: str | None = None
    sync_namespace: str = "apexfabric"
    sync_worker_id: str = "tvt-edge"
    rollout_timeout: int = 180
    discovery_onvif_timeout: float = 1.0
    discovery_tcp_timeout: float = 1.0

    @classmethod
    def from_environment(cls) -> "Settings":
        port = int(os.getenv("TVT_LISTEN_PORT", "8088"))
        metrics_port = int(os.getenv("TVT_METRICS_LISTEN_PORT", "9108"))
        timeout = int(os.getenv("TVT_ROLLOUT_TIMEOUT", "180"))
        onvif_timeout = float(os.getenv("TVT_DISCOVERY_ONVIF_TIMEOUT", "1.0"))
        tcp_timeout = float(os.getenv("TVT_DISCOVERY_TCP_TIMEOUT", "1.0"))
        if not 1 <= port <= 65535:
            raise ValueError("TVT_LISTEN_PORT must be between 1 and 65535")
        if not 1 <= metrics_port <= 65535 or metrics_port == port:
            raise ValueError("TVT_METRICS_LISTEN_PORT must be a distinct valid port")
        if not 1 <= timeout <= 3600:
            raise ValueError("TVT_ROLLOUT_TIMEOUT must be between 1 and 3600")
        if onvif_timeout <= 0 or onvif_timeout > 10:
            raise ValueError("TVT_DISCOVERY_ONVIF_TIMEOUT must be in (0, 10]")
        if tcp_timeout <= 0 or tcp_timeout > 10:
            raise ValueError("TVT_DISCOVERY_TCP_TIMEOUT must be in (0, 10]")
        host = os.getenv("TVT_LISTEN_HOST", "127.0.0.1")
        require_loopback_ip(host)
        metrics_host = os.getenv("TVT_METRICS_LISTEN_HOST", "127.0.0.1")
        try:
            metrics_address = ipaddress.ip_address(metrics_host)
        except ValueError as error:
            raise ValueError("TVT_METRICS_LISTEN_HOST must be an explicit IP address") from error
        if metrics_address.is_unspecified:
            raise ValueError("TVT_METRICS_LISTEN_HOST must not expose every host interface")
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
            discovery_onvif_timeout=onvif_timeout,
            discovery_tcp_timeout=tcp_timeout,
        )
