from __future__ import annotations

import hashlib
import socket
import ssl
import threading
from pathlib import Path

import pytest

from bell.config import Destination
from bell.protocols.sip import (
    SIPClient,
    SIPError,
    SIPResponse,
    SIPTransport,
    build_digest_authorization,
    build_request,
    build_sdp,
    parse_digest_challenge,
    parse_sdp,
    parse_sip_response,
    select_digest_challenge,
)


def sip_destination(**changes) -> Destination:
    data = {
        "name": "pbx",
        "protocol": "sip",
        "port": 5060,
        "sip_uri": "sip:page@example.test",
        "sip_host": "127.0.0.1",
        "sip_transport": "udp",
        "timeout_seconds": 1,
        "retries": 0,
    }
    data.update(changes)
    return Destination(**data)


def test_sip_response_request_and_sdp_parsing() -> None:
    response = parse_sip_response(
        b"SIP/2.0 200 OK\r\nTo: <sip:x>;tag=abc\r\nContent-Length: 4\r\n\r\ntestextra"
    )
    assert response.status == 200 and response.body == b"test"
    request = build_request("OPTIONS", "sip:x@example.test", {"Call-ID": "1"})
    assert request.startswith(b"OPTIONS sip:x@example.test SIP/2.0\r\n")
    assert request.endswith(b"Content-Length: 0\r\n\r\n")
    answer = parse_sdp(
        b"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 40000 RTP/AVP 0 8\r\na=rtpmap:0 PCMU/8000\r\n"
    )
    assert (answer.address, answer.port, answer.payload_types) == ("127.0.0.1", 40000, (0, 8))
    assert b"a=ptime:20" in build_sdp("127.0.0.1", 12345)
    compact = parse_sip_response(
        b"SIP/2.0 200 OK\r\ni: compact-call\r\nl: 0\r\nt: <sip:x>;tag=1\r\n\r\n"
    )
    assert compact.headers["call-id"] == "compact-call"
    assert compact.headers["to"].endswith("tag=1")


def test_sdp_negotiates_preferred_common_codec() -> None:
    offer = build_sdp("127.0.0.1", 12345, ("g722", "pcmu", "pcma"))
    assert b"m=audio 12345 RTP/AVP 9 0 8" in offer
    assert b"a=rtpmap:9 G722/8000" in offer
    answer = parse_sdp(
        b"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 40000 RTP/AVP 8 0\r\n",
        ("g722", "pcma", "pcmu"),
    )
    assert answer.codec == "pcma" and answer.payload_type == 8
    with pytest.raises(SIPError, match="SRTP is not configured"):
        parse_sdp(
            b"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 40000 RTP/SAVP 0\r\n"
        )


def test_sha256_digest_matches_hand_computation() -> None:
    challenge = parse_digest_challenge(
        'Digest realm="school", nonce="abc", algorithm=SHA-256, qop="auth", opaque="xyz"'
    )
    authorization = build_digest_authorization(
        challenge,
        "bell",
        "secret",
        "INVITE",
        "sip:page@example.test",
        nonce_count=1,
        cnonce="010203",
    )
    ha1 = hashlib.sha256(b"bell:school:secret").hexdigest()
    ha2 = hashlib.sha256(b"INVITE:sip:page@example.test").hexdigest()
    expected = hashlib.sha256(f"{ha1}:abc:00000001:010203:auth:{ha2}".encode()).hexdigest()
    assert f'response="{expected}"' in authorization
    assert "algorithm=SHA-256" in authorization
    assert 'opaque="xyz"' in authorization
    selected = select_digest_challenge(
        'Digest realm="school", nonce="legacy", algorithm=MD5, qop="auth"\n'
        'Digest realm="school", nonce="modern", algorithm=SHA-256, qop="auth"'
    )
    assert selected["nonce"] == "modern"


