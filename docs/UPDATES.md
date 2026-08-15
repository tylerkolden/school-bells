# Secure web OTA updates

## First installation

On a fresh 64-bit Raspberry Pi OS host, use the one-paste installer from the latest immutable
production release:

```bash
sudo apt-get update && sudo apt-get install --yes curl && curl -fL https://github.com/tylerkolden/school-bells/releases/latest/download/install-school-bells.sh -o /tmp/install-school-bells.sh && sudo bash /tmp/install-school-bells.sh
```

This is the only terminal bootstrap. It requires `aarch64`, installs the OS prerequisites, accepts
only a stable immutable release published by the approved GitHub Actions identity, checks the exact
asset name, size, publisher, redirect host, and SHA-256 digest, safely extracts regular files only,
initializes a new configuration with the Pi's active IPv4 address, and passes the release provenance
fields to the root installer. Set `BELL_INTERFACE_IP` on the bootstrap command when the Pi has more
than one interface and automatic route selection would not choose the phone VLAN. Existing site
configuration is never replaced. All later production updates use the authenticated web console.

The operator computer does not need internet access. It opens the console over the school LAN;
the Raspberry Pi makes outbound HTTPS requests to GitHub when an administrator clicks **Check for
updates** or confirms an install.

## Trust model

Web OTA intentionally trusts only one source: stable production releases in
`tylerkolden/school-bells`. The repository name, GitHub API host, asset naming convention, maximum
size, expected workflow publisher, and stable semantic-version format are compiled into the
root-owned updater. Browser input cannot change them.

An accepted release must be all of the following:

- a newer `vMAJOR.MINOR.PATCH` release—not a branch, commit selected in the UI, prerelease,
  reinstall, or downgrade;
- published by `github-actions[bot]` using the production release workflow;
- immutable in GitHub, so its tag and assets cannot be moved, replaced, or deleted in place;
- accompanied by exactly one correctly named appliance archive with a GitHub SHA-256 digest;
- internally consistent: its release manifest must name the same tag and a full commit SHA;
- a safe regular-file archive within fixed compressed, expanded-size, and member-count limits.

The release workflow runs lint, tests with the coverage gate, dependency audit, and package build.
It then includes ARM64 wheelhouses for Python 3.11, 3.12, and 3.13, so installation does not execute
downloads from PyPI, generates GitHub build-provenance attestation, and publishes the archive. Third-party GitHub
Actions are pinned to full commits.

The Pi verifies the immutable release metadata, exact publisher, declared size, and SHA-256 digest
over HTTPS. It does not presently perform local Sigstore verification of the GitHub attestation;
the attestation remains useful for an independent audit. For a higher-assurance environment,
require offline organizational signing or TUF/Uptane-style threshold metadata before extending
this mechanism beyond the fixed public project.

## GitHub setup required once

The repository is currently private. Make it public only after confirming that its entire history
contains no credentials, real phone-system secrets, private certificates, student information, or
licensed audio. Site configuration, credentials, production recordings, SQLite state, and logs stay
under `/opt/bell/shared` on the Pi and are never included in an update.

Before publishing the first tag:

1. Enable GitHub **immutable releases** for the repository. The appliance refuses mutable releases.
2. Protect `main`: require a pull request, passing CI, review, resolved conversations, and no force
   pushes or deletion.
3. Create a GitHub Actions environment named `production`. Add an independent required reviewer and
   prevent self-review if the account/plan supports it. The tag workflow pauses there before it can
   publish code that will run as root on appliances.
4. Protect production tags matching `v*` if repository rules support tag protection.
5. Enable secret scanning, push protection, Dependabot alerts, and two-factor authentication.

Open source improves auditability, but public visibility is not the security boundary. Repository
administration, branch/environment protection, release immutability, and account security protect
the update channel.

## Publishing a release

Update the version in `pyproject.toml` and `bell/__init__.py`, merge through the protected branch,
then create and push the matching tag:

```bash
git switch main
git pull --ff-only
git tag v0.2.0
git push origin v0.2.0
```

Approve the `production` environment deployment after reviewing the commit and successful gates.
Do not manually create or upload the release; the appliance requires the Actions publisher.

## Operator flow

1. Choose a quiet period more than 15 minutes before the next bell.
2. Open **Updates** and click **Check for updates**.
3. Review the version, release notes, immutable status, and final confirmation.
4. Click **Install production update** once. The page may reconnect while the service restarts.
5. Confirm the page reports success and **System status** is Ready.

The updater refuses to begin unless the current appliance is Ready, while a page is active, or within
15 minutes of the next scheduled event. It stages a new version under `/opt/bell/releases`, preflights that exact environment, switches
`/opt/bell/current` atomically, restarts, and waits for `/ready`. A failed readiness check restores
the previous release and service unit automatically. Configuration/state backups remain under
`/var/backups/bell-system`.

The update is intentionally administrator-initiated, never unattended. Surprise restarts and
unreviewed supply-chain changes are unacceptable for a school paging system.

## Network policy

Allow the Pi outbound TCP 443 and DNS only as needed. GitHub currently serves metadata and release
assets from `api.github.com`, `github.com`, `objects.githubusercontent.com`, and
`release-assets.githubusercontent.com`; the updater rejects redirects elsewhere. Also retain the
separate NTP access required for correct bell time. Do not allow inbound internet access to port
8080—the console remains LAN/VPN only.

If the Pi has no outbound internet either, web OTA cannot work. Use an offline, signed update bundle
carried on removable media and verify it locally before installation.

## Diagnostics

```bash
systemctl status bell-update.path bell-update.service
journalctl -u bell-update.service --since today
cat /opt/bell/state/update/status.json | python3 -m json.tool
readlink -f /opt/bell/current
```

A broken health service causes web OTA to fail closed. Recover locally using the console/SSH and a
previous directory under `/opt/bell/releases`; do not weaken the web updater to bypass readiness.
