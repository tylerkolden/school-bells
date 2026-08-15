"""Minimal hardened SIP UAC for scheduled one-way PCMU paging.

Implements the relevant RFC 3261 transaction behavior for UDP, reliable TCP/TLS transports,
Digest authentication including SHA-256 from RFC 8760, SDP negotiation, ACK/BYE dialog handling,
and unicast RTP/PCMU media. It is deliberately a paging client, not a general-purpose PBX stack.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bell.audio import load_frames
from bell.config import Destination
from bell.protocols.base import DeliveryOutcome
from bell.wire.base import StreamState
from bell.wire.plain_rtp import PlainRTP

LOGGER = logging.getLogger(__name__)


class SIPError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SIPResponse:
    status: int
    reason: str
    headers: dict[str, str]
    body: bytes
    peer: tuple[str, int] | None = None


@dataclass(frozen=True, slots=True)
class SDPAnswer:
    address: str
    port: int
    payload_types: tuple[int, ...]


def parse_sip_response(data: bytes, peer: tuple[str, int] | None = None) -> SIPResponse:
    head, separator, body = data.partition(b"\r\n\r\n")
    if not separator:
        raise SIPError("malformed SIP response: missing header terminator")
    lines = head.decode("utf-8", "replace").split("\r\n")
    match = re.fullmatch(r"SIP/2\.0\s+(\d{3})\s*(.*)", lines[0])
    if not match:
        raise SIPError("malformed SIP status line")
    headers: dict[str, str] = {}
    compact_names = {
        "c": "content-type",
        "f": "from",
        "i": "call-id",
        "l": "content-length",
        "m": "contact",
        "t": "to",
        "v": "via",
    }
    current: str | None = None
    for line in lines[1:]:
        if line.startswith((" ", "\t")) and current:
            headers[current] += " " + line.strip()
            continue
        name, marker, value = line.partition(":")
        if not marker:
            raise SIPError(f"malformed SIP header: {line!r}")
        current = compact_names.get(name.strip().lower(), name.strip().lower())
        cleaned = value.strip()
        headers[current] = f"{headers[current]}\n{cleaned}" if current in headers else cleaned
    length = int(headers.get("content-length", len(body)))
    if len(body) < length:
        raise SIPError("truncated SIP body")
    return SIPResponse(int(match.group(1)), match.group(2), headers, body[:length], peer)


def parse_digest_challenge(value: str) -> dict[str, str]:
    scheme, _, parameters = value.partition(" ")
    if scheme.lower() != "digest":
        raise SIPError("SIP Basic authentication is not supported")
    result: dict[str, str] = {}
    for match in re.finditer(r'(\w[\w-]*)\s*=\s*(?:"([^"]*)"|([^,\s]+))', parameters):
        result[match.group(1).lower()] = match.group(2) if match.group(2) is not None else match.group(3)
    if not {"realm", "nonce"} <= result.keys():
        raise SIPError("incomplete SIP Digest challenge")
    return result


def select_digest_challenge(value: str) -> dict[str, str]:
    """Choose the strongest supported challenge when a server sends multiple algorithms."""
    candidates: list[dict[str, str]] = []
    for challenge_value in value.splitlines():
        try:
            challenge = parse_digest_challenge(challenge_value)
            algorithm = challenge.get("algorithm", "MD5").upper().removesuffix("-SESS")
            if algorithm in {"MD5", "SHA-256", "SHA-512-256"}:
                candidates.append(challenge)
        except SIPError:
            continue
    if not candidates:
        raise SIPError("no supported SIP Digest challenge was offered")
    strength = {"MD5": 1, "SHA-256": 2, "SHA-512-256": 3}
    return max(
        candidates,
        key=lambda item: strength[item.get("algorithm", "MD5").upper().removesuffix("-SESS")],
    )


def _hash(algorithm: str, value: str) -> str:
    normalized = algorithm.upper().removesuffix("-SESS")
    names = {"MD5": "md5", "SHA-256": "sha256", "SHA-512-256": "sha512_256"}
    if normalized not in names:
        raise SIPError(f"unsupported SIP Digest algorithm {algorithm!r}")
    try:
        digest = hashlib.new(names[normalized])
    except ValueError as exc:
        raise SIPError(f"hash algorithm {algorithm!r} is unavailable") from exc
    digest.update(value.encode())
    return digest.hexdigest()


def build_digest_authorization(
    challenge: dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
    *,
    nonce_count: int = 1,
    cnonce: str | None = None,
) -> str:
    algorithm = challenge.get("algorithm", "MD5")
    qop_options = {item.strip().lower() for item in challenge.get("qop", "auth").split(",")}
    if "auth" not in qop_options:
        raise SIPError("SIP Digest challenge does not offer qop=auth")
    cnonce = cnonce or secrets.token_hex(12)
    nc = f"{nonce_count:08x}"
    ha1 = _hash(algorithm, f"{username}:{challenge['realm']}:{password}")
    if algorithm.upper().endswith("-SESS"):
        ha1 = _hash(algorithm, f"{ha1}:{challenge['nonce']}:{cnonce}")
    ha2 = _hash(algorithm, f"{method}:{uri}")
    response = _hash(
        algorithm,
        f"{ha1}:{challenge['nonce']}:{nc}:{cnonce}:auth:{ha2}",
    )
    fields = [
        f'username="{username}"',
        f'realm="{challenge["realm"]}"',
        f'nonce="{challenge["nonce"]}"',
        f'uri="{uri}"',
        f"response=\"{response}\"",
        f"algorithm={algorithm}",
        "qop=auth",
        f"nc={nc}",
        f'cnonce="{cnonce}"',
    ]
    if opaque := challenge.get("opaque"):
        fields.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(fields)


def parse_sdp(body: bytes) -> SDPAnswer:
    address: str | None = None
    media_address: str | None = None
    port: int | None = None
    payload_types: tuple[int, ...] = ()
    in_audio = False
    for line in body.decode("utf-8", "replace").replace("\r", "").split("\n"):
        if line.startswith("m=audio "):
            parts = line.split()
            if len(parts) < 4:
                raise SIPError("malformed SDP audio media line")
            port = int(parts[1])
            payload_types = tuple(int(item) for item in parts[3:] if item.isdigit())
            in_audio = True
        elif line.startswith("m="):
            in_audio = False
        elif line.startswith("c=IN IP4 "):
            candidate = line.split()[2].split("/", 1)[0]
            if in_audio:
                media_address = candidate
            elif address is None:
                address = candidate
    selected_address = media_address or address
    if not selected_address or not port:
        raise SIPError("SDP answer has no usable IPv4 audio destination")
    if 0 not in payload_types:
        raise SIPError("SIP peer did not accept PCMU payload type 0")
    return SDPAnswer(selected_address, port, payload_types)


def build_sdp(local_ip: str, media_port: int) -> bytes:
    session_id = secrets.randbelow(10**12)
    lines = (
        "v=0",
        f"o=bell {session_id} {session_id} IN IP4 {local_ip}",
        "s=School Bell Page",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {media_port} RTP/AVP 0",
        "a=rtpmap:0 PCMU/8000",
        "a=sendonly",
        "a=ptime:20",
    )
    return ("\r\n".join(lines) + "\r\n").encode()


def build_request(
    method: str,
    uri: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> bytes:
    complete = {**headers, "Content-Length": str(len(body))}
    text = f"{method} {uri} SIP/2.0\r\n" + "".join(
        f"{name}: {value}\r\n" for name, value in complete.items()
    )
    return text.encode() + b"\r\n" + body


def _request_header(message: bytes, name: str) -> str | None:
    header_text = message.partition(b"\r\n\r\n")[0].decode("utf-8", "replace")
    prefix = name.lower() + ":"
    for line in header_text.split("\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _matches_transaction(response: SIPResponse, message: bytes) -> bool:
    expected_call_id = _request_header(message, "Call-ID")
    expected_cseq = _request_header(message, "CSeq")
    return (
        (expected_call_id is None or response.headers.get("call-id") == expected_call_id)
        and (expected_cseq is None or response.headers.get("cseq") == expected_cseq)
    )


class SIPTransport:
    def __init__(self, destination: Destination, local_ip: str) -> None:
        self.destination = destination
        self.local_ip = local_ip

    def _addresses(self) -> list[tuple[Any, ...]]:
        if not self.destination.sip_host:
            raise SIPError("SIP host is not configured")
        socktype = socket.SOCK_DGRAM if self.destination.sip_transport == "udp" else socket.SOCK_STREAM
        return socket.getaddrinfo(
            self.destination.sip_host,
            self.destination.port,
            socket.AF_INET,
            socktype,
        )

    def request(self, message: bytes) -> SIPResponse:
        errors: list[str] = []
        for family, socktype, protocol, _canonical, address in self._addresses():
            attempts = 1 if self.destination.sip_transport == "udp" else self.destination.retries + 1
            for _attempt in range(attempts):
                try:
                    if self.destination.sip_transport == "udp":
                        return self._udp_request(message, family, socktype, protocol, address)
                    return self._stream_request(message, family, socktype, protocol, address)
                except (OSError, SIPError, TimeoutError) as exc:
                    errors.append(f"{address[0]}:{address[1]}: {exc}")
                if self.destination.sip_transport == "udp":
                    break
        raise SIPError("all SIP targets failed: " + "; ".join(errors))

    def send(self, message: bytes) -> None:
        family, socktype, protocol, _canonical, address = self._addresses()[0]
        if self.destination.sip_transport == "udp":
            with socket.socket(family, socktype, protocol) as sock:
                sock.bind((self.local_ip, 0))
                sock.sendto(message, address)
            return
        with self._connect_stream(family, socktype, protocol, address) as sock:
            sock.sendall(message)

    def _udp_request(
        self,
        message: bytes,
        family: int,
        socktype: int,
        protocol: int,
        address: tuple[str, int],
    ) -> SIPResponse:
        deadline = time.monotonic() + self.destination.timeout_seconds
        interval = 0.5
        max_sends = self.destination.retries + 1
        with socket.socket(family, socktype, protocol) as sock:
            sock.bind((self.local_ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
            sock.sendto(message, address)
            sends = 1
            next_send = time.monotonic() + interval if sends < max_sends else deadline
            while time.monotonic() < deadline:
                wait = max(0.01, min(next_send, deadline) - time.monotonic())
                sock.settimeout(wait)
                try:
                    data, peer = sock.recvfrom(65535)
                except TimeoutError:
                    if time.monotonic() >= next_send and sends < max_sends:
                        sock.sendto(message, address)
                        sends += 1
                        interval = min(interval * 2, 4.0)
                        next_send = (
                            time.monotonic() + interval if sends < max_sends else deadline
                        )
                    continue
                response = parse_sip_response(data, (peer[0], peer[1]))
                if not _matches_transaction(response, message):
                    LOGGER.warning("unmatched_sip_response_ignored", extra={"peer": peer[0]})
                    continue
                if response.status >= 200:
                    return response
            raise TimeoutError("SIP UDP transaction timed out")

    def _connect_stream(
        self,
        family: int,
        socktype: int,
        protocol: int,
        address: tuple[str, int],
    ) -> socket.socket:
        raw = socket.socket(family, socktype, protocol)
        raw.settimeout(self.destination.timeout_seconds)
        raw.bind((self.local_ip, 0))
        raw.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        raw.connect(address)
        if self.destination.sip_transport != "tls":
            return raw
        context = ssl.create_default_context(
            cafile=str(self.destination.tls_ca_file) if self.destination.tls_ca_file else None
        )
        return context.wrap_socket(raw, server_hostname=self.destination.tls_server_name)

    def _stream_request(
        self,
        message: bytes,
        family: int,
        socktype: int,
        protocol: int,
        address: tuple[str, int],
    ) -> SIPResponse:
        with self._connect_stream(family, socktype, protocol, address) as sock:
            sock.sendall(message)
            file = sock.makefile("rb")
            while True:
                status_line = file.readline(8192)
                if not status_line:
                    raise SIPError("SIP peer closed the connection without a final response")
                header_lines = [status_line]
                content_length = 0
                header_bytes = len(status_line)
                for _header_number in range(64):
                    line = file.readline(8192)
                    if not line:
                        raise SIPError("SIP peer closed during response headers")
                    header_lines.append(line)
                    header_bytes += len(line)
                    if header_bytes > 65536:
                        raise SIPError("SIP response headers exceed 64 KiB")
                    if line == b"\r\n":
                        break
                    if line.lower().startswith(b"content-length:"):
                        content_length = int(line.split(b":", 1)[1].strip())
                else:
                    raise SIPError("SIP response contains too many headers")
                if content_length > 65536:
                    raise SIPError("SIP response body exceeds 64 KiB")
                body = file.read(content_length)
                response = parse_sip_response(b"".join(header_lines) + body, address)
                if _matches_transaction(response, message) and response.status >= 200:
                    return response


class SIPClient:
    def __init__(
        self,
        destination: Destination,
        local_ip: str,
        transport: SIPTransport | None = None,
    ) -> None:
        if destination.protocol != "sip":
            raise ValueError("SIPClient requires a SIP destination")
        self.destination = destination
        self.local_ip = local_ip
        self.transport = transport or SIPTransport(destination, local_ip)

    def options(self) -> DeliveryOutcome:
        started = time.monotonic()
        call_id = f"{secrets.token_hex(12)}@{self.local_ip}"
        cseq = 1
        from_tag = secrets.token_hex(8)
        headers = self._base_headers("OPTIONS", call_id, cseq, from_tag, secrets.token_hex(8))
        attempts = 1
        try:
            response = self.transport.request(build_request("OPTIONS", self.destination.sip_uri or "", headers))
            if response.status in {401, 407}:
                challenge_name = "www-authenticate" if response.status == 401 else "proxy-authenticate"
                challenge_value = response.headers.get(challenge_name)
                if not challenge_value or not self.destination.sip_username or not self.destination.sip_password_env:
                    raise SIPError("OPTIONS requires SIP credentials that are not configured")
                password = os.environ.get(self.destination.sip_password_env)
                if password is None:
                    raise SIPError(f"environment variable {self.destination.sip_password_env} is not set")
                cseq += 1
                headers = self._base_headers(
                    "OPTIONS", call_id, cseq, from_tag, secrets.token_hex(8)
                )
                auth_name = "Authorization" if response.status == 401 else "Proxy-Authorization"
                headers[auth_name] = build_digest_authorization(
                    select_digest_challenge(challenge_value),
                    self.destination.sip_username,
                    password,
                    "OPTIONS",
                    self.destination.sip_uri or "",
                )
                response = self.transport.request(
                    build_request("OPTIONS", self.destination.sip_uri or "", headers)
                )
                attempts += 1
            reachable = 200 <= response.status < 300 or response.status == 405
            return DeliveryOutcome(
                self.destination.name,
                "sip",
                reachable,
                str(response.status),
                response.reason,
                attempts,
                time.monotonic() - started,
            )
        except (SIPError, OSError, TimeoutError) as exc:
            return DeliveryOutcome(
                self.destination.name,
                "sip",
                False,
                "unreachable",
                str(exc),
                1,
                time.monotonic() - started,
            )

    def page(self, raw_audio: Path, cancel_event: threading.Event | None = None) -> DeliveryOutcome:
        started = time.monotonic()
        media_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        media_socket.bind((self.local_ip, 0))
        media_socket.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
        call_id = f"{secrets.token_hex(12)}@{self.local_ip}"
        from_tag = secrets.token_hex(8)
        branch = secrets.token_hex(8)
        cseq = 1
        uri = self.destination.sip_uri or ""
        body = build_sdp(self.local_ip, media_socket.getsockname()[1])
        headers = self._base_headers("INVITE", call_id, cseq, from_tag, branch)
        headers["Content-Type"] = "application/sdp"
        attempts = 1
        auth_context: tuple[str, dict[str, str], str, str] | None = None
        try:
            response = self.transport.request(build_request("INVITE", uri, headers, body))
            if response.status in {401, 407}:
                challenge_name = "www-authenticate" if response.status == 401 else "proxy-authenticate"
                challenge_value = response.headers.get(challenge_name)
                if not challenge_value or not self.destination.sip_username or not self.destination.sip_password_env:
                    raise SIPError("SIP authentication required but credentials are not configured")
                password = os.environ.get(self.destination.sip_password_env)
                if password is None:
                    raise SIPError(f"environment variable {self.destination.sip_password_env} is not set")
                self.transport.send(self._ack(response, uri, headers, call_id, cseq, from_tag))
                cseq += 1
                branch = secrets.token_hex(8)
                headers = self._base_headers("INVITE", call_id, cseq, from_tag, branch)
                headers["Content-Type"] = "application/sdp"
                auth_name = "Authorization" if response.status == 401 else "Proxy-Authorization"
                challenge = select_digest_challenge(challenge_value)
                headers[auth_name] = build_digest_authorization(
                    challenge,
                    self.destination.sip_username,
                    password,
                    "INVITE",
                    uri,
                )
                response = self.transport.request(build_request("INVITE", uri, headers, body))
                auth_context = (
                    auth_name,
                    challenge,
                    self.destination.sip_username,
                    password,
                )
                attempts += 1
            if not 200 <= response.status < 300:
                raise SIPError(f"INVITE failed with {response.status} {response.reason}")
            answer = parse_sdp(response.body)
            self.transport.send(self._ack(response, uri, headers, call_id, cseq, from_tag))
            media_error: OSError | None = None
            packet_count = 0
            cancelled = False
            try:
                packet_count, cancelled = self._stream_media(
                    media_socket,
                    load_frames(raw_audio),
                    (answer.address, answer.port),
                    cancel_event,
                )
            except OSError as exc:
                media_error = exc
            finally:
                cseq += 1
                bye_uri = _contact_uri(response.headers.get("contact")) or uri
                bye_headers = self._dialog_headers(
                    "BYE", response, call_id, cseq, from_tag, secrets.token_hex(8)
                )
                if auth_context:
                    auth_name, challenge, username, password = auth_context
                    bye_headers[auth_name] = build_digest_authorization(
                        challenge,
                        username,
                        password,
                        "BYE",
                        bye_uri,
                        nonce_count=2,
                    )
                bye_response = self.transport.request(build_request("BYE", bye_uri, bye_headers))
                if not 200 <= bye_response.status < 300:
                    LOGGER.warning(
                        "sip_bye_failed",
                        extra={"destination": self.destination.name, "status": bye_response.status},
                    )
            if media_error:
                raise media_error
            return DeliveryOutcome(
                self.destination.name,
                "sip",
                not cancelled,
                "cancelled" if cancelled else "200",
                f"streamed {packet_count} PCMU packets",
                attempts,
                time.monotonic() - started,
            )
        except (SIPError, OSError, TimeoutError) as exc:
            return DeliveryOutcome(
                self.destination.name,
                "sip",
                False,
                "failed",
                str(exc),
                attempts,
                time.monotonic() - started,
            )
        finally:
            media_socket.close()

    def _base_headers(
        self, method: str, call_id: str, cseq: int, from_tag: str, branch: str
    ) -> dict[str, str]:
        transport = self.destination.sip_transport.upper()
        identity = self.destination.sip_username or "bell"
        uri = self.destination.sip_uri or ""
        return {
            "Via": f"SIP/2.0/{transport} {self.local_ip};branch=z9hG4bK{branch};rport",
            "Max-Forwards": "70",
            "From": f'<sip:{identity}@{self.local_ip}>;tag={from_tag}',
            "To": f"<{uri}>",
            "Call-ID": call_id,
            "CSeq": f"{cseq} {method}",
            "Contact": f"<sip:{identity}@{self.local_ip};transport={self.destination.sip_transport}>",
            "User-Agent": "bell-system/0.1",
            "Allow": "INVITE, ACK, BYE, OPTIONS",
        }

    def _ack(
        self,
        response: SIPResponse,
        uri: str,
        invite_headers: dict[str, str],
        call_id: str,
        cseq: int,
        from_tag: str,
    ) -> bytes:
        headers = self._dialog_headers("ACK", response, call_id, cseq, from_tag, secrets.token_hex(8))
        headers["Via"] = invite_headers["Via"] if response.status >= 300 else headers["Via"]
        return build_request("ACK", _contact_uri(response.headers.get("contact")) or uri, headers)

    def _dialog_headers(
        self,
        method: str,
        response: SIPResponse,
        call_id: str,
        cseq: int,
        from_tag: str,
        branch: str,
    ) -> dict[str, str]:
        headers = self._base_headers(method, call_id, cseq, from_tag, branch)
        headers["To"] = response.headers.get("to", headers["To"])
        return headers

    @staticmethod
    def _stream_media(
        sock: socket.socket,
        frames: Iterable[bytes],
        address: tuple[str, int],
        cancel_event: threading.Event | None,
    ) -> tuple[int, bool]:
        state = StreamState()
        wire = PlainRTP()
        start = time.monotonic()
        count = 0
        for index, frame in enumerate(frames):
            if cancel_event is not None and cancel_event.is_set():
                return count, True
            remaining = start + index * 0.020 - time.monotonic()
            if remaining > 0:
                if cancel_event is not None and cancel_event.wait(remaining):
                    return count, True
                if cancel_event is None:
                    time.sleep(remaining)
            seq, timestamp, ssrc = state.next()
            sock.sendto(wire.build_packet(frame, seq, timestamp, ssrc, index == 0, 0), address)
            count += 1
        return count, False


def _contact_uri(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"<?(sips?:[^>;\s]+)", value, re.IGNORECASE)
    return match.group(1) if match else None
