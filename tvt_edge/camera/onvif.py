"""ONVIF WS-Discovery probe and response parsing helpers."""

from __future__ import annotations

import re
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any

import ipaddress
from urllib.parse import urlparse


MULTICAST_ENDPOINT = ("239.255.255.250", 3702)
SOFT_TIMEOUT = 1.0


_WSD_PROBE_MESSAGE = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery">
  <soap:Header>
    <wsa:MessageID>urn:uuid:{message_id}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action>"http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"</wsa:Action>
  </soap:Header>
  <soap:Body>
    <wsd:Probe>
      <wsd:Types>dn:NetworkVideoTransmitter</wsd:Types>
      <wsd:Scopes></wsd:Scopes>
    </wsd:Probe>
  </soap:Body>
</soap:Envelope>"""


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _iter_xml_bytes(payload: bytes):
    import xml.etree.ElementTree as ET

    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        return None


def _text(node: Any) -> str | None:
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _parse_scopes(scopes_text: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if scopes_text is None:
        return result
    for piece in re.split(r"\s+", scopes_text.strip()):
        if "//" not in piece:
            continue
        lowered = piece.lower()
        if "onvif.org/hardware" in lowered:
            result["manufacturer"] = piece.split("/", -1)[-1]
        elif "onvif.org/model" in lowered:
            result["model"] = piece.split("/", -1)[-1]
        elif "onvif.org/name" in lowered:
            result["name"] = piece.split("/", -1)[-1]
    return result


@dataclass(frozen=True)
class OnvifDiscovery:
    endpoint_uuid: str | None
    interface: str | None
    xaddrs: list[str] = field(default_factory=list)
    scopes: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def hosts(self) -> list[str]:
        hosts: list[str] = []
        for item in self.xaddrs:
            parsed = urlparse(item)
            if not parsed.hostname:
                continue
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError:
                continue
            hosts.append(parsed.hostname)
        return hosts


def parse_onvif_response(payload: bytes) -> list[OnvifDiscovery]:
    """Parse ONVIF probe responses into normalized discovery records."""

    root = _iter_xml_bytes(payload)
    if root is None:
        return []
    response = []
    for element in root.findall(".//"):
        tag = _strip_ns(element.tag)
        if tag != "ProbeMatches":
            continue
        for candidate in list(element):
            if _strip_ns(candidate.tag) != "ProbeMatch":
                continue
            address_node = None
            xaddr_node = None
            scopes_node = None
            types_node = None
            metadata: dict[str, Any] = {}
            for item in candidate:
                name = _strip_ns(item.tag)
                if name == "EndpointReference":
                    address_node = next(
                        (
                            child
                            for child in item
                            if _strip_ns(child.tag) == "Address"
                        ),
                        None,
                    )
                elif name == "XAddrs":
                    xaddr_node = item
                elif name == "Scopes":
                    scopes_node = item
                elif name == "Types":
                    types_node = item
            endpoint = _text(address_node)
            xaddrs = []
            if xaddr_node is not None:
                xaddrs = _text(xaddr_node).split() if _text(xaddr_node) else []
            scope_info = _parse_scopes(_text(scopes_node))
            metadata["types"] = _text(types_node)
            if endpoint:
                response.append(
                    OnvifDiscovery(
                        endpoint_uuid=_text(address_node),
                        interface=None,
                        xaddrs=xaddrs,
                        scopes=scope_info,
                        metadata=metadata,
                    )
                )
    return response


def discover_onvif(
    *,
    interfaces: list[str] | None = None,
    timeout: float = SOFT_TIMEOUT,
    interface_timeout: float = SOFT_TIMEOUT,
) -> list[OnvifDiscovery]:
    """Send WS-Discovery Probe packets and return camera candidates."""

    probes = interfaces or [None]
    message_id = uuid.uuid4()
    request = _WSD_PROBE_MESSAGE.format(message_id=message_id)
    payload = request.encode("utf-8")
    result: list[OnvifDiscovery] = []

    for item in probes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.settimeout(interface_timeout)
            try:
                if item:
                    # Best effort bind to interface; ignore failures silently.
                    try:
                        sock.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_BINDTODEVICE,
                            item.encode(),
                        )
                    except OSError:
                        pass
                sock.sendto(payload, MULTICAST_ENDPOINT)
            except OSError:
                continue
            deadline = timeout
            while deadline > 0:
                try:
                    response, _ = sock.recvfrom(65535)
                    matches = parse_onvif_response(response)
                    for candidate in matches:
                        if not candidate.endpoint_uuid:
                            continue
                        result.append(
                            OnvifDiscovery(
                                endpoint_uuid=candidate.endpoint_uuid,
                                interface=item,
                                xaddrs=candidate.xaddrs,
                                scopes=candidate.scopes,
                                metadata=candidate.metadata,
                            )
                        )
                    deadline = 0
                except socket.timeout:
                    break
        finally:
            sock.close()
    deduped: list[OnvifDiscovery] = []
    seen: set[tuple[str, str]] = set()
    for candidate in result:
        for address in candidate.hosts:
            key = (address, candidate.endpoint_uuid or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
            break
    return deduped


__all__ = ["OnvifDiscovery", "discover_onvif", "parse_onvif_response"]

