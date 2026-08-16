# Verifying Poly Group Page on the school VLAN

The Algo 8186 is configured for **Poly Group Page**, and the Yealink T31P phones send the
Poly PTT/Page datagram format documented by Poly Engineering Advisory 70568. This is not
RFC 3550 RTP: treating its first bytes as an RTP header produces misleading `RTPv0 PT=...`
output.

The production encoder remains fail-closed until controlled captures prove that the site's
traffic follows this contract. Do not hand-author a calibration.

## Proven school packet contract

Live traffic from the school's Yealink at `192.168.10.198` to `239.255.255.255:601` showed:

- 20-byte control datagrams and 346-byte G.722 transmit datagrams;
- opcode `0x0f` for alert, `0x10` for audio, and `0xff` for end;
- a 20-byte identity header: opcode, encoded channel, 32-bit sender ID, caller-ID length,
  and a fixed 13-byte caller-ID field;
- a 6-byte audio header: codec type, flags, and 32-bit sample count;
- codec type `0x09` for G.722;
- one 160-byte frame in the first audio packet and previous+current 160-byte frames in later
  packets (186 and 346 bytes total respectively);
- Page group 25 encoded as channel 50. Controlled captures of three groups must prove the
  general page mapping before activation.

Poly's published contract says Page groups 1–25 are encoded as channels 26–50, so the expected
mapping is `encoded_channel = group + 25`. The wizard derives and checks that bias from the live
captures rather than assuming it.

## Guided capture on the Raspberry Pi

1. Open **Setup → Destinations**.
2. Configure the Pi's wired phone-VLAN address and the destination
   `239.255.255.255:601` with **Poly Group Page** and the codec currently used by the phones.
   The school currently uses **G.722**.
3. Enable the transmission kill switch. Capture never disables this guard.
4. Select **Run guided Poly capture**.
5. Enter a known group, click **Start 10-second capture**, and immediately originate a muted
   page on that group.
6. Repeat for three distinct known groups (normally 23, 24, and 25).
7. Review the candidate and explicitly confirm the group labels before activation.

The listener joins only the configured multicast address, port, and wired interface. It ignores
control and unrelated datagrams, accepts only structurally valid Poly transmit packets using the
configured codec, discards encoded audio immediately, and stores only each 26-byte transmit
header plus SHA-256 evidence.

Activation is rejected when:

- fewer than eight valid transmit packets remain for any group;
- fewer than three distinct known groups were captured;
- the capture's codec, multicast address, port, or interface differs from the active contract;
- packet sizes, opcodes, caller-ID framing, flags, codec type, or channel range are invalid;
- the three captures do not prove one consistent channel bias;
- configuration changed after the review page loaded.

Changing the Poly destination codec after capture makes the endpoint unhealthy until captures
are cleared and repeated. PCMA is valid for regular RTP but is not offered for Poly Group Page;
the published Poly format supports PCMU (`0x00`) and G.722 (`0x09`).

## Transmission behavior after verification

For every page, the service sends:

1. 31 identical alert packets at approximately 30 ms intervals;
2. 20 ms audio frames, with the previous frame repeated before the current frame after the
   first packet;
3. a 50 ms end delay;
4. 12 identical end packets at approximately 30 ms intervals.

The 32-bit sender identity is the stream's random non-zero identifier, and **Setup → Safety &
settings → Poly caller ID** controls the fixed 1–13 ASCII-byte display name. Safety gates,
maximum duration, kill switch, and required-destination health remain enforced.

## Primary references

- Poly Engineering Advisory 70568, *UC Software PTT/Group Paging Audio Packet Format*:
  <https://kaas.hpcloud.hp.com/pdf-public/pdf_9124264_en-US-1.pdf>
- Algo multicast guide: <https://docs.algosolutions.com/docs/multicast-guide>
