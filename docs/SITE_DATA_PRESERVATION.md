# Site-data preservation plan and adversarial review

## Contract

An application upgrade must not replace site schedules, safety settings, calibration, credentials,
branding, custom uploads or existing state with repository defaults. Unknown schemas, unsupported
storage layouts, unreadable files, insufficient space and unverifiable copies fail closed. No
software can guarantee recovery from loss of the only storage device: a verified off-device copy
and a witnessed restore remain part of production readiness.

## Implementation order

1. Portable recovery: include every regular upload, not a WAV extension filter; version/checksum
   manifests; validate archives in an empty sandbox; retain legacy WAV-only backup compatibility.
   Restore data into the current installation's storage paths, never archive-supplied paths.
2. Upgrade checkpoint: quiesce the service, verify a complete snapshot of config/sounds/state,
   including credentials and consistent SQLite snapshots, and preserve it outside release code.
   Inventory unexpected layouts and reject rather than silently omit custom storage.
3. Installer transaction: lock concurrent installers; build dependencies before downtime; recheck
   the quiet window immediately before stopping; checkpoint before activation; compare config and
   uploads after staged validation and health checks. Restore prior data/code/service files on failure.
   A durable transaction record must block unattended restart after an interrupted activation until
   rollback has completed. Do not add automatic resend behavior for the maintenance interval.
4. Release gates: regression and adversarial tests must run on each supported Python version; Linux
   installer harness must test the actual shell control flow without touching a real system service.

## Review of the plan

A file-extension expansion alone does not protect custom directories, embedded storage paths,
partial archives, concurrent edits, changed SQLite schemas, or crashes after code activation.
Portable archives intentionally exclude authentication secrets and execution claims; deployment
checkpoints must include them. The two backup profiles cannot be advertised as interchangeable.
Unsupported portable configurations must fail explicitly rather than produce an incomplete backup.
Automatic schema migration is outside the code-only upgrade contract: future destructive migrations
need a new reviewed contract and restoration tests. The updater must not call successful rollback
just because a symlink changed back; it must restore the corresponding data and pass readiness.

## Attacks / acceptance

Mixed-case MP3, WAV, FLAC and arbitrary upload names; nested uploads; byte equality after restore;
custom calendar and safety values; absolute/relative custom storage paths; missing referenced audio;
legacy archives; path traversal, backslashes, duplicate/case-colliding names, symlinks and hardlinks;
checksum tampering and size limits; failure during copy/reload/health check; service-written changes;
interrupted activation; previous code and data restoration; cold-boot maintenance marker; two
concurrent installers. Synthetic tests never transmit audio or modify the deployed Pi.

## Implemented behavior and review findings

- Portable schema 2 includes every regular file beneath sounds, including nested uploads and
  mixed-case extensions, with an exact SHA-256 file inventory. It is extracted and validated
  before publication or pruning. Schema 1 remains readable and never deletes non-WAV uploads
  that an older backup could not contain. Archive size/member limits fail explicitly.
- Validation substitutes sandbox storage roots in memory. Applying a restore retains the current
  appliance's sounds/state/log locations. Public CA certificates inside config can accompany a
  portable archive; external CA dependencies require a deployment checkpoint/reviewed migration.
  Portable backups exclude bell.env and the account store, not every possible secret in arbitrary
  configuration or uploads. Protect them accordingly.
- A restore checksums incoming data before touching the site and saves a fresh pre-restore archive.
  A `.restore-incomplete` marker in the configured state directory blocks transmission and startup
  after interruption. In-process rollback removes it only after the old configuration reloads.
  A power-interrupted restore requires an administrator to recover from the archive recorded in
  that marker before clearing it; do not simply delete the marker to resume bells.
- The installer uses an exclusive lock and stages dependencies before quiescing the current service.
  The quiet window is rechecked at that boundary. Config, uploads and state are checkpointed together;
  SQLite's backup API includes committed WAL contents. Checkpoints contain credentials and retain
  file modes/ownership. They remain root-only under `/var/lib/bell-updater/transactions` and are not
  automatically pruned. Copy completed checkpoints off-device under the school's security policy.
- Config/sound hashes must remain identical through validation and candidate health checks. The
  candidate blocks all transmissions (including manual overrides) and web mutations while the
  root-owned `/opt/bell/.upgrade-incomplete` marker exists. A persistent systemd ExecCondition prevents
  boot activation after a crash; its own runtime token permits the maintenance health probe without
  resetting any administrator-installed systemd conditions. Existing SQLite rows/columns and other
  state files must remain intact; additive tables/columns/rows are permitted. Alert dispatch waits
  until this verification completes.
- Failure restores the checkpoint, former code symlink and root-managed service/updater files,
  then checks readiness. Rollback failure keeps the guard and checkpoint for intervention. Existing
  event claims and the scheduler's 60-second misfire policy remain in force; no explicit replay is
  added. Use the enforced quiet window, since startup of an older rollback release can still honor
  a recent unclaimed scheduled event. Supported code-only upgrades use a versioned `current` symlink and distinct
  config/sounds/state roots within `/opt/bell`. External roots, overlapping roots, legacy layouts,
  links/special files or incompatible schemas require an explicit migration; no defaults replace them.

## Recover an interrupted upgrade

The marker names the retained transaction directory. On the Pi, as an administrator, inspect it
and use the **saved trusted recovery tool**, not a new repository checkout:

```sh
sudo cat /opt/bell/.upgrade-incomplete
sudo python3 /var/lib/bell-updater/transactions/TRANSACTION/upgrade_transaction.py recover --transaction /var/lib/bell-updater/transactions/TRANSACTION
```

Replace TRANSACTION with the recorded directory name. This restores a prepared transaction;
a committed transaction merely finishes cleanup. `--rollback-committed` explicitly restores a
committed release's checkpoint too and therefore discards edits made since that checkpoint;
use it only as part of immediate failed-upgrade recovery, never as casual version switching.
Do not modify checkpoint manifests or clear guard files to force startup. If checksums or
readiness fail, retain the evidence and recover from a verified off-device copy.

## Verification boundaries

The Linux harness runs the real installer shell against isolated paths. It simulates OS identity,
package installation, systemd and health responses; it does not test a real Pi, power supply or
network. Separate tests execute the actual archive/checkpoint/rollback functions, integrity
checks and maintenance enforcement. Passing these gates is required for release preparation;
a witnessed spare-Pi restore and the site's actual schedule/audio acceptance remain deployment
checks. There is no promise against destruction of the only disk or malicious root access.
