# SIP school-bell product research and feature selection

Research was performed against current vendor documentation and IETF standards on 2026-08-15.
The aim was not to copy a product UI; it was to identify the capabilities that recur across mature
school paging systems and make the first real deployment easier to validate and support.

## Product findings

- [Algo 8301 documentation](https://docs.algosolutions.com/docs/8301-user-guide) combines NTP
  calendaring, SIP registration/paging, regular RTP and Poly Group Page multicast, priority zones,
  up to 50 zones, DTMF selection, stored audio, and scheduled REST actions. It also documents G.711,
  G.722, and Opus usage in different modes.
- [BellCommander](https://www.acrovista.com/bellcommander/) emphasizes day/calendar exceptions,
  multi-zone device groups, SIP and multicast output, live and prerecorded emergency paging,
  relay integrations, remote web management, weather triggers, and automated backups. Its
  [SIP data sheet](https://www.acrovista.com/bellcommander/bcsipdatasheet.pdf) specifically calls
  out emergency looping and priority over routine pages.
- [Valcom's K-12 platform](https://www.valcom.com/who-we-help/k12/) combines centralized bell
  schedules, zone control, emergency-action automation, device monitoring, audio/visual endpoints,
  and multiple activation methods. Its [ezIP platform](https://www.valcom.com/product-portfolio/ez-ip/)
  includes secure SIP, timed tones, multicast, dry contacts, analog bridging, and override audio.
- [Advanced Network Devices](https://advancednetworkdevices.com/explore-products) combines bell
  scheduling, paging, intercom, graphics, strobes, buttons, PoE endpoints, and automated/audio-visual
  notification in ClockWise Campus.

## The five implemented feature tracks

1. **Advanced schedules and sequences.** Existing day calendars, snow days, weekday defaults, DST,
   and standing items now also support pre-announce tones, bounded repeats, repeat intervals,
   per-event priority, and explicit busy policy. Configuration writes are atomic, validated, backed
   up, and automatically rolled back on validation failure.
2. **Multi-protocol delivery.** One zone can target synchronized regular RTP, fail-closed Poly Group
   Page, SIP paging over UDP/TCP/TLS with SDP and PCMU RTP, and signed HTTP(S) webhooks for remote
   gateways, strobes, or displays. Required and optional endpoints have distinct failure semantics.
3. **Emergency priority and concurrency control.** Only one page owns the audio path at a time.
   Routine work may skip or queue; emergency-priority work cooperatively cancels and preempts lower
   priority audio. Repeat counts remain bounded by safety configuration.
4. **Authenticated automation.** A JSON API provides health, today's plan, and immediate triggers.
   It uses separate normal/emergency keys, strict emergency scope, rate limiting, persistent
   idempotency, fire-time safety checks, HMAC-signed outbound webhooks, audit logging, and optional TLS.
5. **Endpoint health and supportability.** Background protocol probes, per-endpoint last-success data,
   consecutive failure tracking, an optional-endpoint circuit breaker, degraded readiness for required
   failures, destination-isolated network errors, structured delivery results, and acceptance checks
   expose failures before a scheduled bell depends on them.

## Standards baseline

- [RFC 3261](https://www.rfc-editor.org/info/rfc3261/) defines SIP transactions, retransmission,
  matching, timeouts, dialogs, and transports.
- [RFC 3263](https://www.rfc-editor.org/info/rfc3263/) defines SIP server discovery and failover.
  This implementation supports explicit proxy hosts plus all A-record addresses returned by the OS;
  it does not claim NAPTR/SRV discovery.
- [RFC 8760](https://www.rfc-editor.org/rfc/rfc8760.html) updates SIP Digest with SHA-256 and
  SHA-512/256. Both are supported; MD5 remains available only for legacy PBX interoperability.
- [RFC 3550](https://www.rfc-editor.org/info/rfc3550/) defines RTP sequence numbers, timestamps,
  SSRCs, and RTCP. Paging media uses random stream state, 20 ms cumulative pacing, and PCMU PT 0.

## Deliberate limits

This is a one-way scheduled paging controller, not a general-purpose PBX, E911 system, fire-alarm
control panel, or certified life-safety system. SIP supports outbound paging and OPTIONS monitoring,
not inbound phone registration or two-way intercom. The proprietary Poly extension remains blocked
until the school's real packet capture is available. No protocol adapter may weaken that guard.