def test_legacy_digest_without_qop_is_supported() -> None:
    challenge = parse_digest_challenge('Digest realm="school", nonce="abc", algorithm=MD5')
    authorization = build_digest_authorization(
        challenge, "bell", "secret", "OPTIONS", "sip:page@example.test"
    )
    ha1 = hashlib.md5(b"bell:school:secret").hexdigest()
    ha2 = hashlib.md5(b"OPTIONS:sip:page@example.test").hexdigest()
    expected = hashlib.md5(f"{ha1}:abc:{ha2}".encode()).hexdigest()
    assert f'response="{expected}"' in authorization
    assert "qop=" not in authorization and "cnonce=" not in authorization


class FakeTransport:
    def __init__(self, media_port: int) -> None:
        self.media_port = media_port
        self.requests: list[bytes] = []
        self.sent: list[bytes] = []

    def request(self, message: bytes) -> SIPResponse:
        self.requests.append(message)
        if message.startswith(b"INVITE"):
            sdp = (
                f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {self.media_port} RTP/AVP 0\r\n"
            ).encode()
            return SIPResponse(
                200,
                "OK",
                {
                    "to": "<sip:page@example.test>;tag=remote",
                    "contact": "<sip:page@127.0.0.1>",
                    "record-route": "<sip:first.example;lr>\n<sip:second.example;lr>",
                },
                sdp,
            )
        return SIPResponse(200, "OK", {}, b"")

    def send(self, message: bytes) -> None:
        self.sent.append(message)


def test_sip_client_streams_pcmu_and_closes_dialog(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2)
    raw = tmp_path / "audio.ulaw"
    raw.write_bytes(b"a" * 320)
    packets: list[bytes] = []

    def receive() -> None:
        while len(packets) < 2:
            packets.append(listener.recvfrom(2048)[0])

    thread = threading.Thread(target=receive)
    thread.start()
    transport = FakeTransport(listener.getsockname()[1])
    outcome = SIPClient(sip_destination(), "127.0.0.1", transport).page(raw)
    thread.join(timeout=3)
    listener.close()
    assert outcome.success and len(packets) == 2
    assert transport.requests[0].startswith(b"INVITE ")
    assert transport.sent[0].startswith(b"ACK ")
    assert transport.requests[-1].startswith(b"BYE ")
    assert b"Route: <sip:second.example;lr>, <sip:first.example;lr>" in transport.requests[-1]
    assert packets[0][12:] == b"a" * 160


def test_udp_transport_ignores_unmatched_response() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    destination = sip_destination(port=server.getsockname()[1])

    def respond() -> None:
        request, peer = server.recvfrom(8192)
        text = request.decode()
        call_id = next(line.split(":", 1)[1].strip() for line in text.split("\r\n") if line.lower().startswith("call-id:"))
        cseq = next(line.split(":", 1)[1].strip() for line in text.split("\r\n") if line.lower().startswith("cseq:"))
        server.sendto(
            f"SIP/2.0 200 Wrong\r\nCall-ID: other\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n".encode(),
            peer,
        )
        server.sendto(
            f"SIP/2.0 200 OK\r\nCall-ID: {call_id}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n".encode(),
            peer,
        )

    thread = threading.Thread(target=respond)
    thread.start()
    message = build_request(
        "OPTIONS",
        "sip:page@example.test",
        {"Call-ID": "expected", "CSeq": "1 OPTIONS"},
    )
    response = SIPTransport(destination, "127.0.0.1").request(message)
    thread.join(timeout=2)
    server.close()
    assert response.status == 200 and response.reason == "OK"


def test_tcp_transport_handles_provisional_then_final_response() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    destination = sip_destination(port=server.getsockname()[1], sip_transport="tcp")

    def respond() -> None:
        connection, _peer = server.accept()
        with connection:
            request = connection.recv(8192).decode()
            call_id = next(
                line.split(":", 1)[1].strip()
                for line in request.split("\r\n")
                if line.lower().startswith("call-id:")
            )
            cseq = next(
                line.split(":", 1)[1].strip()
                for line in request.split("\r\n")
                if line.lower().startswith("cseq:")
            )
            common = f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\nContent-Length: 0\r\n\r\n"
            connection.sendall(
                f"SIP/2.0 100 Trying\r\n{common}SIP/2.0 200 OK\r\n{common}".encode()
            )

    thread = threading.Thread(target=respond)
    thread.start()
    message = build_request(
        "OPTIONS",
        "sip:page@example.test",
        {"Call-ID": "tcp-call", "CSeq": "1 OPTIONS"},
    )
    response = SIPTransport(destination, "127.0.0.1").request(message)
    thread.join(timeout=2)
    server.close()
    assert response.status == 200


