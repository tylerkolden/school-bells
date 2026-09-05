from pathlib import Path

import pytest

from scripts.check_yealink_barge import PARAMETER, assess_config, main


@pytest.mark.parametrize("value", ["0", '"0"', "'0'", " 0 # office override"])
def test_explicit_disabled(value: str) -> None:
    assert assess_config(f"{PARAMETER} = {value}")["status"] == "disabled"


@pytest.mark.parametrize("value", ["1", "10", "23", "24", "25", "31"])
def test_enabled_thresholds(value: str) -> None:
    assert assess_config(f"{PARAMETER}={value}")["status"] == "enabled"


@pytest.mark.parametrize("text", [
    "", f"# {PARAMETER}=0", "multicast.receive_priority.enable=0",
    f"{PARAMETER}=0\n{PARAMETER}=25", f"{PARAMETER}=32", f"{PARAMETER}=-1",
    f"{PARAMETER}=", f'{PARAMETER}="0', f"{PARAMETER}=Disabled",
])
def test_unknown_is_not_a_pass(text: str) -> None:
    assert assess_config(text)["status"] == "unknown"


def test_does_not_expose_export_secrets() -> None:
    report = assess_config(f"account.1.password=secret\n{PARAMETER}=sensitive-value")
    assert "secret" not in str(report)
    assert "sensitive-value" not in str(report)


@pytest.mark.parametrize(("value", "code"), [("0", 0), ("25", 1), ("bad", 2)])
def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str, code: int) -> None:
    config = tmp_path / "phone.cfg"
    config.write_text(f"{PARAMETER}={value}", encoding="utf-8-sig")
    assert main([str(config)]) == code
    assert '"status"' in capsys.readouterr().out


def test_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "missing.cfg")]) == 2
    assert "unknown" in capsys.readouterr().out


def test_office_provisioning_fragment() -> None:
    fragment = Path(__file__).resolve().parents[1] / "deploy/yealink/office-no-barge.cfg"
    assert assess_config(fragment.read_text())["status"] == "disabled"
