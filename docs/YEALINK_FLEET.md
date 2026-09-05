# Yealink fleet inspection

For calls placed on hold by bells, follow [the call-interruption runbook](CALL_INTERRUPTION.md).
It includes a provider provisioning override and `scripts/check_yealink_barge.py` for auditing
a local text CFG export. Web inspection and file audits require witnessed call tests before
they can establish that the deployed receiver policy works.

`scripts/yealink_fleet.py` is a standalone, read-only diagnostic for Yealink web
configuration. It implements the phone's encrypted web login so an operator can inspect phones
from a Raspberry Pi on the voice network without changing their settings.

The inspector:

- pins the phone's SHA-256 certificate fingerprint before authentication;
- submits exactly one login request and does not retry rejected credentials;
- reads the multicast configuration page and its same-phone JavaScript assets;
- redacts passwords, session identifiers, and anti-CSRF tokens from its report; and
- has no configuration-write or reboot implementation.

Run it as the normal service user, not as root:

```console
python3 scripts/yealink_fleet.py \
  --host 192.168.10.234 \
  --fingerprint DC:CC:4A:86:18:E3:AB:3D:60:38:C8:8B:1B:C2:04:9D:C9:21:05:A6:16:6C:9C:F9:FB:AC:D5:D5:94:25:85:98 \
  --username pcadmin \
  --output /tmp/yealink-234-readonly.json
```

The password is requested without echo and is not accepted as a command-line argument. The JSON
report is created with mode `0600`. Treat a fingerprint mismatch as a hard stop until the phone's
identity has been verified manually.

This tool is intentionally inspection-only. Any future configuration writer must be a separate,
explicit operation with before-state capture, dry-run output, a one-phone pilot, post-save
verification, and rollback.
