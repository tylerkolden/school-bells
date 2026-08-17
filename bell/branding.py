"""Bounded logo validation and content rewriting for appliance branding."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class BrandingError(RuntimeError):
    pass


def _recognized_image(path: Path) -> bool:
    header = path.read_bytes()[:16]
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


def normalize_logo(source: Path, destination: Path, *, timeout_seconds: float = 15.0) -> None:
    """Decode an allowlisted raster image and rewrite it as a bounded PNG."""
    if not source.is_file() or not _recognized_image(source):
        raise BrandingError("Logo must be a valid PNG, JPEG, or WebP image")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise BrandingError("FFmpeg is required to validate and rewrite logo images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=".logo-", suffix=".png", delete=False
        ) as handle:
            temporary = Path(handle.name)
        command = [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-max_alloc",
            str(64 * 1024 * 1024),
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease",
            "-an",
            "-y",
            str(temporary),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            raise BrandingError(result.stderr.strip() or "Logo image could not be decoded")
        if temporary.stat().st_size > 2 * 1024 * 1024:
            raise BrandingError("Normalized logo exceeds the 2 MiB storage limit")
        temporary.replace(destination)
        temporary = None
    except subprocess.TimeoutExpired as exc:
        raise BrandingError("Logo processing exceeded the safety timeout") from exc
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
