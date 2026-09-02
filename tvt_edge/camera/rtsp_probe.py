"""Minimal RTSP endpoint probe used by the discovery/validation worker."""

from __future__ import annotations

import base64
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any

from tvt_edge.security import redact


SUPPORTED_CODECS = {
    "h264",
    "h265",
    "vp8",
    "vp9",
    "mpeg4",
    "mjpeg",
    "av1",
}


@dataclass(frozen=True)
class ProbeResult:
    result_code: str
    safe_result: dict[str, Any]


def _build_authorization(username: str | None, password: str | None) -> str | None:
    if not username and not password:
        return None
    payload = f"{username or ''}:{password or ''}".encode()
    return base64.b64encode(payload).decode("ascii")


def _parse_status_line(payload: bytes) -> int:
    first = payload.split(b"\r\n", 1)[0].decode(errors="replace")
    parts = first.split()
    if len(parts) < 2:
        raise ValueError("Malformed RTSP status line")
    try:
        return int(parts[1])
    except ValueError as error:
        raise ValueError("Malformed RTSP status line") from error


def _parse_headers(payload: bytes) -> dict[str, str]:
    lines = payload.decode(errors="replace").split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _read_response(sock: socket.socket, *, timeout: float) -> tuple[int, dict[str, str], bytes]:
    sock.settimeout(timeout)
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise ValueError("PROBE_INTERNAL_ERROR")
        buffer += chunk
    header_blob, body = buffer.split(b"\r\n\r\n", 1)
    status = _parse_status_line(header_blob)
    headers = _parse_headers(header_blob)
    expected = int(headers.get("content-length", "0") or "0")
    while len(body) < expected:
        body += sock.recv(4096)
    return status, headers, body[:expected]


def _send_request(sock: socket.socket, request: str, *, timeout: float) -> tuple[int, dict[str, str], bytes]:
    sock.sendall(request.encode("ascii", errors="replace"))
    return _read_response(sock, timeout=timeout)


def _build_request(
    method: str,
    path: str,
    host: str,
    *,
    cseq: int,
    accept: str = "*/*",
    authorization: str | None = None,
) -> str:
    headers = [
        f"{method} {path} RTSP/1.0",
        f"CSeq: {cseq}",
        f"Host: {host}",
        f"Accept: {accept}",
        "User-Agent: tvt-edge-discovery/1.0",
    ]
    if authorization:
        headers.append(f"Authorization: Basic {authorization}")
    return "\r\n".join(headers) + "\r\n\r\n"


def _parse_sdp(body: bytes) -> dict[str, Any]:
    lines = body.decode(errors="replace").splitlines()
    profile: str | None = None
    codec: str | None = None
    width = None
    height = None
    fps = None
    for line in lines:
        if line.startswith("m=") and " video " in line:
            profile = line.split()[3] if len(line.split()) > 3 else profile
        if line.startswith("a=rtpmap:") and "/" in line:
            _, payload = line.split(":", 1)
            codec_token = payload.split()[1].split("/")[0].strip()
            codec = codec_token.lower()
        if line.startswith("a=framesize:") and " " in line:
            _, dims = line.split(":", 1)
            dims = dims.split(" ", 1)[1] if " " in dims else dims
            width_str, height_str = dims.split("-")[:2]
            try:
                width = int(width_str)
                height = int(height_str)
            except ValueError:
                pass
        if line.startswith("a=framerate:"):
            value = line.split(":", 1)[-1]
            try:
                fps = float(value)
            except ValueError:
                pass
    if codec is not None and profile is None:
        profile = "video"
    if codec is not None and codec not in SUPPORTED_CODECS:
        return {"result_code": "UNSUPPORTED_CODEC", "codec": codec}
    if codec is None:
        return {}
    return {
        "result_code": "OK",
        "codec": codec,
        "width": width,
        "height": height,
        "fps": fps,
        "transport": "tcp",
        "profile": profile,
    }


