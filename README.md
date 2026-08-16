# School Bell System

This project runs a scheduled, fail-safe multicast paging transmitter for a Catholic K–12
school. A Raspberry Pi sends configured regular RTP or verified Poly Group Page audio to Yealink
T31P phones and an Algo 8186 horn.
It schedules in local wall-clock time, records every fire attempt in SQLite, refuses late or
duplicate bells, and exposes a simple front-office UI.

> **Deployment gate:** Poly Group Page is intentionally uncalibrated until the site's packet
> contract is verified. The system will not transmit without live evidence. Complete
> the guided **Setup → Run guided Poly capture** workflow on the Raspberry Pi before live use.
> See [the capture procedure](docs/CAPTURE.md) for preparation and the manual fallback.

## Zones

Multicast destinations default to `239.255.255.255:601`; administrators may change the multicast
IPv4 address and UDP port in **Setup → Destinations** to match receiver provisioning. The Poly
channel is the zone selector.

| Channel | Zone | Desk phones | Algo horn | Typical use |
|---:|---|:---:|:---:|---|
| 23 | Indoors | yes | no | class bells and prayer |
| 24 | Outdoors | yes | yes | recess and dismissal |
| 25 | Everywhere | yes | yes | emergency and all-call |

Regular RTP supports PCMU, PCMA (G.711 A-law), and G.722 wideband. Poly Group Page supports the
published PCMU (`0x00`) and G.722 (`0x09`) codec types and wraps 20 ms/160-byte frames with Poly
session headers and previous-frame redundancy. SIP uses an ordered preference list; every
multicast destination uses exactly one codec selected to match its receivers. The school's live
Yealink capture uses G.722. G.722 is encoded at 16 kHz but uses an 8 kHz sample-count clock.

## Architecture

- `bell/wire/` builds packets. `PolyGroupPage` fails closed until a three-channel live capture is
  derived, confirmed, persisted, and revalidated against its header-only evidence.
- `bell/audio.py` prepares, probes, caches, and transcodes audio with ffmpeg.
- `bell/transmit.py` sends synchronized destination streams with cumulative pacing correction.
- `bell/protocols/` provides SIP UDP/TCP/TLS paging and signed HTTP(S) delivery adapters.
- `bell/delivery.py` fans one event out across required and optional protocol endpoints.
- `bell/paging.py` serializes routine pages and cooperatively preempts them for emergencies.
- `bell/monitor.py` probes endpoints, records health, and opens optional-endpoint circuit breakers.
- `bell/config.py` loads and cross-validates the YAML configuration.
- `bell/scheduler.py` resolves only the current day and provides at-most-once SQLite state.
- `bell/safety.py` rechecks all safety rules immediately before every fire.
- `bell/service.py` runs the scheduler and localhost health endpoints.
- `bell/web/` is a password-protected, server-rendered front-office UI.
- `deploy/ota_updater.py` installs immutable production releases through a privilege-separated,
  web-triggered systemd job with health-checked rollback.
- `bell/probe.py` and `bell/listen.py` are field diagnostic tools.

## Network and hardware prerequisites

Use a Raspberry Pi 4 or 5 with wired Ethernet on the same VLAN as the receivers. Do not use
Wi-Fi: multicast may be dropped, rate-limited, or silently converted to unicast. Give the Pi a
DHCP reservation (the example uses `192.168.10.20`). The switched network should have IGMP
snooping **and an active IGMP querier**; snooping without a querier can make multicast stop
after membership entries age out.

Use a quality SD card or USB SSD, a small UPS, and preferably a DS3231 real-time clock. A Pi
has no battery-backed clock; after a power failure, NTP and the RTC protect wall-clock bells.
Do not use the local Docker profile in production. Host networking, multicast interface selection,
clock status, systemd readiness, and hardware RTC visibility should remain explicit and easy to
diagnose on the Raspberry Pi.

## Test locally on Windows with Docker

