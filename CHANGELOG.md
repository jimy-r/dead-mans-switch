# Changelog

Notable changes to `dead-mans-switch`, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- An artefact record a run writes partway through no longer hides that same run's `FAILED` or `HUNG` result. The artefact is now compared with when the logged run ended rather than with the start stamp in the log's filename, and it overrides the log only once it is more than ten minutes newer than that end. A record stamped more than five minutes in the future is ignored as clock skew (#11).

### Changed

- An unknown key at the top level or inside a task is now a config error. `check` exits 2 and names the key, the closest allowed key and the full allowed set. Before, a misspelling such as `artifact` for `artefact` was dropped without a word and the setting it meant to configure stayed off. **This can break a config that carries a typo or an extra key**, so run `check` once after upgrading.
- The licence is declared as the SPDX string `MIT` with `license-files`, and building needs setuptools 77.0.3 or newer.
- CI tests on Python 3.14 as well as 3.10 and 3.13, and the classifiers list 3.14.
- Every workflow action is pinned to a full commit SHA, and pull requests run a redaction check (#11, #12).

### Removed

- The README no longer offers a `pip install dead-mans-switch` line. Nothing is published to PyPI under that name, so install from source or copy `deadmans.py` (#12).

## [0.2.0] - 2026-09-13

### Added

- `artefact`, an optional per-task freshness signal keyed to what the job produces (a file's modification time, or the newest matching record in a JSONL file), so a job run outside its usual launcher is no longer reported as dead.
- `start_sentinel` and `max_runtime_hours`. A run that starts and never finishes reports `HUNG`, or `RUNNING` while it is inside its allowance, instead of `NO_SENTINEL`.
- `timezone`, a top-level key naming the clock that stamps the log filenames. It takes `local`, `UTC`, a fixed offset or an IANA zone name.
- A `pyproject.toml` and a `dead-mans-switch` console entry point, so `pip install .` works from a clone. Nothing is published to PyPI.
- `CONTRIBUTING.md`, `AGENTS.md` and `SECURITY.md`.

### Changed

- A sentinel has to open a line (after optional whitespace, backticks, asterisks or underscores) instead of appearing anywhere in the log, so a log that only quotes the string no longer passes. The same applies to `failure_sentinel`.
- Log timestamps and the clock are both timezone-aware, so a producer and a checker on different clocks no longer skew every age.
- Ruff runs at a pinned version under an explicit rule selection.
- The publish workflow runs only by hand. Its build job holds no git credentials, and the publish job checks the artifact digest against the one the build job produced.

## [0.1.0] - 2026-08-27

### Added

- First release. Config-driven sentinel freshness checking for scheduled jobs, with a staleness window per task, optional failure sentinels, manual tasks that pass until their first run, and exit codes made for cron and CI. One stdlib-only file.

[Unreleased]: https://github.com/jimy-r/dead-mans-switch/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jimy-r/dead-mans-switch/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jimy-r/dead-mans-switch/releases/tag/v0.1.0
