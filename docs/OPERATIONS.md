# Bell system operations

## Snow day from a phone at 6 AM

1. Connect to the trusted school LAN/VPN and open `http://<pi-address>:8080`.
2. Sign in, tap **Calendar**, then **Today**.
3. Enter a clear no-bell reason such as “Snow day — district closure” and save.
4. Return to **Today** and confirm the no-bell banner and reason are visible.
5. Open **Status** and confirm configuration is valid. Do not turn on the kill switch for an
   ordinary snow day; the dated calendar entry is auditable and expires automatically.

## Fire drill / emergency all-call

Coordinate with school leadership before the drill. In **Ring Now**, choose the approved alert
sound. Select **Everywhere** only after reading its description: every desk phone and the outdoor
horn will sound. Review the second confirmation screen, then confirm once. The emergency styling
and separation are intentional safeguards. If outside normal hours, the override checkbox is
required and the override is logged at WARNING.

## Bells did not ring — triage in order

1. Check whether the kill switch is on.
2. Check Today/Calendar for a no-bell day or wrong schedule override.
3. Run `curl -s localhost:8000/health | python3 -m json.tool` and inspect readiness, next fire,
   last result, configuration hash, clock state, each endpoint's protocol health, and any required
   endpoint causing degraded readiness.
4. Run `journalctl -u bell-system --since '30 minutes ago'` and find blocked, missed, or failed
   fire records. A bell more than 60 seconds late is intentionally skipped.
5. From a wired VLAN host, run `python -m bell.listen --iface <ip> --seconds 10 --output test.wav`
   during a controlled transmission. This confirms packets and playable configured-codec audio on
   the wire.
6. Use `sudo tcpdump -ni eth0 udp port <configured-port>` to verify multicast leaves the Pi.
7. Confirm the T31P multicast subscriptions and Algo Receiver/Poly Group Page configuration.
8. Ask whether the hosted provider reprovisioned phones; provisioning can wipe local web-UI
   settings overnight.

For a SIP destination, use its health detail to distinguish DNS/connect timeout, TLS certificate,
Digest credential, SDP/codec, INVITE rejection, RTP, and BYE errors. A `401`/`407` OPTIONS result is
reachable rather than offline; page calls still require the configured credential environment variable.
For an HTTP destination, inspect the returned HTTP status and the remote system's idempotency/HMAC log.
Optional destinations enter circuit-breaker cooldown after repeated failures; required destinations
are always attempted and make readiness degraded when unhealthy.

Never repeatedly press manual fire while diagnosing. At-most-once state and the daily cap are
safety features, and repeated live tests disrupt the whole building.

## Phones ring but the horn does not

Confirm the event uses channel 24 or 25; channel 23 is intentionally phones-only. Verify the Algo
is in Poly Group Page Receiver mode and subscribed to the exact group. A channel or calibrated
Poly-extension mismatch can let phones behave differently from the horn. Re-run the golden capture
test and inspect the Algo Group 24 checkbox, especially after the capture procedure.

## Everything rings an hour off

Run `timedatectl status`: timezone must be `America/Denver` and NTP synchronized. Check the health
endpoint clock status. Verify `sudo hwclock -r` reports correct time; a dead/missing RTC can expose
a bad clock after power loss before NTP starts. The scheduler uses `zoneinfo` wall-clock times, so
do not manually offset YAML times for DST.

## Routine changes

After every configuration change, reboot, phone reprovisioning, or software upgrade, run:

```bash
cd /opt/bell/current
.venv/bin/python scripts/acceptance.py --config-dir /opt/bell/config
```

Keep the full production schedule disabled until Poly calibration passes and one observed channel-23
bell succeeds during a low-impact period.

Calendar and Schedule Builder changes made in the UI create atomic backups under
`state/config-backups/`; the newest 30 of each type are retained. If validation or live activation
fails, the previous file and runtime configuration are restored automatically. Restore manually only
with the service stopped, preserve ownership, validate, and then restart:

```bash
sudo systemctl stop bell-system
sudo -u bell cp state/config-backups/calendar-<timestamp>.yaml config/calendar.yaml
sudo -u bell .venv/bin/python -m bell.config validate --config-dir config
sudo systemctl start bell-system
```

Setup changes for zones, destinations, standing items, calendar rules, and safety settings use the
same validation/backup/rollback path. Sound replacements and deletions retain recovery copies under
`state/sound-backups/`. The UI blocks deletion of a sound, zone, destination, or schedule while
another configuration object references it; resolve the listed dependencies first.

Automation keys are credentials. Rotate normal and emergency keys independently, keep the emergency
key only in approved panic-button/integration systems, and never put either key in a URL or browser
bookmark. Reusing an `Idempotency-Key` intentionally returns the original result without ringing again.

## Monitoring and upgrades

Scrape `http://127.0.0.1:8000/metrics` through a local monitoring agent. Alert when `bell_ready` is
zero, a required `bell_endpoint_healthy` value is zero, the service restarts, or no fresh JSON log
records arrive. The HTTP endpoint is intentionally loopback-only; do not expose it directly.

Use **Updates** in the operator console for routine upgrades after completing the one-time setup in
`docs/UPDATES.md`. Check and install only in a quiet maintenance window. The root-owned updater
accepts only newer immutable production releases from the fixed repository, validates the staged
release, and waits for readiness after restart. If readiness fails, it atomically restores the
previous release. Each upgrade also archives configuration and state under
`/var/backups/bell-system`; archives are root-only and retained for 90 days.

`sudo bash deploy/install.sh` from a physically reviewed checkout remains the local recovery path,
not the routine update mechanism. Never expose the update UI to the internet or grant the `bell`
service account general sudo access.
