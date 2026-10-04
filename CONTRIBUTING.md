# Contributing

Issues and pull requests are welcome. This is maintained best-effort by one person, so a reply may take a week.

## The rule that is not negotiable

The checker stays one stdlib-only file. `deadmans.py` is meant to be copied into a scheduling environment on its own, with nothing to install and nothing to keep in sync, and that property is the reason the tool gets adopted at all. A pull request that adds a runtime dependency, splits the checker into a package, or requires an install step before `python deadmans.py check` works gets closed however good the rest of it is. Packaging metadata for the optional `pip install .` path is fine; a dependency it pulls in is not.

The second rule follows from the first. Tests stay offline. No network, temp directories only, no reaching into a real log directory.

## Setting up

Python 3.10 or newer. Nothing else.

```bash
git clone https://github.com/jimy-r/dead-mans-switch.git
cd dead-mans-switch
python -m pytest test_deadmans.py
```

The suite is plain `unittest`, so `python -m unittest -v` runs it too, and that is the form CI uses. Either works.

Try the tool against a throwaway state machine before changing it:

```bash
python deadmans.py selftest        # built-in state machine over a temp directory
python deadmans.py init            # write a starter config to edit
python deadmans.py check --json    # what a scheduled run reports
```

## Before you push

```bash
python -m pytest test_deadmans.py
pip install -r .github/requirements-ci.txt
ruff check .
ruff format --check .
```

CI runs the suite on Python 3.10, 3.13 and 3.14 and lints on 3.13. Lint pins live in `.github/requirements-ci.txt` and the explicit select lives in `ruff.toml`, so a ruff bump cannot silently change what is linted. Install the pinned version if a finding looks unfamiliar.

## Changing the checker

- Stdlib only. See the rule above.
- A new finding kind needs a case in `test_deadmans.py` for the finding and a case for the nearest state that must stay clean. Freshness logic that only fires on real elapsed time is untestable, so drive it through the injectable clock the suite already uses.
- Exit codes are the interface. 0 means every tracked task is fresh, 1 means at least one is not, 2 means the config or the invocation is wrong. A schedule or a CI step depends on that split, so changing it is a breaking change and belongs in its own pull request.
- Config keys are documented in [`README.md`](README.md) and shown in `deadmans.example.json`. A new key changes all three in the same commit.
- Times are timezone-aware. Never compare a naive datetime against a log timestamp.

## Scope

This repository is the checker. The pattern behind it, ["Make silent failure loud"](https://github.com/jimy-r/agent-workspace-architecture/blob/main/PATTERNS.md), lives in [agent-workspace-architecture](https://github.com/jimy-r/agent-workspace-architecture), which takes proposals about the pattern itself. How your own jobs write their sentinel lines is yours; the tool only reads them.

## Commits and pull requests

[Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`), one logical change per commit. Agent-assisted commits carry a `Co-Authored-By:` trailer.

A pull request description says what changed and why, and reports the test run. If the change alters what counts as a finding, say so in the first line so a reviewer looks there first.

## Security

Do not report a vulnerability in an issue or a pull request. [`SECURITY.md`](SECURITY.md) has the private path.
