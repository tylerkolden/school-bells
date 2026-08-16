# Deriving the Poly Group Page wire format

## Why this is required

The Algo 8186 is configured in **Poly Group Page** mode. This protocol is RTP-based but adds a
proprietary RTP header extension carrying the group/channel. Plain RTP will not select channels,
and an invented extension can fail silently. `bell/wire/poly_group_page.py` therefore refuses to
send until its `PolySpec` is populated from real packets.

This procedure is deliberately complete so it can be repeated years from now.

## Prerequisites and safety

Use a **wired** laptop or Raspberry Pi on the phone VLAN (`192.168.10.0/24`). Wi-Fi multicast
is unreliable and can yield an empty capture even while the paging source is working.

Before transmitting test pages, open the Algo 8186 at `192.168.10.32` and temporarily uncheck
**Group 24**. Record that you made this change. This prevents the outdoor horn from sounding.
Re-enable Group 24 immediately after capture.

On a Yealink T31P, configure a DSS key as **Paging List** and ensure the list contains channels
23 (Indoors) and 24 (Outdoors). Pick a quiet test window and notify staff nearby.

## Recommended: guided capture on the Raspberry Pi

The web console automates capture, privacy filtering, comparison, persistence, and runtime reload:

1. In **Setup → Safety & settings**, enable the transmission kill switch. This prevents scheduled
   or manual bells from competing with the known external pages used for calibration.
2. In **Setup → Destinations**, save the multicast address and UDP port that the phones use. Choose
   **Poly Group Page**, then select **Run guided Poly capture**.
3. Choose the enabled multicast destination and enter the exact channel you will page from a known
   Poly/Yealink source.
4. Select **Start 10-second capture** and immediately trigger that page. Repeat for at least three
   distinct channels. Channels 23, 24, and 25 are the normal school defaults.
5. The Pi accepts only RTP version 2, PCMU payload type 0, and a consistent RTP extension layout.
   It discards audio payloads immediately and stores only RTP headers.
6. If exactly one full-byte channel position and constant values for every other extension byte are
   proven across all packets, review the candidate, confirm the known-channel labels, and activate.
7. Activation archives header-only evidence and SHA-256 hashes under
   `state/poly-calibration/verified`, writes the derived mapping to `settings.yaml`, validates the
   complete configuration, reloads the service, and keeps the kill switch enabled.
8. Restore horn subscriptions, then deliberately disable the kill switch only after reviewing
   System status and running `python scripts/acceptance.py`.

If capture reports that the service cannot listen on UDP port 601, update to release v0.6.1 or
newer and restart `bell-system`. Linux reserves ports below 1024; the production unit grants only
`CAP_NET_BIND_SERVICE` so the unprivileged `bell` process can open that configured UDP port. It does
not grant raw-packet capture or network-administration access.

The wizard fails closed on silence, mixed layouts, non-PCMU traffic, independently changing
extension bytes, an address/port change after capture, or fewer than three known channels. It never
guesses a proprietary header.

## Manual fallback

Use this only when the guided workflow reports an unsupported or ambiguous extension layout.

On the wired host, install the project (the probe itself only needs Python's standard library).
Replace `<wired-ip>` with the host's address:

```bash
python -m bell.probe --iface <wired-ip> --count 200 --save ch23.bin
```

Press the Paging List key, choose **Indoors/channel 23**, and speak for five seconds. Repeat:

```bash
python -m bell.probe --iface <wired-ip> --count 200 --save ch24.bin
```

Choose **Outdoors/channel 24** and speak for five seconds. The probe prints the first 48 bytes,
parsed RTP fields, extension profile, extension word count, raw extension bytes, payload size,
and observed packet time.

Compare the first packet while ignoring sequence, timestamp, and SSRC:

```bash
python -m bell.probe --compare ch23.bin ch24.bin
```

The channel-carrying byte should change from decimal 23 to 24. Confirm the difference is inside
the extension bytes, not voice payload. If multiple bytes change, capture again with silence and
several channels; do not guess.

## Encode a manually reviewed result

In `bell/wire/poly_group_page.py`, set `SPEC` using only observed values:

```python
SPEC = PolySpec(
    extension_profile_id=0x0000,  # replace with captured value
    extension_word_count=1,      # replace with captured value
    mappings=(
        (0, "channel"),          # replace offset with captured channel byte
        (1, 0x00),               # include every captured constant byte
        (2, 0x00),
        (3, 0x00),
    ),
)
```

The numbers above are placeholders demonstrating syntax, **not protocol values**. Do not commit
them unchanged. Copy `ch23.bin` and `ch24.bin` into `tests/fixtures/` as golden captures. Add a
test that builds matching packets and asserts byte-for-byte equality after masking only sequence,
timestamp, and SSRC. The extension profile, length, marker, channel byte, constants, header size,
and payload offset must match exactly.

Run the full tests and `python scripts/acceptance.py`. Check 6 must pass. Finally, return to the
Algo at `192.168.10.32` and **re-check Group 24**. Verify the change was restored.
