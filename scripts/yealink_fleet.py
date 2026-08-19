#!/usr/bin/env python3
"""Safely inspect Yealink web configuration without changing phone settings."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import html
import json
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

LOGIN_FORM_PATH = "/servlet?m=mod_listener&p=login&q=loginForm&jumpto=status"
LOGIN_POST_PATH = "/servlet?m=mod_listener&p=login&q=login"
MULTICAST_PATH = "/servlet?m=mod_data&p=contacts-multicastIP&q=load"
SENSITIVE_NAME = re.compile(
    r"password|passwd|pwd|secret|token|session|cookie|credential",
    re.IGNORECASE,
)
DISCOVERY_TOKENS = (
    "ReceivePriority",
    "IgnoreDNDPriority",
    "ReceiveActive",
    "Listening",
    "Channel",
    "Multicast",
    "YLForm.submit",
)


class YealinkError(RuntimeError):
    """Raised when a safe Yealink operation cannot be completed."""


@dataclass(frozen=True)
class Control:
    tag: str
    type: str
    name: str
    id: str
    value: str
    default: str


@dataclass(frozen=True)
class AssetFinding:
    url: str
    size: int
    tokens: list[str]
    contexts: list[str]


def normalize_fingerprint(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    if len(normalized) != 64:
        raise ValueError("SHA-256 certificate fingerprint must contain 64 hex digits")
    return normalized


def certificate_fingerprint(host: str, port: int, timeout: float) -> str:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=timeout) as connection,
        context.wrap_socket(connection, server_hostname=host) as secure_connection,
    ):
        certificate = secure_connection.getpeercert(binary_form=True)
    return hashlib.sha256(certificate).hexdigest().upper()


def extract_rsa_key(page: str) -> tuple[str, str]:
    modulus = re.search(r'var\s+g_rsa_n\s*=\s*"([0-9A-Fa-f]+)"', page)
    exponent = re.search(r'var\s+g_rsa_e\s*=\s*"([0-9A-Fa-f]+)"', page)
    if not modulus or not exponent:
        raise YealinkError("the login page did not contain a Yealink RSA public key")
    return modulus.group(1), exponent.group(1)


def rsa_encrypt_text(plaintext: str, modulus_hex: str, exponent_hex: str) -> str:
    modulus = int(modulus_hex, 16)
    exponent = int(exponent_hex, 16)
    size = (modulus.bit_length() + 7) // 8
    message = plaintext.encode()
    padding_length = size - len(message) - 3
    if padding_length < 8:
        raise YealinkError("RSA message is too long")

    padding = bytearray()
    while len(padding) < padding_length:
        padding.extend(value for value in secrets.token_bytes(padding_length) if value)
    encoded = b"\x00\x02" + bytes(padding[:padding_length]) + b"\x00" + message
    encrypted = pow(int.from_bytes(encoded, "big"), exponent, modulus)
    encrypted_hex = format(encrypted, "x")
    return encrypted_hex if len(encrypted_hex) % 2 == 0 else "0" + encrypted_hex


def aes_encrypt_password(plaintext: str, key: bytes, iv: bytes) -> str:
    openssl = shutil.which("openssl")
    if not openssl:
        raise YealinkError("openssl is required for Yealink AES login encryption")
    encoded = plaintext.encode()
    padded = encoded + b"\x00" * ((-len(encoded)) % 16)
    completed = subprocess.run(
        [
            openssl,
            "enc",
            "-aes-128-cbc",
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=padded,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise YealinkError(f"OpenSSL AES encryption failed: {detail}")
    return base64.b64encode(completed.stdout).decode("ascii")


def sanitize(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if SENSITIVE_NAME.search(str(key)) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def redact_context(text: str) -> str:
    patterns = (
        re.compile(
            r"((?:g_strToken|token|sessionid)\s*[:=]\s*['\"])[^'\"]*(['\"])",
            re.IGNORECASE,
        ),
        re.compile(r"(JSESSIONID=)[A-Za-z0-9]+", re.IGNORECASE),
    )
    for pattern in patterns:
        text = pattern.sub(r"\1<redacted>\2" if pattern.groups == 2 else r"\1<redacted>", text)
    return text


def contexts_for(text: str, tokens: tuple[str, ...] = DISCOVERY_TOKENS) -> list[str]:
    ranges: list[tuple[int, int]] = []
    for token in tokens:
        start = 0
        while (position := text.find(token, start)) >= 0:
            left = max(0, position - 500)
            right = min(len(text), position + 1400)
            if not any(left >= old_left and right <= old_right for old_left, old_right in ranges):
                ranges.append((left, right))
            start = position + len(token)
    return [redact_context(text[left:right]) for left, right in ranges[:20]]


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[Control] = []
        self.scripts: list[str] = []
        self.inline_scripts: list[str] = []
        self._script_parts: list[str] | None = None
        self._res_info_parts: list[str] = []
        self._in_res_info = False

    @property
    def res_info(self) -> str:
        return html.unescape("".join(self._res_info_parts).strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        lowered = tag.lower()
        if lowered == "script":
            source = attributes.get("src")
            if source:
                self.scripts.append(source)
            else:
                self._script_parts = []
            return
        if lowered == "div" and attributes.get("id") == "_RES_INFO_":
            self._in_res_info = True
            return
        if lowered not in {"input", "select", "textarea"}:
            return
        identity = f"{attributes.get('name', '')} {attributes.get('id', '')}"
        value = (
            "<redacted>" if SENSITIVE_NAME.search(identity) else attributes.get("value", "")
        )
        self.controls.append(
            Control(
                tag=lowered,
                type=attributes.get("type", ""),
                name=attributes.get("name", ""),
                id=attributes.get("id", ""),
                value=value,
                default=attributes.get("dvalue", attributes.get("defaultvalue", "")),
            )
        )

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "script" and self._script_parts is not None:
            self.inline_scripts.append("".join(self._script_parts))
            self._script_parts = None
        elif lowered == "div" and self._in_res_info:
            self._in_res_info = False

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)
        if self._in_res_info:
            self._res_info_parts.append(data)


class YealinkClient:
    def __init__(
        self,
        host: str,
        fingerprint: str,
        username: str,
        password: str,
        *,
        timeout: float = 20,
    ) -> None:
        self.host = host
        self.base_url = f"https://{host}"
        self.fingerprint = normalize_fingerprint(fingerprint)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.cookies = CookieJar()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = build_opener(
            HTTPCookieProcessor(self.cookies),
            HTTPSHandler(context=context),
        )
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                "Chrome/151 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*",
        }

    def verify_identity(self) -> None:
        actual = certificate_fingerprint(self.host, 443, self.timeout)
        if actual != self.fingerprint:
            raise YealinkError(
                "certificate fingerprint mismatch: "
                f"expected {self.fingerprint}, received {actual}"
            )

    def _same_phone_url(self, url: str) -> str:
        absolute = urljoin(self.base_url, url)
        if urlsplit(absolute).hostname != self.host:
            raise YealinkError(f"refusing to contact an external asset host: {absolute}")
        return absolute

    def get(self, url: str) -> tuple[str, str]:
        request = Request(self._same_phone_url(url), headers=self.headers, method="GET")
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.geturl(), response.read().decode(errors="replace")

    def login(self) -> None:
        self.verify_identity()
        self.get(LOGIN_FORM_PATH)
        random_value = f"0.{secrets.randbelow(10**16):016d}"
        _, fresh_login = self.get(
            f"/servlet?m=mod_listener&p=login&q=loginForm&Random={random_value}"
        )
        modulus, exponent = extract_rsa_key(fresh_login)
        session_id = next(
            (cookie.value for cookie in self.cookies if cookie.name == "JSESSIONID"),
            None,
        )
        if not session_id:
            raise YealinkError("the phone did not issue a JSESSIONID cookie")

        key_hex = hashlib.md5(secrets.token_bytes(32), usedforsecurity=False).hexdigest()
        iv_hex = hashlib.md5(secrets.token_bytes(32), usedforsecurity=False).hexdigest()
        prefix = f"0.{secrets.randbelow(10**16):016d}"
        encrypted_password = aes_encrypt_password(
            f"{prefix};{session_id};{self.password}",
            bytes.fromhex(key_hex),
            bytes.fromhex(iv_hex),
        )
        body = urlencode(
            {
                "username": self.username,
                "pwd": encrypted_password,
                "rsakey": rsa_encrypt_text(key_hex, modulus, exponent),
                "rsaiv": rsa_encrypt_text(iv_hex, modulus, exponent),
            }
        ).encode("ascii")
        login_url = self._same_phone_url(
            f"{LOGIN_POST_PATH}&Rajax=0.{secrets.randbelow(10**16):016d}"
        )
        request = Request(
            login_url,
            data=body,
            method="POST",
            headers={
                **self.headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self._same_phone_url(LOGIN_FORM_PATH),
            },
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            result = response.read().decode(errors="replace")
        if re.search(r'"authstatus"\s*:\s*"(?:none|lock)"', result, re.IGNORECASE):
            raise YealinkError("login was rejected or the account is locked; no retry attempted")

    def inspect_multicast(self) -> dict:
        final_url, page = self.get(MULTICAST_PATH)
        if "loginForm" in final_url or 'id="idUsername"' in page:
            raise YealinkError("authenticated session was not retained")
        if "multicast" not in page.lower():
            raise YealinkError("the expected multicast page was not returned")

        parser = PageParser()
        parser.feed(page)
        findings: list[AssetFinding] = []
        for source in dict.fromkeys(parser.scripts):
            asset_url, asset = self.get(source)
            tokens = [token for token in DISCOVERY_TOKENS if token in asset]
            if tokens:
                findings.append(
                    AssetFinding(
                        url=asset_url,
                        size=len(asset),
                        tokens=tokens,
                        contexts=contexts_for(asset),
                    )
                )

        page_data = None
        if parser.res_info:
            try:
                page_data = sanitize(json.loads(parser.res_info))
            except json.JSONDecodeError:
                page_data = parser.res_info[:4000]
        inline = "\n".join(parser.inline_scripts)
        return {
            "host": self.host,
            "page": final_url,
            "certificate_fingerprint": self.fingerprint,
            "controls": [asdict(control) for control in parser.controls],
            "page_data": page_data,
            "script_sources": [self._same_phone_url(source) for source in parser.scripts],
            "inline_contexts": contexts_for(inline),
            "asset_findings": [asdict(finding) for finding in findings],
            "writes_performed": 0,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Yealink management IP address")
    parser.add_argument("--fingerprint", required=True, help="Pinned SHA-256 certificate fingerprint")
    parser.add_argument("--username", required=True, help="Yealink web username")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    password = getpass.getpass("Yealink web password: ")
    try:
        client = YealinkClient(args.host, args.fingerprint, args.username, password)
        client.login()
        report = client.inspect_multicast()
    except (OSError, ValueError, YealinkError) as exc:
        print(f"READ-ONLY INSPECTION FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""

    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        args.output.chmod(0o600)
        print(f"Read-only report written to {args.output}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
