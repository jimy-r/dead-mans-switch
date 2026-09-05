#!/usr/bin/env python3
"""deadmans.py -- a dead-man's-switch freshness checker for scheduled jobs.

A scheduled job that stops firing -- a cron entry, a CI nightly, an agent's
own recurring task -- fails silently. Nothing raises, nothing alerts, the
thing it was supposed to produce is just quietly missing. This tool inverts
the check: instead of watching for errors, it watches for the absence of
success.

Each tracked job writes a plain success sentinel string into its own log
file. This tool scans for that sentinel inside a per-task staleness window
and reports a finding whenever it is missing, stale, or replaced by a
failure sentinel, instead of waiting for a human to notice the job never
ran.

Origin: pattern 3, "Make silent failure loud (the dead-man's switch)",
from the agent-workspace-architecture reference --
https://github.com/jimy-r/agent-workspace-architecture/blob/main/PATTERNS.md#3-make-silent-failure-loud-the-dead-mans-switch

Stdlib only. Python 3.10+.

    python deadmans.py init            # write a starter deadmans.json
    python deadmans.py check           # exit 0 if every task is fresh
    python deadmans.py check --json
    python deadmans.py selftest        # verify the tool works on this machine
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

DEFAULT_CONFIG_PATH = Path("deadmans.json")
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_PATTERN = "{task}_{date}-{time}.log"
DEFAULT_TIMEZONE = "local"

# A log filename carries no offset, so "2026-01-15-0930" is only meaningful
# once you say which clock wrote it. Comparing that naive stamp against a
# naive local now() silently skews every age by the gap between the two --
# a UTC-stamping producer read from a UTC+10 host reports every job ten
# hours fresher than it is. Both sides are made aware instead.
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<hh>\d{2}):?(?P<mm>\d{2})$")

# States that mean "this task is not a finding". Everything else -- STALE,
# FAILED, NO_SENTINEL, NEVER_RAN, LOG_UNREADABLE -- fails the check.
OK_STATES = frozenset({"FRESH", "MANUAL_OK"})

EXAMPLE_CONFIG: dict[str, Any] = {
    "log_dir": "logs",
    "log_pattern": "{task}_{date}-{time}.log",
    "timezone": "local",
    "tasks": [
        {
            "name": "nightly-report",
            "max_age_hours": 30,
            "sentinel": "NIGHTLY_REPORT_OK",
            "failure_sentinel": "NIGHTLY_REPORT_FAILED",
            "manual": False,
        },
        {
            "name": "weekly-audit",
            "max_age_hours": 192,
            "sentinel": "WEEKLY_AUDIT_OK",
            "manual": True,
        },
    ],
}


class ConfigError(Exception):
    """Raised for a malformed or unreadable deadmans.json."""


def local_timezone() -> dt.tzinfo:
    """The host's current UTC offset as a concrete tzinfo.

    The offset is read once, so a config left on "local" across a daylight
    saving transition is off by an hour until the next run. Name the zone
    explicitly (``"timezone": "Australia/Brisbane"``) if that matters.
    """
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta(0)
    return dt.timezone(offset)


def resolve_timezone(spec: str | None) -> dt.tzinfo:
    """Turn a config `timezone` value into a tzinfo.

    Accepts "local" (the default), "UTC"/"Z", a fixed offset such as
    "+10:00" or "-0500", or an IANA zone name such as "Australia/Brisbane".
    IANA names need a tz database: it ships with most Linux and macOS
    installs, and on Windows it comes from the `tzdata` package. When one
    cannot be resolved this raises rather than quietly falling back, because
    a silent fallback to the wrong clock is the bug this key exists to fix.
    """
    if spec is None:
        return local_timezone()
    text = spec.strip()
    if not text or text.lower() == "local":
        return local_timezone()
    if text.upper() in {"UTC", "Z"}:
        return dt.timezone.utc
    match = _OFFSET_RE.match(text)
    if match:
        delta = dt.timedelta(
            hours=int(match.group("hh")), minutes=int(match.group("mm"))
        )
        if delta > dt.timedelta(hours=24):
            raise ConfigError(f"timezone offset out of range: {spec!r}")
        return dt.timezone(-delta if match.group("sign") == "-" else delta)
    try:
        return ZoneInfo(text)
    except Exception as exc:
        raise ConfigError(
            f"unknown timezone {spec!r}: expected 'local', 'UTC', an offset "
            f"like '+10:00', or an installed IANA zone name ({exc})"
        ) from exc


def as_aware(value: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """Read a naive datetime as `tz`; leave an already-aware one alone."""
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


class Task(NamedTuple):
    name: str
    max_age_hours: float
    sentinel: str
    failure_sentinel: str | None
    manual: bool


class Status(NamedTuple):
    task: str
    state: str
    last_run: dt.datetime | None
    age_hours: float | None
    detail: str


class Config(NamedTuple):
    tasks: dict[str, Task]
    log_dir: Path
    log_pattern: str
    tzinfo: dt.tzinfo


def compile_log_pattern(pattern: str, task_name: str) -> re.Pattern[str]:
    """Compile a "{task}_{date}-{time}.log" style pattern into a regex for
    one specific task's log filenames.

    {task} is substituted with the literal, escaped task name, so the
    resulting regex matches only that task's files. {date} matches
    YYYY-MM-DD and {time} matches HHMM, both captured by name. Everything
    else in the pattern -- separators, extension -- is matched literally.
    """
    if "{date}" not in pattern or "{time}" not in pattern:
        raise ConfigError(
            f"log_pattern {pattern!r} must contain both {{date}} and {{time}} placeholders"
        )
    if "{task}" not in pattern:
        raise ConfigError(
            f"log_pattern {pattern!r} must contain a {{task}} placeholder"
        )

    parts = re.split(r"(\{task\}|\{date\}|\{time\})", pattern)
    regex_parts = []
    for part in parts:
        if part == "{task}":
            regex_parts.append(re.escape(task_name))
        elif part == "{date}":
            regex_parts.append(r"(?P<date>\d{4}-\d{2}-\d{2})")
        elif part == "{time}":
            regex_parts.append(r"(?P<time>\d{4})")
        else:
            regex_parts.append(re.escape(part))
    return re.compile("^" + "".join(regex_parts) + "$")


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level JSON must be an object")

    log_dir_value = raw.get("log_dir", DEFAULT_LOG_DIR)
    if not isinstance(log_dir_value, str) or not log_dir_value:
        raise ConfigError(f"{path}: log_dir must be a non-empty string")
    log_dir = Path(log_dir_value)
    if not log_dir.is_absolute():
        log_dir = path.resolve().parent / log_dir

    log_pattern = raw.get("log_pattern", DEFAULT_LOG_PATTERN)
    if not isinstance(log_pattern, str) or not log_pattern:
        raise ConfigError(f"{path}: log_pattern must be a non-empty string")

    tz_value = raw.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(tz_value, str) or not tz_value:
        raise ConfigError(f"{path}: timezone must be a non-empty string")
    try:
        tzinfo = resolve_timezone(tz_value)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    task_entries = raw.get("tasks")
    if not isinstance(task_entries, list) or not task_entries:
        raise ConfigError(f"{path}: tasks must be a non-empty list")

    tasks: dict[str, Task] = {}
    for i, entry in enumerate(task_entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: tasks[{i}] must be an object")

        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"{path}: tasks[{i}] is missing a non-empty name")
        if name in tasks:
            raise ConfigError(f"{path}: duplicate task name {name!r}")

        max_age_hours = entry.get("max_age_hours")
        if not isinstance(max_age_hours, (int, float)) or isinstance(
            max_age_hours, bool
        ):
            raise ConfigError(f"{path}: task {name!r} needs a numeric max_age_hours")
        if max_age_hours <= 0:
            raise ConfigError(f"{path}: task {name!r} max_age_hours must be positive")

        sentinel = entry.get("sentinel")
        if not isinstance(sentinel, str) or not sentinel:
            raise ConfigError(f"{path}: task {name!r} needs a non-empty sentinel")

        failure_sentinel = entry.get("failure_sentinel")
        if failure_sentinel is not None and (
            not isinstance(failure_sentinel, str) or not failure_sentinel
        ):
            raise ConfigError(
                f"{path}: task {name!r} failure_sentinel must be a non-empty "
                "string if present"
            )

        manual = entry.get("manual", False)
        if not isinstance(manual, bool):
            raise ConfigError(f"{path}: task {name!r} manual must be true or false")

        tasks[name] = Task(
            name=name,
            max_age_hours=float(max_age_hours),
            sentinel=sentinel,
            failure_sentinel=failure_sentinel,
            manual=manual,
        )

    return Config(tasks=tasks, log_dir=log_dir, log_pattern=log_pattern, tzinfo=tzinfo)


def latest_log(task: str, log_dir: Path, log_pattern: str) -> Path | None:
    if not log_dir.is_dir():
        return None
    matcher = compile_log_pattern(log_pattern, task)
    candidates = [
        entry
        for entry in log_dir.iterdir()
        if entry.is_file() and matcher.match(entry.name)
    ]
    if not candidates:
        return None
    # Zero-padded YYYY-MM-DD/HHMM in a fixed position sorts lexicographically
    # in chronological order, same as the sample this generalises.
    return max(candidates, key=lambda p: p.name)


def parse_log_time(
    log: Path, task: str, log_pattern: str, tz: dt.tzinfo | None = None
) -> dt.datetime | None:
    """The timestamp encoded in a log filename, read as `tz` (default local)."""
    matcher = compile_log_pattern(log_pattern, task)
    match = matcher.match(log.name)
    if not match:
        return None
    try:
        return dt.datetime.strptime(
            f"{match.group('date')}-{match.group('time')}", "%Y-%m-%d-%H%M"
        ).replace(tzinfo=tz or local_timezone())
    except ValueError:
        # Matched the digit shape (e.g. a filename with month=99) but is not
        # a real date. Treat as an unknown timestamp rather than crashing the
        # whole check run over one malformed filename.
        return None


def check_task(
    task: Task,
    log_dir: Path,
    log_pattern: str,
    now: dt.datetime | None = None,
    tz: dt.tzinfo | None = None,
) -> Status:
    tz = tz or local_timezone()
    # A caller passing a naive `now` (a test, a fixed clock) means it in the
    # same zone the log stamps are read in, so read it that way rather than
    # raising on the aware/naive subtraction below.
    now = as_aware(now, tz) if now else dt.datetime.now(tz)
    log = latest_log(task.name, log_dir, log_pattern)
    if log is None:
        if task.manual:
            return Status(task.name, "MANUAL_OK", None, None, "manual task; no log yet")
        return Status(task.name, "NEVER_RAN", None, None, "no log file matches pattern")

    log_time = parse_log_time(log, task.name, log_pattern, tz)
    age_hours = (now - log_time).total_seconds() / 3600.0 if log_time else None

    try:
        body = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Status(
            task.name, "LOG_UNREADABLE", log_time, age_hours, f"read error: {exc}"
        )

    success = task.sentinel in body
    failure = task.failure_sentinel is not None and task.failure_sentinel in body

    # Staleness is checked first and short-circuits: a log old enough to
    # breach the window is a finding regardless of what it contains. Within
    # the window, a fresh timestamp alone is not a pass -- the sentinel
    # check still runs and can still fail a recent-looking log.
    if age_hours is not None and age_hours > task.max_age_hours:
        return Status(
            task.name,
            "STALE",
            log_time,
            age_hours,
            f"last log is {age_hours:.1f}h old (max {task.max_age_hours:.1f}h)",
        )

    if failure and not success:
        return Status(
            task.name,
            "FAILED",
            log_time,
            age_hours,
            "failure sentinel present, no success sentinel",
        )
    if not success:
        return Status(
            task.name,
            "NO_SENTINEL",
            log_time,
            age_hours,
            "no success sentinel in last log",
        )
    return Status(task.name, "FRESH", log_time, age_hours, "ok")


def render_text(statuses: list[Status], tz: dt.tzinfo | None = None) -> str:
    lines = [
        "Task freshness check -- "
        + dt.datetime.now(tz or local_timezone()).isoformat(timespec="seconds"),
        "",
    ]
    width = max(len(s.task) for s in statuses)
    for s in statuses:
        age = f"{s.age_hours:6.1f}h" if s.age_hours is not None else "   ----"
        lines.append(f"  {s.task:<{width}}  {s.state:<13}  {age}  {s.detail}")
    return "\n".join(lines) + "\n"


def render_json(statuses: list[Status], tz: dt.tzinfo | None = None) -> str:
    payload = [
        {
            "task": s.task,
            "state": s.state,
            "last_run": s.last_run.isoformat() if s.last_run else None,
            "age_hours": s.age_hours,
            "detail": s.detail,
        }
        for s in statuses
    ]
    return json.dumps(
        {
            "checked_at": dt.datetime.now(tz or local_timezone()).isoformat(
                timespec="seconds"
            ),
            "tasks": payload,
        },
        indent=2,
    )


def cmd_check(config_path: Path, only_task: str | None, as_json: bool) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    tasks = config.tasks
    if only_task:
        if only_task not in tasks:
            print(f"unknown task: {only_task}", file=sys.stderr)
            return 2
        tasks = {only_task: tasks[only_task]}

    statuses = [
        check_task(t, config.log_dir, config.log_pattern, tz=config.tzinfo)
        for t in tasks.values()
    ]

    if as_json:
        print(render_json(statuses, config.tzinfo))
    else:
        print(render_text(statuses, config.tzinfo))

    return 0 if all(s.state in OK_STATES for s in statuses) else 1


def cmd_init(config_path: Path) -> int:
    if config_path.exists():
        print(f"refusing to overwrite existing file: {config_path}", file=sys.stderr)
        return 1
    config_path.write_text(
        json.dumps(EXAMPLE_CONFIG, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote example config to {config_path}")
    return 0


def cmd_selftest() -> int:
    """Exercise every state on a throwaway temp dir. A quick sanity check for
    a freshly copied deadmans.py, independent of the repo's own
    test_deadmans.py suite."""
    failures: list[str] = []
    # A fixed clock, pinned to UTC so the scenarios below mean the same thing
    # on every host this file gets copied to.
    now = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()

        def write_log(task_name: str, when: dt.datetime, body: str) -> None:
            name = f"{task_name}_{when:%Y-%m-%d}-{when:%H%M}.log"
            (log_dir / name).write_text(body, encoding="utf-8")

        def expect(label: str, task: Task, expected_state: str) -> None:
            result = check_task(
                task, log_dir, DEFAULT_LOG_PATTERN, now=now, tz=dt.timezone.utc
            )
            if result.state != expected_state:
                failures.append(
                    f"{label}: expected {expected_state}, got {result.state} ({result.detail})"
                )

        # Each scenario gets its own task name so its log file cannot be
        # shadowed by a more-recent log left behind by an earlier scenario
        # in this same throwaway log_dir (latest-by-filename wins, same as
        # latest_log() everywhere else -- see test_picks_most_recent_by_filename
        # in test_deadmans.py for the behaviour this relies on).
        def scenario(name: str) -> Task:
            return Task(name, 24.0, "OK_SENTINEL", "FAIL_SENTINEL", False)

        write_log("fresh-case", now - dt.timedelta(hours=2), "hello\nOK_SENTINEL\n")
        expect("fresh log", scenario("fresh-case"), "FRESH")

        write_log("stale-case", now - dt.timedelta(hours=48), "hello\nOK_SENTINEL\n")
        expect("stale log", scenario("stale-case"), "STALE")

        write_log("failed-case", now - dt.timedelta(hours=1), "hello\nFAIL_SENTINEL\n")
        expect("failed log", scenario("failed-case"), "FAILED")

        write_log(
            "no-sentinel-case", now - dt.timedelta(hours=1), "hello, no sentinel here\n"
        )
        expect("sentinel-less log", scenario("no-sentinel-case"), "NO_SENTINEL")

        expect(
            "never ran",
            Task("never-ran", 24.0, "OK_SENTINEL", None, False),
            "NEVER_RAN",
        )
        expect(
            "manual, no log",
            Task("manual-task", 24.0, "OK_SENTINEL", None, True),
            "MANUAL_OK",
        )

    if failures:
        print("SELFTEST FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("DEADMANS_SELFTEST_OK -- all internal checks passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deadmans.py",
        description=(
            "Dead-man's-switch freshness checker: alert when a scheduled job's "
            "success sentinel goes missing or stale, not just when it errors."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check tracked tasks for freshness")
    check.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"path to config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    check.add_argument("--json", action="store_true", help="emit JSON instead of text")
    check.add_argument("--task", help="check only this task")

    init = sub.add_parser("init", help="write a starter config")
    init.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"path to write (default: {DEFAULT_CONFIG_PATH})",
    )

    sub.add_parser("selftest", help="run an internal smoke test on this machine")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return cmd_check(args.config, args.task, args.json)
    if args.command == "init":
        return cmd_init(args.config)
    return cmd_selftest()


if __name__ == "__main__":
    sys.exit(main())
