#!/usr/bin/env python3
"""Non-disruptive post-deployment acceptance checks."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bell.audio import load_frames, transcode  # noqa: E402
from bell.config import ConfigLoadError, load_config  # noqa: E402
from bell.probe import load_capture, parse_rtp  # noqa: E402
from bell.service import clock_sync_status, interface_present  # noqa: E402
from bell.transmit import DestinationEndpoint, Transmitter  # noqa: E402
from bell.wire.plain_rtp import PlainRTP  # noqa: E402
from bell.wire.poly_group_page import PolyGroupPage  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    reason: str


def command_check(command: list[str]) -> tuple[bool, str]:
    executable = shutil.which(command[0])
    if not executable:
        return False, f"{command[0]} is unavailable"
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    detail = result.stdout.strip() or result.stderr.strip()
    return result.returncode == 0, detail


def loopback_check(raw: Path, interface_ip: str) -> tuple[bool, str]:
    test_group = "239.255.254.250"
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("", 0))
    listener.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(test_group) + socket.inet_aton(interface_ip),
    )
    listener.settimeout(3)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    def receive() -> None:
        try:
            while len(received) < 5:
                received.append(listener.recvfrom(2048)[0])
        except TimeoutError:
            pass

    thread = threading.Thread(target=receive)
    thread.start()
    frames = list(load_frames(raw))[:5]
    with Transmitter(
        PlainRTP(),
        [DestinationEndpoint("loopback", test_group, port)],
        interface_ip,
        loopback=True,
    ) as transmitter:
        transmitter.send(frames, 0)
    thread.join(timeout=4)
    listener.close()
    if len(received) != len(frames):
        return False, f"received {len(received)} of {len(frames)} packets"
    parsed = [parse_rtp(packet) for packet in received]
    if [item.payload for item in parsed] != frames:
        return False, "RTP payload integrity mismatch"
    if any((b.sequence - a.sequence) & 0xFFFF != 1 for a, b in pairwise(parsed)):
        return False, "sequence numbers are not contiguous"
    if any((b.timestamp - a.timestamp) & 0xFFFFFFFF != 160 for a, b in pairwise(parsed)):
        return False, "timestamps do not advance by 160"
    return True, f"5 PCMU frames passed end to end on dedicated group {test_group}"


def poly_golden_check(fixture_dir: Path) -> tuple[bool, str]:
    wire = PolyGroupPage()
    if not wire.calibrated:
        return False, "not calibrated - follow docs/CAPTURE.md"
    try:
        for channel in (23, 24):
            path = fixture_dir / f"ch{channel}.bin"
            packets = load_capture(path)
            if not packets:
                return False, f"{path.name} contains no packets"
            parsed = parse_rtp(packets[0])
            rebuilt = wire.build_packet(
                parsed.payload,
                parsed.sequence,
                parsed.timestamp,
                parsed.ssrc,
                parsed.marker,
                channel,
            )
            if rebuilt != packets[0]:
                return False, f"generated channel {channel} packet does not match {path.name}"
    except (OSError, ValueError, RuntimeError) as exc:
        return False, f"golden fixture verification failed: {exc}"
    return True, "calibrated builder exactly matches channel 23 and 24 golden captures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    args = parser.parse_args()
    checks: list[Check] = []

    enabled, enabled_detail = command_check(["systemctl", "is-enabled", "bell-system"])
    active, active_detail = command_check(["systemctl", "is-active", "bell-system"])
    checks.append(Check("systemd service", enabled and active, f"enabled={enabled_detail}; active={active_detail}"))

    try:
        with urllib.request.urlopen(args.health_url, timeout=3) as response:
            health = json.load(response)
        unhealthy = health.get("unhealthy_required_endpoints", [])
        health_ok = (
            response.status == 200
            and health.get("config_valid") is True
            and health.get("ready") is True
            and "kill_switch" in health
            and not unhealthy
        )
        health_reason = (
            f"HTTP {response.status}; status={health.get('status')}; "
            f"kill_switch={health.get('kill_switch')}; unhealthy_required={unhealthy}"
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        health_ok, health_reason = False, str(exc)
    checks.append(Check("health endpoint", health_ok, health_reason))
    metrics_url = args.health_url.rsplit("/", 1)[0] + "/metrics"
    try:
        with urllib.request.urlopen(metrics_url, timeout=3) as response:
            metrics = response.read().decode("utf-8")
        metrics_ok = response.status == 200 and "bell_ready 1" in metrics
        metrics_reason = f"HTTP {response.status}; bell_ready={'present' if 'bell_ready 1' in metrics else 'not ready'}"
    except (OSError, urllib.error.URLError) as exc:
        metrics_ok, metrics_reason = False, str(exc)
    checks.append(Check("metrics endpoint", metrics_ok, metrics_reason))

    synced, sync_detail = clock_sync_status()
    rtc_path = Path("/sys/class/rtc/rtc0")

    try:
        config = load_config(args.config_dir)
        sound_names = {
            name
            for schedule in config.schedules
            for event in schedule.events
            for name in (event.sound, event.pre_tone)
            if name
        } | {
            name
            for item in config.standing_items
            if item.enabled
            for name in (item.sound, item.pre_tone)
            if name
        }
        codecs = {
            codec
            for destination in config.destinations
            if destination.enabled and destination.protocol in {"multicast", "sip"}
            for codec in destination.codecs
        }
        for name in sorted(sound_names):
            for codec in sorted(codecs):
                transcode(config.sounds_path / name, codec)
        pcmu_files = [transcode(config.sounds_path / name) for name in sorted(sound_names)]
        checks.append(
            Check(
                "configuration/audio",
                True,
                f"valid; prepared {len(sound_names)} sounds across {len(codecs)} codecs",
            )
        )
    except (ConfigLoadError, OSError, RuntimeError) as exc:
        print(f"Configuration unavailable: {exc}")
        checks.append(Check("configuration/audio", False, str(exc)))
        config = None
        pcmu_files = []

    rtc_required = config.settings.rtc_required if config else False
    clock_ok = synced is True and (not rtc_required or rtc_path.exists())
    checks.append(
        Check(
            "clock and RTC",
            clock_ok,
            f"{sync_detail}; RTC={'present' if rtc_path.exists() else 'not detected'}; required={rtc_required}",
        )
    )

    if pcmu_files:
        if config is None:
            passed, reason = False, "configuration unavailable"
        else:
            try:
                passed, reason = loopback_check(pcmu_files[0], config.settings.interface_ip)
            except OSError as exc:
                passed, reason = False, f"multicast loopback setup failed: {exc}"
        checks.append(Check("loopback RTP", passed, reason))
    else:
        checks.append(Check("loopback RTP", False, "no transcoded sound available"))

    fixture_dir = ROOT / "tests" / "fixtures"
    golden, golden_reason = poly_golden_check(fixture_dir)
    checks.append(Check("Poly calibration", golden, golden_reason))

    if config:
        present, detail = interface_present(config.settings.interface_ip)
        multicast = socket.has_ipv6 or hasattr(socket, "IP_MULTICAST_IF")
        checks.append(Check("wired multicast interface", present and multicast, f"{detail}; multicast socket support={multicast}"))
        usage = shutil.disk_usage(config.state_path.parent)
        free_gb = usage.free / (1024**3)
        log_files = list(config.log_path.glob("bell-system.jsonl*")) if config.log_path.exists() else []
        rotation_sane = len(log_files) <= 6 and all(path.stat().st_size <= 11 * 1024 * 1024 for path in log_files)
        checks.append(Check("disk and logs", free_gb >= 1 and rotation_sane, f"{free_gb:.1f} GiB free; {len(log_files)} rotated log files"))
    else:
        checks.extend([Check("wired multicast interface", False, "configuration unavailable"), Check("disk and logs", False, "configuration unavailable")])

    width = max(len(item.name) for item in checks)
    print(f"{'CHECK':<{width}}  RESULT  REASON")
    print(f"{'-' * width}  ------  ------")
    for item in checks:
        print(f"{item.name:<{width}}  {'PASS' if item.passed else 'FAIL':<6}  {item.reason}")
    passed_count = sum(item.passed for item in checks)
    print(f"\n{passed_count}/{len(checks)} checks passed")
    return 0 if passed_count == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