Install and start [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
using its Linux/WSL 2 engine. Then double-click `Start-Local-Test.cmd`, or run this from PowerShell
in the repository directory:

```powershell
docker compose up --build --detach --wait
```

Open <http://localhost:8080> and sign in with the default local-only password
`local-test-only`. Open <http://localhost:9000> in a second tab to see each simulated page arrive.
After a successful Ring Now test, the result banner also links directly to that receiver dashboard.
When testing outside configured bell hours, select the logged emergency-hours override as prompted.
The stack uses a dedicated bridge network, both ports bind only to Windows loopback, all Linux
capabilities are dropped, and the only configured delivery destination is the bundled HTTP receiver.
It cannot send multicast to phones or horns. Production OTA is also disabled; rebuild the image to
test newer code.

The named Docker volume preserves calendar edits, logs, cached audio, and test history between
starts. Useful commands:

```powershell
# Follow application and receiver logs.
docker compose logs --follow

# Stop containers but retain test data (or double-click Stop-Local-Test.cmd).
docker compose down

# Reset all local test configuration and history to repository defaults.
docker compose down --volumes
docker compose up --build --detach --wait
```

Override the test password before starting if desired:

```powershell
$env:BELL_UI_PASSWORD = "a-long-local-test-password"
docker compose up --build --detach --wait
```

This profile tests the real UI, scheduler, safety checks, configuration editing, FFmpeg input
validation, SQLite history, health monitoring, and HTTP delivery. It intentionally does not prove
Raspberry Pi systemd/OTA behavior, RTC/NTP integration, physical audio output, multicast routing,
or Poly Group Page compatibility. Complete the Pi acceptance procedure before live deployment.

## Install for development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -e '.[dev]'
pytest
ruff check .
python -m bell.config validate --config-dir config
```

Install `ffmpeg` first (`sudo apt install ffmpeg` on Raspberry Pi OS). The sample WAV files are
safe placeholders; replace them with approved school recordings, run `python -m bell.audio prep`,
and validate again. Run `python -m bell.service --check-only --config-dir config` on the target Pi.

## Configuration

Configuration is split into comment-friendly YAML files:

- `settings.yaml`: timezone, wired interface IP, paths, wire format, and safety window.
- `destinations.yaml`: multicast endpoint(s), TTL, and enable flags.
- `zones.yaml`: channel and destination mapping plus plain-language descriptions.
- `schedules.yaml`: named schedules and standing prayer items.
- `calendar.yaml`: weekday defaults, date overrides/ranges, and no-bell dates.

Example scheduled event:

```yaml
- time: "08:00"
  sound: class-bell.wav
  zone: indoors
  label: First bell
```

Administrators normally edit this from **Schedules** in the web console. The Schedule Builder can
create, duplicate, edit, preview, and safely delete schedules; each row exposes time, sound, zone,
repeat, priority, optional pre-tone, and audio-busy behavior. **Save & activate** validates the
complete configuration, writes an atomic backup, and reloads today's future jobs without replaying
past bells. The browser's sound preview stays local and never transmits to a paging zone.

The **Setup** workspace provides dependency-aware administration for standing items, weekday
defaults, date ranges, sounds, zones, delivery destinations, and safety settings. Create, update,
and delete actions use the same stale-edit detection and rollback pipeline. Referenced sounds,
zones, and destinations cannot be deleted until their dependents are changed. Multicast edits
validate an IPv4 multicast address and UDP port, and select exactly one receiver-compatible codec.
Uploaded audio is bounded,
validated by FFmpeg, normalized, and stored as 8 kHz mono WAV before it enters the library.

Example snow day:

```yaml
no_bell_dates:
  2027-01-15: Snow day
```

Times are always `America/Denver` wall-clock values. DST changes do not move the displayed or
scheduled bell time.

Events may also define a bounded pre-tone/repeat/priority policy:

```yaml
- time: "09:30"
  sound: lockdown-message.wav
  pre_tone: attention-chime.wav
  zone: everywhere
  label: Lockdown drill
  repeat_count: 3
  repeat_interval_seconds: 2
  priority: 100
  busy_policy: preempt
```

`max_repeats` in `settings.yaml` remains the final safety ceiling. Priority at or above
`emergency_priority_threshold` always uses cooperative preemption; it cannot be accidentally
configured to wait behind a routine page.

`clock_sync_required` defaults to `true`, so both startup validation and the readiness endpoint
fail closed when NTP synchronization cannot be confirmed. `max_audio_seconds` limits each source
recording and `max_page_seconds` limits the complete pre-tone/repeat sequence; these bounds are
checked before any receiver is contacted. Set a larger value deliberately for longer announcements.

## Delivery protocols

A zone may reference any mix of these destination types:

- `multicast`: Regular RTP or calibrated Poly Group Page, with per-destination TTL and isolated
  socket failures. Multicast destinations sharing a wire format are paced from one frame buffer.
- `sip`: outbound one-way paging using SIP over UDP, TCP, or certificate-verified TLS; SDP negotiates
  the configured PCMU/PCMA/G.722 preference. Digest supports SHA-256/SHA-512-256, qop and legacy
  no-qop/MD5 compatibility. Loose-route proxy dialog sets are honored. All OS-resolved A-record
  addresses are tried; configure an explicit proxy host/port because NAPTR/SRV discovery is not claimed.
- `http`: retrying JSON webhooks for another paging gateway, strobe, display, or automation server.
  Requests carry an idempotency key and optional HMAC-SHA256 signature.

Secrets are referenced by environment-variable name in YAML and stored in `config/bell.env`, never
inline in `destinations.yaml`. Disabled SIP and HTTP examples are included in the sample configuration.
Required endpoint failure fails the page visibly; an optional endpoint is isolated and enters a short
circuit-breaker cooldown after repeated failures.

Source recordings may use any format decoded by the installed FFmpeg build; WAV, MP3, AAC/M4A,
FLAC, Ogg Vorbis, and Opus-in-Ogg are common choices. They are converted and cached before paging.
SRTP/DTLS-SRTP and proprietary vendor payloads are rejected rather than silently downgraded; use a
trusted voice VLAN or a standards-compliant paging gateway when encrypted media is required.

## Automation API

The office service exposes JSON endpoints on the same port as the UI:

- `GET /api/v1/health`
- `GET /api/v1/today`
- `POST /api/v1/trigger`

The service-only `GET /ready` endpoint returns HTTP 503 until the clock, scheduler, monitor, and
every required destination are ready. This makes it suitable for systemd and external readiness
watchdogs. Docker's image health check uses liveness (`GET /health`) so an intentionally degraded
test configuration remains inspectable in the UI. The JSON response names each failing readiness
condition.
`GET /metrics` on the same localhost-only port exposes a small Prometheus text endpoint for readiness,
uptime, and per-destination health without adding a metrics framework dependency.

Set separate `BELL_API_KEY` and `BELL_EMERGENCY_API_KEY` values. Every request uses
`X-Bell-API-Key`; triggers also require a stable `Idempotency-Key`. Only the emergency key can use an
emergency priority or override allowed hours. Requests are rate-limited and idempotency survives a
restart in SQLite.

```bash
curl -X POST https://bell.example.edu:8080/api/v1/trigger \
  -H 'Content-Type: application/json' \
  -H 'X-Bell-API-Key: ...' \
  -H 'Idempotency-Key: drill-2026-08-15-a' \
  -d '{"sound":"class-bell.wav","zone":"indoors","label":"Office test"}'
```

Use a trusted certificate via `BELL_TLS_CERTFILE` and `BELL_TLS_KEYFILE` whenever API/password traffic
can cross anything beyond a physically trusted management LAN. Security headers, strict session
cookies under TLS, per-session CSRF protection, sign-in throttling, no-store responses, scoped keys,
and HMAC outbound signatures are built in. Calendar and schedule saves use a configuration
fingerprint to reject stale edits, retain rolling backups, and atomically replace YAML only after
validation. A failed live reload restores the previous file and runtime configuration.

## Phones and Algo

Configure the T31P receivers for Poly/Yealink group paging channels 23, 24, and 25 at the
shared multicast endpoint. Configure the Algo 8186 at `192.168.10.32` as **Poly Group Page**,
Receiver mode, subscribed to Groups 1, 24, and 25. Regular RTP mode is not compatible.

Provision these settings through the authoritative phone configuration when possible. Hosted
phone providers commonly reprovision overnight and wipe changes made in the local web UI.
After any reprovisioning, run the acceptance test and verify paging subscriptions again.

## Production

Review [operations](docs/OPERATIONS.md), deploy to a fresh 64-bit Raspberry Pi OS host with one
paste, and then complete Poly calibration and hardware acceptance before enabling schedules:

```bash
sudo apt-get update && sudo apt-get install --yes curl && curl -fL https://github.com/tylerkolden/school-bells/releases/latest/download/install-school-bells.sh -o /tmp/install-school-bells.sh && sudo bash /tmp/install-school-bells.sh
```

The public release needs no GitHub account or token. The downloaded bootstrap installs OS
prerequisites, verifies the latest immutable production package and its GitHub SHA-256 digest, and
then initializes a fresh configuration with the Pi's active IPv4 address and runs
`deploy/install.sh`. On a multi-NIC Pi, run the final command as
`sudo BELL_INTERFACE_IP=192.168.x.x bash /tmp/install-school-bells.sh` to select the phone VLAN.
Do not replace this with `curl | sudo bash`; keeping download and
execution separate makes failures visible and leaves a script that can be inspected first.

You may pre-create `/opt/bell/config/bell.env` from `bell.env.example` with
mode 0600. If it is absent, the installer creates strong UI/session secrets and prints the UI
password once; store it in the school's password manager.
The front-office UI binds on `0.0.0.0:8080`; firewall it to the trusted school LAN and never
publish it to the internet. The health service stays on `127.0.0.1:8000`.

Run the installer as root so it can install and enable the systemd units; it performs Python
package installation as the unprivileged `bell` account. The installer preserves local configuration
and sounds under `/opt/bell/shared`, creates a 90-day deployment backup, stages a versioned release,
validates the installed code/config/audio before an atomic switch, and only then activates the
hardened unit:

```bash
sudo bash deploy/install.sh
```

After the first install, a signed-in administrator can use **Updates** in the LAN console. The Pi—not
the viewing computer—checks GitHub and installs only a newer, immutable, workflow-published stable
release. It refuses active/near-bell maintenance and automatically switches back if readiness fails.
See [secure web OTA setup and threat model](docs/UPDATES.md) before publishing the first release.

If an RTC is installed and verified, set `rtc_required: true` so acceptance checks enforce it.

After install, run `python scripts/acceptance.py`. First live-test one channel-23 bell during a
lunch period. Do not load the full production schedule until that controlled test is observed.

The systemd unit uses readiness notification and a watchdog heartbeat. If the scheduler, endpoint
monitor, health server, or operator server dies, the process exits nonzero and systemd restarts it.
It runs with only `CAP_NET_BIND_SERVICE`, which is required to capture the default privileged UDP
port 601 during Poly calibration, plus a restricted filesystem/device/namespace view. It does not
receive raw-packet, network-administration, or general root capabilities.

The product comparison and standards rationale are documented in [docs/RESEARCH.md](docs/RESEARCH.md).
