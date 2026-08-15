from __future__ import annotations

import json
import logging
from pathlib import Path

from bell.logging_setup import configure_logging


def test_structured_rotating_log_shape(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    logging.getLogger("bell.test").info("transmission", extra={"zone": "indoors", "packets": 3})
    for handler in logging.getLogger().handlers:
        handler.flush()
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["message"] == "transmission"
    assert record["zone"] == "indoors"
    assert record["packets"] == 3
    assert record["timestamp"].endswith("+00:00")
