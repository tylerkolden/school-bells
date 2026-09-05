"""Audit a Yealink text configuration export without contacting or changing phones."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PARAMETER = "multicast.receive_priority.priority"


def assess_config(text: str) -> dict[str, str]:
    """Check explicit assignments only; missing/ambiguous values are not a pass.

    This checks one export, not the effective merge of provider configuration layers
    or actual handset behavior. Never include unrelated settings or secrets in output.
    """
    values: list[str] = []
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == PARAMETER:
            value = value.split("#", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if not values:
        status, detail = "unknown", "Paging Barge is absent; inspect the effective phone setting."
    elif len(set(values)) != 1 or any(value not in {str(n) for n in range(32)} for value in values):
        status, detail = "unknown", "Paging Barge has conflicting or unsupported assignments."
    elif values[0] == "0":
        status, detail = "disabled", "Paging Barge is explicitly disabled in this file."
    else:
        status, detail = "enabled", "Paging Barge permits eligible pages to interrupt active calls."
    return {
        "parameter": PARAMETER,
        "status": status,
        "detail": detail,
        "verification": "Confirm the effective setting and witness idle, active-call, and emergency tests.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Decrypted text CFG export (kept local)")
    args = parser.parse_args(argv)
    try:
        report = assess_config(args.config.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        # An export can contain SIP credentials: never echo its contents or exceptions.
        print(json.dumps({"status": "unknown", "detail": "Cannot read a UTF-8 text CFG export."}))
        return 2
    print(json.dumps(report, indent=2))
    return {"disabled": 0, "enabled": 1, "unknown": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
