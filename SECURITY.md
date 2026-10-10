# Security policy

## Supported versions

Only the latest release gets fixes. Pin to a specific tag or commit if you need a fix to land on your own schedule.

## Reporting a vulnerability

`check` reads the names and contents of the log files in the directory you point it at and, where you configure an artefact, the file that job produces. It prints a report and sets an exit code. It opens no network connection and sends no notification of its own. A vulnerability here still matters to anyone running it unattended, because that exit code decides whether a dead job gets noticed.

- Use GitHub's [private security advisories](https://github.com/jimy-r/dead-mans-switch/security/advisories/new), not a public Issue.
- Include the version affected and a minimal repro if you have one.

## Out of scope

- A job that should have been flagged stale but wasn't, or the reverse: that's a detection-logic bug, not a vulnerability. File it as a regular Issue.
- Whatever you wire to the exit code, such as a cron mailer, a CI notification or a webhook. This tool has no notification channel of its own, so report a vulnerability there to that provider.

## Maintainer response

Private security advisories get a first response within a week. If you don't hear back in two weeks, open a new private advisory as a ping.

---

*Last verified against the repo structure on **2026-10-10**.*
