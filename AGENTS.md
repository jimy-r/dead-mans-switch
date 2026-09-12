# AGENTS.md

Instructions for any coding agent working in this repository, in the
[agents.md](https://agents.md/) format.

This repo is a freshness checker for scheduled jobs: it watches for the absence
of success rather than for errors. The whole checker is one stdlib-only file,
`deadmans.py`, and it must stay that way.

Run the tests before you commit: `python -m pytest test_deadmans.py` (CI runs
the same suite as `python -m unittest -v`), then `ruff check .` and
`ruff format --check .`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor rules.
