from __future__ import annotations

import base64
import shutil
import subprocess

import pytest

from scripts.yealink_fleet import (
    PageParser,
    aes_encrypt_password,
    contexts_for,
    extract_rsa_key,
    normalize_fingerprint,
    redact_context,
    rsa_encrypt_text,
)

MODULUS = (
    "D36E437448D97C95DC0D217FBF1521F984DCE34459ED0119B4D91172A23DC35"
    "B06409A7844F3C17A1ED5833290B0D5039BA5E41A863FCDAD426D54D2FD2ADB311"
    "183298B75D5E46BE9BC80A2341212CF9721BF3F17EC06365AFB4CEB7DDA393877"
    "635BCDA97F93362DA7D731A9149A445BD86A6670B799487CAA8F8D0F7DC091"
)


def test_normalize_fingerprint() -> None:
    value = "DC:CC:4A:86:18:E3:AB:3D:60:38:C8:8B:1B:C2:04:9D:" \
        "C9:21:05:A6:16:6C:9C:F9:FB:AC:D5:D5:94:25:85:98"
    assert normalize_fingerprint(value) == value.replace(":", "")


def test_normalize_fingerprint_rejects_invalid_length() -> None:
    with pytest.raises(ValueError, match="64 hex digits"):
        normalize_fingerprint("AA:BB")


def test_extract_rsa_key() -> None:
    page = f'var g_rsa_n = "{MODULUS}"; var g_rsa_e = "010001";'
    assert extract_rsa_key(page) == (MODULUS, "010001")


def test_rsa_encryption_is_pkcs1_randomized() -> None:
    first = rsa_encrypt_text("0123456789abcdef0123456789abcdef", MODULUS, "010001")
    second = rsa_encrypt_text("0123456789abcdef0123456789abcdef", MODULUS, "010001")
    assert first != second
    assert len(first) <= 256
    assert len(first) % 2 == 0
    assert int(first, 16) < int(MODULUS, 16)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl unavailable")
def test_aes_encryption_round_trip() -> None:
    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    iv = bytes.fromhex("ffeeddccbbaa99887766554433221100")
    plaintext = "0.123;SESSION;password"
    encrypted = base64.b64decode(aes_encrypt_password(plaintext, key, iv))
    completed = subprocess.run(
        [
            shutil.which("openssl") or "openssl",
            "enc",
            "-d",
            "-aes-128-cbc",
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=encrypted,
        capture_output=True,
        check=True,
    )
    assert completed.stdout.rstrip(b"\x00").decode() == plaintext


def test_page_parser_redacts_sensitive_fields_and_finds_scripts() -> None:
    parser = PageParser()
    parser.feed(
        '<script src="/js/page.js"></script>'
        '<script>var ReceivePriority = 25;</script>'
        '<input name="Address1" value="239.1.1.1:601">'
        '<input type="hidden" name="token" value="secret">'
        '<div id="_RES_INFO_">{"enabled":true}</div>'
    )
    assert parser.scripts == ["/js/page.js"]
    assert "ReceivePriority" in parser.inline_scripts[0]
    assert parser.controls[0].value == "239.1.1.1:601"
    assert parser.controls[1].value == "<redacted>"
    assert parser.res_info == '{"enabled":true}'


def test_contexts_for_limits_output_to_matching_regions() -> None:
    text = "a" * 600 + "ReceivePriority" + "b" * 1600
    contexts = contexts_for(text)
    assert len(contexts) == 1
    assert "ReceivePriority" in contexts[0]


def test_redact_context_hides_tokens_and_sessions() -> None:
    text = 'g_strToken="abc123"; JSESSIONID=XYZ987; ReceivePriority=25'
    redacted = redact_context(text)
    assert "abc123" not in redacted
    assert "XYZ987" not in redacted
    assert "ReceivePriority=25" in redacted
