from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import bell.audio as audio


def make_wav(path: Path, duration: float = 0.21) -> Path:
    rate = 8000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        samples = [int(12000 * math.sin(2 * math.pi * 880 * index / rate)) for index in range(int(rate * duration))]
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def test_transcode_frame_count_and_cache(tmp_path: Path, monkeypatch) -> None:
    source = make_wav(tmp_path / "tone.wav", 0.2)
    monkeypatch.setenv("BELL_AUDIO_CACHE", str(tmp_path / "cache"))
    calls = 0
    original = audio._run

    def counting(command):
        nonlocal calls
        calls += 1
        return original(command)

    monkeypatch.setattr(audio, "_run", counting)
    raw = audio.transcode(source)
    first_calls = calls
    assert len(list(audio.load_frames(raw))) == 10
    assert audio.transcode(source) == raw
    assert calls == first_calls


def test_common_telephony_codecs_produce_twenty_ms_frames(
    tmp_path: Path, monkeypatch
) -> None:
    source = make_wav(tmp_path / "tone.wav", 0.2)
    monkeypatch.setenv("BELL_AUDIO_CACHE", str(tmp_path / "cache"))
    for codec in ("pcmu", "pcma", "g722"):
        spec = audio.codec_spec(codec)
        raw = audio.transcode(source, codec)  # type: ignore[arg-type]
        frames = list(audio.load_frames(raw, spec.frame_bytes))
        assert len(frames) == 10
        assert all(len(frame) == spec.frame_bytes for frame in frames)


def test_final_pcmu_frame_is_padded_with_digital_silence(tmp_path: Path) -> None:
    raw = tmp_path / "partial.ulaw"
    raw.write_bytes(b"a" * 161)
    frames = list(audio.load_frames(raw))
    assert frames == [b"a" * 160, b"a" + b"\xff" * 159]


def test_stateful_codec_final_frame_is_not_invented(tmp_path: Path) -> None:
    raw = tmp_path / "partial.g722"
    raw.write_bytes(b"a" * 161)
    frames = list(audio.load_frames(raw, 160, None))
    assert frames == [b"a" * 160, b"a"]


def test_probe_and_prep(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "tone.wav", 0.25)
    info = audio.probe_audio(source)
    assert 0.24 <= info.duration <= 0.26
    assert info.sample_rate == 8000
    assert info.channels == 1
    prepared = audio.prep(source, tmp_path / "prepared.wav")
    prepared_info = audio.probe_audio(prepared)
    assert prepared_info.sample_rate == 8000
    assert prepared_info.channels == 1
