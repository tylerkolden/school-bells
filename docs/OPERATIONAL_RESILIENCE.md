# Operational resilience

Merge the four operations PRs in order. These tools require explicit site configuration;
installing application code does not activate an independent monitor or create an off-device copy.
Record the responsible people, escalation route, backup location, receipt date, and witnessed
restore results in **More → Backup & recovery**. That page flags copies older than seven days
and restoration drills at the configured interval (90 days by default). Never enter credentials
in its descriptive fields. Failed drills remain due. Changes retain history and reject stale edits.

## Notification delivery

The service persists notifications before network I/O and retries pending work from its service
loop. Failed deliveries retry after 15, 30, 60 and 120 seconds, with at most five attempts.
Attempts are counted before network I/O, including an interrupted attempt. A 60-second
cross-process lease prevents simultaneous sends. Restart preserves both attempts
and pending work. Successfully delivered messages deduplicate for five minutes; pending messages
coalesce without erasing the original. Each payload and `X-Bell-Notification-ID` contains a stable
ID so a webhook receiver can deduplicate an ambiguous timeout after receipt. Delivery is at least
once; HTTP cannot guarantee that an accepted response was received by the sender.

Optional HMAC signing uses the configured environment variable, never a persisted secret.
Redirects are refused: configure the final webhook URL. Changing/disabling the URL cancels old
pending work on its next attempt. Fixing a missing signing secret allows the remaining retries.
**System status → Notification delivery** exposes pending/exhausted counts and the latest result.
Exhausted alerts require investigation and a new deliberate test; retries do not run forever.
A maximum of 500 pending items bounds storage, with an error logged if the queue fills. Terminal
history retains approximately the latest 500 records. Notification failure does not disable bells.

## Independent monitor (separate host)

Run this on a host with independent power and an independent route to the alert recipient. A
monitor on the Pi cannot detect its own power loss. Arrange supervision of the monitoring host
itself and escalation for a failed timer; one monitor cannot prove its own availability.

1. Install this project's wheel and dependencies in `/opt/bell-watch/.venv` on the monitoring host.
2. Copy `deploy/bell-watch.example.json` to `/etc/bell-watch.json`; replace the example URL,
   owner and escalation. Serve the console over validated HTTPS. Never expose the bare local
   health port publicly; TLS verification and an exact URL are required, with no redirects.
3. Set a distinct random `BELL_MONITOR_API_KEY` in the Pi's protected `bell.env` and restart the
   service. This credential authorizes only `GET /api/v1/health`, never page transmission.
4. In root-readable `/etc/bell-watch.env`, set `BELL_MONITOR_API_KEY` to that value,
   `BELL_WATCH_WEBHOOK_URL` to the final HTTPS alert URL and `BELL_WATCH_SECRET` to its shared
   signing secret. Configure the receiver to map `external_escalation` to the backup contact;
   the descriptive escalation text does not configure a second delivery route by itself.
5. Copy the watch service/timer examples to `/etc/systemd/system`, run `systemctl daemon-reload`,
   then `systemctl enable --now bell-watch.timer`. Verify a signed test reaches the owner.

The timer polls about every 30 seconds plus request duration. Three failed probes open an
incident; two fresh successful probes close it. Valid HTTP alone is insufficient: the response
must say `ready: true` and carry a timezone-qualified `observed_at` within 90 seconds (10 seconds
future tolerance). Synchronize both hosts' clocks. A missing runtime, malformed/stale response,
HTTP error, timeout, or TLS failure counts as a failed probe. Outages, unacknowledged reminders,
escalations and recoveries use a persistent notification outbox on the independent host.

Inspect or acknowledge on that host (as root for a DynamicUser state directory):

```sh
/opt/bell-watch/.venv/bin/python -m bell.watchdog --config /etc/bell-watch.json --state-dir /var/lib/bell-watch --status
/opt/bell-watch/.venv/bin/python -m bell.watchdog --config /etc/bell-watch.json --state-dir /var/lib/bell-watch --ack INCIDENT_ID --by 'Responder name' --note 'Checking power; next update in 10 minutes'
```

Acknowledgment silences reminders, not recovery checking or the time-bound escalation. Incidents
remain open until repeated healthy responses. Inspect JSON journal entries for probe and outbox
results. The local status command records the active and most recent closed incident. Test loss
of Pi power/network, monitor restarts, webhook outage, stale health and recovery in a maintenance
window before claiming coverage. Do not stop emergency communications to test these cases.

## Off-device backups and restoration

`python -m bell.continuity --config-dir /opt/bell/config --destination /mnt/bell-backups`
creates a portable archive, validates its contents, copies through a temporary file, flushes it,
and compares SHA-256 before publishing its final filename. A matching `.records.zip` sidecar
contains consistent SQLite snapshots of receiver evidence and continuity records. It retains ten
archives and their sidecars. A missing
mount fails instead of creating a misleading backup on the Pi. Configure the destination as an
actual mounted NAS/remote volume and validate its independent storage; `ismount` alone cannot
prove physical independence. File permissions on that server remain the owner's responsibility.

Use the provided backup service/timer only after configuring the mount and writable ownership.
The examples require the mount and run daily at 21:00 America/Denver, catching missed runs on boot.
Supervise timer failures through the site's independent monitoring service. Verify the destination
receipt before recording the copy date in Recovery. These examples are not enabled by the installer.

Portable backups preserve configuration, audio and branding; they intentionally do not export
credentials or execution claims. The scheduled off-device tool also copies receiver/continuity databases in a sidecar. Stop the
console before restoring those two named databases into the new state directory, then restart
and review their dates/configuration fingerprints. Never restore stale transmission claims. Keep credentials in the site's password manager. On a spare,
isolated appliance, restore the archive through Recovery, re-establish credentials, compare the
effective calendar and sound library, and verify guards. Only then perform witnessed receiver/call/
emergency tests in an approved window. Record the date, observer, outcome, and recovery time.
A download or checksum success is not a witnessed restoration.

## School-year readiness

**Calendar → Review school year** lists the effective schedule, source and exact bell times for
up to 371 days. Weekdays without events need a decision unless explicitly marked no bells.
Holidays, testing days and Mass days must be checked against the school's approved calendar;
they are never guessed. Bulk changes first show every date's before/after events (including
standing items and weekends). The final submission is bound to the signed ten-minute review
and exact configuration. Changed inputs or configuration require another review.
