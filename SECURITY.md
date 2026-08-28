# Security policy

## Supported versions

Only the latest release gets fixes. Pin to a specific tag or commit if you need a fix to land on your own schedule.

## Reporting a vulnerability

This tool runs against your scheduled-job state and, depending on how you wire it, can read job outputs and send notifications. A vulnerability here is a real concern for anyone running it unattended.

- Use GitHub's [private security advisories](https://github.com/jimy-r/dead-mans-switch/security/advisories/new) — not a public Issue.
- Include the version affected and a minimal repro if you have one.

## Out of scope

- A job that should have been flagged stale but wasn't, or the reverse: that's a detection-logic bug, not a vulnerability. File it as a regular Issue.
- Vulnerabilities in the notification channels you've configured (email, webhook targets, etc.): report upstream to that provider.

## Maintainer response

Private security advisories get a first response within a week. If you don't hear back in two weeks, open a new private advisory as a ping.

---

*Last verified against the repo structure on **2026-08-28**.*
