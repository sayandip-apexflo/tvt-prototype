"""Credential encryption, key loading and centralized redaction."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


RTSP_URL = re.compile(r"rtsps?://[^\s\"']+", re.IGNORECASE)
SENSITIVE_KEYS = {
    "authorization",
    "camera_sources",
    "ciphertext",
    "credential",
    "credentials",
    "password",
    "secret",
    "stringData",
    "token",
    "username",
}


def redact_text(value: str, limit: int = 1000) -> str:
    return RTSP_URL.sub("[REDACTED_RTSP_URL]", value).replace("\n", " ")[-limit:]


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in {item.lower() for item in SENSITIVE_KEYS} else redact(item)
            for key, item in value.items()
        }
    return value


def credential_aad(camera_id: uuid.UUID, credential_id: uuid.UUID, version: int = 1) -> bytes:
    return f"tvt-camera-credential:v{version}:{camera_id}:{credential_id}".encode()


@dataclass(frozen=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes
    key_version: int
    aad_version: int = 1


class CredentialKeyring:
    """Read-only versioned AES keyring.

    Production key files are exactly 32 raw bytes and named ``vN.key``. Tests
    may construct a keyring directly from an in-memory mapping.
    """

    def __init__(self, keys: Mapping[int, bytes], active_version: int | None = None):
        normalized = dict(keys)
        if not normalized or any(len(key) != 32 for key in normalized.values()):
            raise ValueError("credential keyring requires 32-byte AES keys")
        self._keys = normalized
        self.active_version = active_version or max(normalized)
        if self.active_version not in normalized:
            raise ValueError("active credential key version is absent")

    @classmethod
    def from_directory(cls, directory: Path) -> "CredentialKeyring":
        directory_stat = directory.stat()
        if directory_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("credential key directory must not be group/world writable")
        keys: dict[int, bytes] = {}
        for path in sorted(directory.glob("v*.key")):
            match = re.fullmatch(r"v([1-9][0-9]*)\.key", path.name)
            if not match:
                continue
            mode = path.stat().st_mode & 0o777
            if mode & 0o007 or mode & 0o020:
                raise PermissionError(f"unsafe credential key permissions: {path}")
            keys[int(match.group(1))] = path.read_bytes()
        return cls(keys)

    @classmethod
    def generate_for_test(cls, version: int = 1) -> "CredentialKeyring":
        return cls({version: AESGCM.generate_key(bit_length=256)}, version)

    def encrypt(
        self,
        camera_id: uuid.UUID,
        credential_id: uuid.UUID,
        document: Mapping[str, Any],
    ) -> EncryptedCredential:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        aad = credential_aad(camera_id, credential_id)
        return EncryptedCredential(
            AESGCM(self._keys[self.active_version]).encrypt(nonce, payload, aad),
            nonce,
            self.active_version,
        )

    def decrypt(
        self,
        camera_id: uuid.UUID,
        credential_id: uuid.UUID,
        ciphertext: bytes | None,
        nonce: bytes | None,
        key_version: int,
        aad_version: int,
    ) -> dict[str, Any]:
        if ciphertext is None or nonce is None:
            raise ValueError("credential material has been destroyed")
        try:
            key = self._keys[key_version]
        except KeyError as error:
            raise ValueError(f"credential key version {key_version} is unavailable") from error
        payload = AESGCM(key).decrypt(
            nonce, ciphertext, credential_aad(camera_id, credential_id, aad_version)
        )
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("decrypted credential document is invalid")
        return value
