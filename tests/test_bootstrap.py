from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "bootstrap.sh"


def load_embedded_bootstrap() -> SimpleNamespace:
    script = BOOTSTRAP.read_text(encoding="utf-8")
    marker = "<<'PY_BOOTSTRAP'\n"
    source = script.split(marker, maxsplit=1)[1].split("\nPY_BOOTSTRAP", maxsplit=1)[0]
    namespace: dict[str, object] = {"__name__": "bootstrap_test"}
    exec(compile(source, str(BOOTSTRAP), "exec"), namespace)
    return SimpleNamespace(**namespace)


def release_metadata() -> dict[str, object]:
    tag = "v0.2.3"
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "immutable": True,
        "html_url": f"https://github.com/tylerkolden/school-bells/releases/tag/{tag}",
        "author": {"login": "github-actions[bot]"},
        "assets": [
            {
                "id": 42,
                "name": f"bell-system-{tag}.tar.gz",
                "size": 1024,
                "digest": "sha256:" + "a" * 64,
                "uploader": {"login": "github-actions[bot]"},
            },
            {"id": 43, "name": "install-school-bells.sh"},
        ],
    }


def test_bootstrap_has_fixed_trust_policy_and_preserves_release_identity() -> None:
    script = BOOTSTRAP.read_text(encoding="utf-8")
    assert "REPOSITORY=tylerkolden/school-bells" in script
    assert "EXPECTED_PUBLISHER='github-actions[bot]'" in script
    assert 'value.get("immutable") is not True' in script
    assert "ALLOWED_DOWNLOAD_HOSTS" in script
    assert "sha256:" in script
    assert "member.isfile() or member.isdir()" in script
    assert "BELL_RELEASE_VERSION" in script
    assert "BELL_RELEASE_COMMIT" in script
    assert "BELL_RELEASE_DIGEST" in script
    assert 'BELL_INTERFACE_IP="$interface_ip"' in script
    assert "ip -4 route get" in script
    assert "curl |" not in script and "wget |" not in script
    subprocess.run(["bash", "-n"], input=script.encode(), check=True)
    load_embedded_bootstrap()


def test_bootstrap_release_validation_fails_closed() -> None:
    bootstrap = load_embedded_bootstrap()
    value = release_metadata()
    checked = bootstrap.validate_release(
        value, "tylerkolden/school-bells", "github-actions[bot]", 128 * 1024 * 1024
    )
    assert checked["tag"] == "v0.2.3"
    assert checked["asset_name"] == "bell-system-v0.2.3.tar.gz"

    for field, replacement, message in (
        ("immutable", False, "not immutable"),
        ("draft", True, "stable releases"),
        ("author", {"login": "repository-owner"}, "approved workflow"),
        ("html_url", "https://example.invalid/release", "fixed repository"),
    ):
        rejected = release_metadata()
        rejected[field] = replacement
        with pytest.raises(bootstrap.BootstrapError, match=message):
            bootstrap.validate_release(
                rejected,
                "tylerkolden/school-bells",
                "github-actions[bot]",
                128 * 1024 * 1024,
            )


def test_bootstrap_safe_extract_accepts_manifest_and_rejects_links(tmp_path: Path) -> None:
    bootstrap = load_embedded_bootstrap()
    valid_archive = tmp_path / "valid.tar.gz"
    manifest = json.dumps(
        {"schema": 1, "tag": "v0.2.3", "version": "0.2.3", "commit": "a" * 40}
    ).encode()
    installer = b"#!/usr/bin/env bash\n"
    with tarfile.open(valid_archive, "w:gz") as output:
        for name, content in (("RELEASE.json", manifest), ("deploy/install.sh", installer)):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            output.addfile(member, io.BytesIO(content))
    destination = tmp_path / "valid"
    destination.mkdir()
    assert bootstrap.safe_extract(valid_archive, destination)["tag"] == "v0.2.3"

    link_archive = tmp_path / "link.tar.gz"
    with tarfile.open(link_archive, "w:gz") as output:
        member = tarfile.TarInfo("deploy/install.sh")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        output.addfile(member)
    rejected = tmp_path / "rejected"
    rejected.mkdir()
    with pytest.raises(bootstrap.BootstrapError, match="link or special"):
        bootstrap.safe_extract(link_archive, rejected)


def test_release_publishes_and_attests_the_bootstrap_asset() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "install -m 0755 deploy/bootstrap.sh dist/release/install-school-bells.sh" in workflow
    assert "subject-path: dist/release/*" in workflow
    assert '"dist/release/install-school-bells.sh"' in workflow