def probe_rtsp(
    host: str,
    port: int,
    *,
    path: str = "/",
    scheme: str = "rtsp",
    username: str | None = None,
    password: str | None = None,
    connect_timeout: float = 3.0,
    negotiate_timeout: float = 5.0,
    media_timeout: float = 10.0,
    overall_timeout: float = 20.0,
) -> ProbeResult:
    """Probe RTSP reachability and retrieve basic SDP metadata."""

    start = time.monotonic()
    if time.monotonic() - start > overall_timeout:
        return ProbeResult("NETWORK_TIMEOUT", {"error": "probe timeout"})
    address = (host, port)
    try:
        raw = socket.create_connection(address, timeout=connect_timeout)
    except TimeoutError:
        return ProbeResult("NETWORK_TIMEOUT", {"error": "connect timeout"})
    except ConnectionRefusedError:
        return ProbeResult("CONNECTION_REFUSED", {"error": "connection refused"})
    except OSError as error:
        return ProbeResult("NETWORK_TIMEOUT", {"error": str(error)})

    sock = raw
    try:
        if scheme == "rtsps":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        remaining = overall_timeout - (time.monotonic() - start)
        if remaining <= 0:
            return ProbeResult("NETWORK_TIMEOUT", {"error": "overall timeout"})
        sock.settimeout(remaining)

        auth = _build_authorization(username, password)
        request_id = 1
        options = _build_request("OPTIONS", "*", host, cseq=request_id, authorization=auth)
        status, _, _ = _send_request(sock, options, timeout=min(negotiate_timeout, remaining))
        if status == 401:
            return ProbeResult("RTSP_AUTH_FAILED", {"method": "OPTIONS"})
        if status >= 400:
            return ProbeResult("RTSP_NEGOTIATION_FAILED", {"method": "OPTIONS", "status": status})

        request_id += 1
        describe = _build_request(
            "DESCRIBE",
            path,
            host,
            cseq=request_id,
            accept="application/sdp",
            authorization=auth,
        )
        status, headers, body = _send_request(
            sock, describe, timeout=min(negotiate_timeout, overall_timeout - (time.monotonic() - start))
        )

        if status == 401:
            return ProbeResult("RTSP_AUTH_FAILED", {"method": "DESCRIBE", "status": status})
        if status == 404:
            return ProbeResult("RTSP_PATH_NOT_FOUND", {"method": "DESCRIBE", "status": status})
        if status >= 400:
            return ProbeResult("RTSP_NEGOTIATION_FAILED", {"method": "DESCRIBE", "status": status})

        negotiation = _parse_sdp(body)
        if negotiation.get("result_code") != "OK":
            return ProbeResult(negotiation.get("result_code", "RTSP_NEGOTIATION_FAILED"), redact(negotiation))

        detail = {
            "path": path,
            "host": host,
            "port": port,
            "scheme": scheme,
            "codec": negotiation.get("codec"),
            "width": negotiation.get("width"),
            "height": negotiation.get("height"),
            "fps": negotiation.get("fps"),
            "transport": negotiation.get("transport"),
            "profile": negotiation.get("profile"),
            "bytes": headers.get("content-length", 0),
        }
        return ProbeResult("OK", redact(detail))
    except socket.timeout:
        return ProbeResult("MEDIA_TIMEOUT", {"error": "probe timeout"})
    except OSError as error:
        return ProbeResult("NETWORK_TIMEOUT", {"error": str(error)})
    except ValueError as error:
        message = str(error)
        if "PROBE_INTERNAL_ERROR" in message:
            return ProbeResult("PROBE_INTERNAL_ERROR", {"error": message})
        return ProbeResult("PROBE_INTERNAL_ERROR", {"error": message})
    finally:
        if scheme == "rtsps":
            sock.close()
        elif raw is not sock:
            sock.close()
        else:
            raw.close()


def probe_camera(
    host: str,
    port: int,
    credential: dict[str, Any] | None,
    *,
    path: str,
    scheme: str = "rtsp",
) -> ProbeResult:
    if credential is None:
        return probe_rtsp(host, port, path=path, scheme=scheme)
    return probe_rtsp(
        host,
        port,
        path=path,
        scheme=scheme,
        username=credential.get("username"),
        password=credential.get("password"),
    )


__all__ = ["ProbeResult", "probe_camera", "probe_rtsp", "SUPPORTED_CODECS"]

