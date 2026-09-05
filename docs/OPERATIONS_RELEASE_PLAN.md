# Front-office improvements

The four PRs are sequential and should be merged in order. They preserve the existing
transmitter, calibration, codec, timezone, and fire-time guards.

1. Reliability: durable manual action identity, configuration/audio binding, complete live
   state, explicit stale/session state, school timezone, accurate pause wording.
2. Daily operations: compact navigation, mobile-first Today, calendar editing, sound preview,
   and reachable schedule review/save controls.
3. Receiver acceptance: immutable evidence tied to receiver configuration, call-policy and
   emergency checks, and pinned authenticated receiver inspection.
4. Resilience: independent monitoring, retryable notifications, recovery ownership and
   school-year readiness.

## Reliability contract

Manual confirmations expire after two minutes. An action is claimed persistently before
execution. A retry returns its previous result; an interrupted claim is never automatically
replayed. An operator must inspect History/receivers before deliberately creating a new action.
Configuration or sound changes invalidate a pending confirmation. Distinct intentional actions
have distinct scheduler keys. All safety checks still run.

The dashboard requests live state every two seconds, does not overlap requests, and aborts a
request after four seconds. At six seconds without a successful snapshot it marks data stale.
Browsers suspended by the OS cannot guarantee this cadence; returning to the tab requests a
fresh snapshot. Stale state is not evidence that audio stopped, so Stop remains available for
last-known active audio. Configuration edits from another device require reloading controls.

Pause blocks scheduled and manual transmissions, including emergency-priority transmissions,
and requests cancellation of current audio. The UI now says so. An hours override only changes
the allowed-hours check; it does not bypass pause or other guards or set emergency priority.

## Verification

Install `.[dev,browser]`, then `python -m playwright install chromium`. Run `pytest` and
`pytest browser_tests`. The browser suite intercepts every request, serves real templates and
JavaScript, and rejects POST/network traffic; it cannot contact production or transmit audio.
Playwright is test-only and is not a Raspberry Pi runtime dependency.

Hardware/provider acceptance and school leadership's emergency policy require witnessed site
work. Code and simulated tests must never be reported as evidence of audible site coverage.
