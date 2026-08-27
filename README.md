# dead-mans-switch

A freshness checker for scheduled agent jobs. It does not watch for errors. It watches for the absence of success, and says so before you notice on your own.

## The problem

A scheduled job that stops firing does not announce itself. Nothing throws, nothing pages, no log fills up with stack traces. The job simply stops running, and the report, update, or sync it was supposed to produce is quietly missing from where you would expect it.

This happens more often than error-driven monitoring catches. A cron entry survives a server migration but the script it calls does not. A credential the job depends on expires and the job exits cleanly before doing anything, because nothing forced it to check. An agent's own recurring task gets dropped without a trace when its scheduler config changes. One lane behind this tool went dark for five weeks, unnoticed. No errors. No alerts. Just an increasingly stale set of outputs nobody was checking day to day.

Catching more failure modes does not fix this. Checking for success directly does.

## How it works

Each job you track writes a plain success string (a sentinel) into its own log file when it finishes. `deadmans.py` scans your log directory for each task's most recent log, inside a staleness window you configure, and checks for that sentinel. A task is a finding if:

- no log exists yet, and the task is not flagged manual
- the most recent log is older than its configured window
- the most recent log has no success sentinel
- the most recent log carries a failure sentinel instead

The exit code inverts what you would expect from a linter. 0 means every tracked task is fresh. 1 means at least one needs attention. That makes `deadmans.py check` a one-line addition to a cron job or a CI pipeline.

## Quickstart

```bash
python deadmans.py init          # writes a starter deadmans.json
```

Edit `deadmans.json` for your own tasks, then make sure each job writes its sentinel string into a log file that matches the configured pattern when it finishes successfully.

```bash
echo "MY_JOB_OK" >> "logs/my-job_$(date +%Y-%m-%d)-$(date +%H%M).log"
```

Run it from cron, from CI, or from wherever your own scheduling already lives.

```bash
python deadmans.py check
```

Exit code 1 means read the output. Exit code 0 means nothing to do.

## Config reference

Top-level keys in `deadmans.json`.

| Key | Meaning | Default |
|---|---|---|
| `log_dir` | Directory the tool scans for logs, resolved relative to the config file if not absolute | `logs` |
| `log_pattern` | Filename template using `{task}`, `{date}`, `{time}` placeholders | `{task}_{date}-{time}.log` |
| `tasks` | List of tracked task objects, see below | required |

Keys inside each task object.

| Key | Meaning | Default |
|---|---|---|
| `name` | Task identifier, matched against the `{task}` slot in `log_pattern` | required |
| `max_age_hours` | How old the most recent log can be before it counts as stale | required |
| `sentinel` | Success string to look for in the most recent log | required |
| `failure_sentinel` | Optional string that marks an explicit failure | none |
| `manual` | If true, a task with no log yet reports `MANUAL_OK` instead of a finding | `false` |

See [`deadmans.example.json`](deadmans.example.json) for a working two-task example.

## The manual flag

Not every tracked task runs on a fixed clock. Some get invoked by hand, or by an agent, on an irregular cadence. A periodic audit, a cleanup pass, something run when someone gets to it rather than on a schedule. For those, a missing log on day one is not a finding. It just means nobody has run it yet.

Set `manual: true` for those tasks. A manual task with no log at all reports `MANUAL_OK`, not `NEVER_RAN`. After that first run, the same staleness window applies as any other task. `manual` only changes what "no log yet" means, not what "an old log" means.

## What "fresh" actually means

`check` reports one of these states per task. `FRESH`, `STALE`, `FAILED`, `NO_SENTINEL`, `NEVER_RAN`, `MANUAL_OK`, or `LOG_UNREADABLE` if the log file itself cannot be read. Only `FRESH` and `MANUAL_OK` pass.

Staleness is checked first. A log old enough to breach its window is `STALE` regardless of what it contains. Inside the window, being recent is not automatically a pass. A log without the success sentinel still fails the check, whether that is because the job crashed and left a `failure_sentinel` behind, or because it exited without writing anything conclusive at all.

## CLI

```
python deadmans.py check [--config PATH] [--json] [--task NAME]
python deadmans.py init [--config PATH]
python deadmans.py selftest
```

`check` is the one to run on a schedule. It exits 0 when every task is fresh and 1 when at least one is not. A config problem, such as a missing file or an unknown `--task` name, exits 2 instead. `init` writes a starter config so there is something to edit instead of a blank file. `selftest` runs the tool's own state machine against a throwaway temp directory, a fast way to confirm the checker behaves correctly on whatever machine it was just copied to, independent of the repo's own test suite.

## Origin

This is pattern 3, ["Make silent failure loud (the dead-man's switch)"](https://github.com/jimy-r/agent-workspace-architecture/blob/main/PATTERNS.md#3-make-silent-failure-loud-the-dead-mans-switch), from the [agent-workspace-architecture](https://github.com/jimy-r/agent-workspace-architecture) reference, extracted into a standalone tool.

## License

MIT. See [LICENSE](LICENSE).