def test_tls_transport_requires_tls_1_2_or_newer(monkeypatch) -> None:
    class FakeSocket:
        def settimeout(self, _value) -> None:
            pass

        def bind(self, _address) -> None:
            pass

        def setsockopt(self, *_values) -> None:
            pass

        def connect(self, _address) -> None:
            pass

    class FakeContext:
        minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED

        def wrap_socket(self, raw, *, server_hostname):
            assert server_hostname == "pbx.example.test"
            return raw

    raw = FakeSocket()
    context = FakeContext()
    monkeypatch.setattr(socket, "socket", lambda *_args: raw)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    destination = sip_destination(
        sip_transport="tls",
        tls_server_name="pbx.example.test",
        port=5061,
    )
    result = SIPTransport(destination, "127.0.0.1")._connect_stream(
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        ("127.0.0.1", 5061),
    )
    assert result is raw
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_options_monitor_validates_digest_credentials(monkeypatch) -> None:
    monkeypatch.setenv("SIP_PASSWORD", "secret")

    class OptionsTransport:
        def __init__(self) -> None:
            self.messages: list[bytes] = []

        def request(self, message: bytes) -> SIPResponse:
            self.messages.append(message)
            if len(self.messages) == 1:
                return SIPResponse(
                    401,
                    "Unauthorized",
                    {
                        "www-authenticate": (
                            'Digest realm="school", nonce="abc", algorithm=SHA-256, qop="auth"'
                        )
                    },
                    b"",
                )
            return SIPResponse(200, "OK", {}, b"")

    transport = OptionsTransport()
    destination = sip_destination(
        sip_username="bell",
        sip_password_env="SIP_PASSWORD",
    )
    outcome = SIPClient(destination, "127.0.0.1", transport).options()
    assert outcome.success and outcome.attempts == 2
    assert b"Authorization: Digest" in transport.messages[1]


def test_authenticated_invite_reuses_digest_for_bye(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIP_PASSWORD", "secret")
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2)

    class AuthTransport:
        def __init__(self) -> None:
            self.requests: list[bytes] = []
            self.sent: list[bytes] = []

        def request(self, message: bytes) -> SIPResponse:
            self.requests.append(message)
            if len(self.requests) == 1:
                return SIPResponse(
                    401,
                    "Unauthorized",
                    {
                        "to": "<sip:page@example.test>;tag=challenge",
                        "www-authenticate": (
                            'Digest realm="school", nonce="abc", algorithm=SHA-256, qop="auth"'
                        ),
                    },
                    b"",
                )
            if message.startswith(b"INVITE"):
                sdp = (
                    f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {listener.getsockname()[1]} RTP/AVP 0\r\n"
                ).encode()
                return SIPResponse(
                    200,
                    "OK",
                    {"to": "<sip:page@example.test>;tag=remote"},
                    sdp,
                )
            return SIPResponse(200, "OK", {}, b"")

        def send(self, message: bytes) -> None:
            self.sent.append(message)

    raw = tmp_path / "one.ulaw"
    raw.write_bytes(b"x" * 160)
    transport = AuthTransport()
    destination = sip_destination(
        sip_username="bell",
        sip_password_env="SIP_PASSWORD",
    )
    outcome = SIPClient(destination, "127.0.0.1", transport).page(raw)
    listener.recvfrom(2048)
    listener.close()
    assert outcome.success
    assert b"Authorization: Digest" in transport.requests[1]
    assert transport.requests[-1].startswith(b"BYE")
    assert b"Authorization: Digest" in transport.requests[-1]
