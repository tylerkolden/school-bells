"""Concurrent multi-protocol page delivery with required/optional endpoint semantics."""

from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from bell.audio import CodecName, codec_spec, load_frames, transcode
from bell.config import BellConfig, BellEvent, Destination, Zone
from bell.monitor import EndpointRegistry
from bell.protocols.base import DeliveryOutcome
from bell.protocols.http import WebhookClient
from bell.protocols.sip import SIPClient
from bell.transmit import DestinationEndpoint, Transmitter
from bell.wire import get_wire_format
from bell.wire.plain_rtp import PlainRTP


class PageDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    outcomes: tuple[DeliveryOutcome, ...]
    cancelled: bool
    duration_seconds: float

    @property
    def successful(self) -> bool:
        return not self.cancelled and all(item.success for item in self.outcomes)


class DeliveryManager:
    def __init__(self, config: BellConfig, registry: EndpointRegistry) -> None:
        self.config = config
        self.registry = registry

    def update_config(self, config: BellConfig) -> None:
        self.config = config

    def deliver(
        self,
        raw_audio: Path,
        event: BellEvent,
        zone: Zone,
        cancel_event: threading.Event,
        *,
        sound_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> DeliveryReport:
        started = time.monotonic()
        destinations = [
            self.config.destination_map[name]
            for name in zone.destinations
            if self.config.destination_map[name].enabled
        ]
        if not destinations:
            raise PageDeliveryError(f"zone {zone.name!r} has no enabled destinations")
        outcomes: list[DeliveryOutcome] = []
        ready: list[Destination] = []
        for destination in destinations:
            if self.registry.should_attempt(destination):
                ready.append(destination)
            else:
                outcomes.append(
                    DeliveryOutcome(
                        destination.name,
                        destination.protocol,
                        False,
                        "circuit_open",
                        "optional destination skipped during circuit-breaker cooldown",
                        0,
                        0.0,
                    )
                )
        source_audio = self.config.sounds_path / (sound_name or event.sound)

        def media_for(codec: CodecName) -> Path:
            return raw_audio if codec == "pcmu" else transcode(source_audio, codec)

        multicast_groups: dict[tuple[str, CodecName], list[Destination]] = {}
        others: list[Destination] = []
        for destination in ready:
            if destination.protocol == "multicast":
                wire_name = destination.wire_format or self.config.settings.wire_format
                codec = destination.codecs[0]
                multicast_groups.setdefault((wire_name, codec), []).append(destination)
            else:
                others.append(destination)
        task_count = len(multicast_groups) + len(others)
        if task_count:
            with ThreadPoolExecutor(max_workers=min(8, task_count), thread_name_prefix="delivery") as pool:
                futures = []
                for (wire_name, codec), group in multicast_groups.items():
                    futures.append(
                        pool.submit(
                            self._multicast,
                            wire_name,
                            codec,
                            group,
                            media_for(codec),
                            zone.channel,
                            cancel_event,
                        )
                    )
                for destination in others:
                    if destination.protocol == "sip":
                        sip_media = {
                            codec: media_for(codec) for codec in destination.codecs
                        }
                        futures.append(
                            pool.submit(
                                SIPClient(destination, self.config.settings.interface_ip).page,
                                sip_media,
                                cancel_event,
                            )
                        )
                    else:
                        payload = {
                            "event": event.label,
                            "sound": sound_name or event.sound,
                            "zone": zone.name,
                            "channel": zone.channel,
                            "priority": event.priority,
                            "timestamp": time.time(),
                        }
                        futures.append(
                            pool.submit(
                                WebhookClient().trigger,
                                destination,
                                payload,
                                idempotency_key or secrets.token_hex(16),
                            )
                        )
                for future in as_completed(futures):
                    result = future.result()
                    if isinstance(result, list):
                        outcomes.extend(result)
                    else:
                        outcomes.append(result)
        for outcome in outcomes:
            self.registry.record(outcome)
        required_names = {item.name for item in destinations if item.required}
        failed_required = [
            item for item in outcomes if item.destination in required_names and not item.success
        ]
        if failed_required:
            summary = "; ".join(f"{item.destination}: {item.detail}" for item in failed_required)
            raise PageDeliveryError(f"required delivery failed: {summary}")
        if outcomes and not any(item.success for item in outcomes):
            summary = "; ".join(f"{item.destination}: {item.detail}" for item in outcomes)
            raise PageDeliveryError(f"no destination accepted the page: {summary}")
        return DeliveryReport(
            tuple(sorted(outcomes, key=lambda item: item.destination)),
            cancel_event.is_set(),
            time.monotonic() - started,
        )

    def _multicast(
        self,
        wire_name: str,
        codec: CodecName,
        destinations: list[Destination],
        raw_audio: Path,
        zone_channel: int,
        cancel_event: threading.Event,
    ) -> list[DeliveryOutcome]:
        started = time.monotonic()
        endpoints = [
            DestinationEndpoint(item.name, item.group or "", item.port, item.ttl)
            for item in destinations
        ]
        channel = 0 if wire_name == "plain_rtp" else zone_channel
        spec = codec_spec(codec)
        wire = (
            PlainRTP(spec.payload_type)
            if wire_name == "plain_rtp"
            else get_wire_format(wire_name, self.config.poly_spec, spec.payload_type)
        )
        try:
            with Transmitter(
                wire,
                endpoints,
                self.config.settings.interface_ip,
                timestamp_step=spec.rtp_clock_rate // 50,
            ) as transmitter:
                result = transmitter.send(
                    load_frames(raw_audio, spec.frame_bytes, spec.padding_byte),
                    channel,
                    cancel_event,
                )
            return [
                DeliveryOutcome(
                    item.name,
                    "multicast",
                    not result.cancelled and item.name not in result.destination_errors,
                    (
                        "cancelled"
                        if result.cancelled
                        else "failed" if item.name in result.destination_errors else "sent"
                    ),
                    (
                        result.destination_errors[item.name]
                        if item.name in result.destination_errors
                        else (
                            f"{result.packet_count} {codec.upper()} packets, "
                            f"{result.destination_bytes[item.name]} bytes"
                        )
                    ),
                    1,
                    time.monotonic() - started,
                )
                for item in destinations
            ]
        except (OSError, RuntimeError, ValueError) as exc:
            return [
                DeliveryOutcome(
                    item.name,
                    "multicast",
                    False,
                    "failed",
                    str(exc),
                    1,
                    time.monotonic() - started,
                )
                for item in destinations
            ]
