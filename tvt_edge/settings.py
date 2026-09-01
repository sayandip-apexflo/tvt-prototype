"""Strict environment-backed settings for host services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg:///tvt"
    credential_key_dir: Path = Path("/etc/tvt/credential-keys")
    listen_host: str = "127.0.0.1"
    listen_port: int = 8088
    kubeconfig: str | None = None
    sync_namespace: str = "apexfabric"
    sync_worker_id: str = "tvt-edge"
    rollout_timeout: int = 180

    @classmethod
    def from_environment(cls) -> "Settings":
        port = int(os.getenv("TVT_LISTEN_PORT", "8088"))
        timeout = int(os.getenv("TVT_ROLLOUT_TIMEOUT", "180"))
        if not 1 <= port <= 65535:
            raise ValueError("TVT_LISTEN_PORT must be between 1 and 65535")
        if not 1 <= timeout <= 3600:
            raise ValueError("TVT_ROLLOUT_TIMEOUT must be between 1 and 3600")
        host = os.getenv("TVT_LISTEN_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the Slice 3 API must bind to loopback")
        return cls(
            database_url=os.getenv("TVT_DATABASE_URL", cls.database_url),
            credential_key_dir=Path(
                os.getenv("TVT_CREDENTIAL_KEY_DIR", str(cls.credential_key_dir))
            ),
            listen_host=host,
            listen_port=port,
            kubeconfig=os.getenv("TVT_KUBECONFIG") or None,
            sync_namespace=os.getenv("TVT_SYNC_NAMESPACE", "apexfabric"),
            sync_worker_id=os.getenv("TVT_SYNC_WORKER_ID", "tvt-edge"),
            rollout_timeout=timeout,
        )
