"""Audio preparation and transcoding for 8 kHz G.711 mu-law paging."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)
FRAME_BYTES = 160


class AudioToolMissing(RuntimeError):
    pass


class AudioProcessingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioInfo:
    duration: float
    sample_rate: int
    channels: int
    peak_dbfs: float | None


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise AudioToolMissing(f"{name} is required; install it with: sudo apt install ffmpeg")
    return executable


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise AudioProcessingError(detail) from exc


def cache_dir() -> Path:
    configured = os.environ.get("BELL_AUDIO_CACHE")
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "bell-audio-cache"


def transcode(src: Path) -> Path:
    """Convert an audio file to headerless 8 kHz mono PCMU and cache the result."""
    source = src.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"audio source not found: {source}")
    stat = source.stat()
    key = hashlib.sha256(
        f"{source}\0{stat.st_mtime_ns}\0{stat.st_size}".encode()
    ).hexdigest()
    target_dir = cache_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{key}.ulaw"
    if target.is_file() and target.stat().st_size > 0:
        return target
    temporary = target.with_suffix(".tmp")
    _run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "mulaw",
            str(temporary),
        ]
    )
    temporary.replace(target)
    return target


def load_frames(raw: Path) -> Iterator[bytes]:
    with raw.open("rb") as handle:
        while chunk := handle.read(FRAME_BYTES):
            yield chunk.ljust(FRAME_BYTES, b"\x00")


def probe_audio(src: Path) -> AudioInfo:
    command = [
        _tool("ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels:format=duration",
        "-of",
        "json",
        str(src),
    ]
    data = json.loads(_run(command).stdout)
    streams = data.get("streams", [])
    if not streams:
        raise AudioProcessingError(f"no audio stream found in {src}")
    stream = streams[0]
    duration = float(data.get("format", {}).get("duration", 0.0))
    peak: float | None = None
    analysis = subprocess.run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-i",
            str(src),
            "-af",
            "volumedetect",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in analysis.stderr.splitlines():
        if "max_volume:" in line:
            with contextlib.suppress(ValueError):
                peak = float(line.split("max_volume:", 1)[1].split("dB", 1)[0].strip())
    return AudioInfo(duration, int(stream["sample_rate"]), int(stream["channels"]), peak)


def prep(src: Path, dst: Path, target_dbfs: float = -3.0, max_seconds: float = 20.0) -> Path:
    """Trim, peak-normalize and fade audio into a WAV suitable for later transcoding."""
    info = probe_audio(src)
    if info.duration > max_seconds:
        LOGGER.warning(
            "audio_exceeds_recommended_duration",
            extra={"duration": info.duration, "max_seconds": max_seconds, "source": str(src)},
        )
    destination = dst.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Normalize to target peak, trim silence, reset timestamps, then apply click-preventing fades.
    gain = 0.0 if info.peak_dbfs is None or info.peak_dbfs == float("-inf") else target_dbfs - info.peak_dbfs
    filters = (
        "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-45dB:"
        "stop_periods=-1:stop_duration=0.05:stop_threshold=-45dB,"
        f"volume={gain:.3f}dB,afade=t=in:st=0:d=0.01,"
        "areverse,afade=t=in:st=0:d=0.01,areverse"
    )
    _run(
        [
            _tool("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-af",
            filters,
            "-ac",
            "1",
            "-ar",
            "8000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe_parser = subparsers.add_parser("probe", help="inspect an audio file")
    probe_parser.add_argument("source", type=Path)
    transcode_parser = subparsers.add_parser("transcode", help="create cached raw PCMU")
    transcode_parser.add_argument("source", type=Path)
    prep_parser = subparsers.add_parser("prep", help="prepare a bell sound")
    prep_parser.add_argument("source", type=Path)
    prep_parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    if args.command == "probe":
        print(json.dumps(asdict(probe_audio(args.source)), indent=2))
    elif args.command == "transcode":
        print(transcode(args.source))
    else:
        print(prep(args.source, args.destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
