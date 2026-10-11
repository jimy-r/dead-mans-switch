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
import difflib
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

# Decoration a sentinel picks up on its way into a log: indentation from a
# shell wrapper, backticks or asterisks from anything that formats markdown.
# Tolerated in front of the sentinel; the sentinel still has to open the line.
_SENTINEL_DECORATION = r"[\s*_`]*"

# One ISO 8601 time in front of the sentinel, for a producer that stamps
# every line it writes ("2026-01-15T09:30:00Z MY_JOB_OK"). Opt-in per task
# through `line_stamp`. The stamp has to open the line and the sentinel has
# to follow it, so a mention further along a stamped line still does not
# count.
_LINE_STAMP = (
    r"(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?[ \t]+)?"
)

# States that mean "this task is not a finding". Everything else -- STALE,
# FAILED, HUNG, NO_SENTINEL, NEVER_RAN, LOG_UNREADABLE -- fails the check.
OK_STATES = frozenset({"FRESH", "MANUAL_OK", "RUNNING"})

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
            "failure_patterns": [r"Traceback \(most recent call last\)"],
            "start_sentinel": "NIGHTLY_REPORT_START",
            "max_runtime_hours": 2,
            "line_stamp": False,
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


TOP_LEVEL_KEYS = ("log_dir", "log_pattern", "timezone", "tasks")
TASK_KEYS = (
    "name",
    "max_age_hours",
    "sentinel",
    "failure_sentinel",
    "failure_patterns",
    "start_sentinel",
    "max_runtime_hours",
    "line_stamp",
    "manual",
    "artefact",
)


def reject_unknown_keys(
    entry: dict[str, Any], allowed: tuple[str, ...], where: str
) -> None:
    """Raise on any key the loader does not read.

    Every key is read with a default, so a misspelt one used to vanish
    without a word. `artifact` for `artefact` switched the artefact signal
    off, and a job that writes no log read as stale however fresh its
    output was.
    """
    unknown = [key for key in entry if key not in allowed]
    if not unknown:
        return
    named = []
    for key in unknown:
        close = difflib.get_close_matches(key, allowed, n=1)
        named.append(f"{key!r} (did you mean {close[0]!r}?)" if close else repr(key))
    noun = "key" if len(unknown) == 1 else "keys"
    raise ConfigError(
        f"{where}: unknown {noun} {', '.join(named)}; "
        f"allowed keys are {', '.join(allowed)}"
    )


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


class Artefact(NamedTuple):
    """A second freshness signal, keyed to what the job produces.

    A log line only exists if the job ran through whatever wrapper writes the
    log. Invoke the same job another way -- by hand, from a different host,
    through an agent rather than its cron entry -- and the log stays where it
    was while the real work happens, so the switch reports a permanent
    false-stale on a task that is running fine.
    """

    path: Path
    format: str
    timestamp_field: str
    match: tuple[tuple[str, re.Pattern[str]], ...]


class Task(NamedTuple):
    name: str
    max_age_hours: float
    sentinel: str
    failure_sentinel: str | None
    manual: bool
    start_sentinel: str | None = None
    max_runtime_hours: float | None = None
    artefact: Artefact | None = None
    failure_patterns: tuple[re.Pattern[str], ...] = ()
    line_stamp: bool = False


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


def compile_log_pattern(
    pattern: str, task_name: str, *, loose: bool = False
) -> re.Pattern[str]:
    """Compile a "{task}_{date}-{time}.log" style pattern into a regex for
    one specific task's log filenames.

    {task} is substituted with the literal, escaped task name, so the
    resulting regex matches only that task's files. {date} matches
    YYYY-MM-DD and {time} matches HHMM or HHMMSS, both captured by name.
    Everything else in the pattern -- separators, extension -- is matched
    literally.

    With `loose`, {time} matches a digit run of any width. That is the
    shape of a name this task's producer plausibly wrote, whether or not a
    run time can be read from it, and latest_log() uses it to tell "no log
    yet" apart from "logs the checker cannot read".
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
            regex_parts.append(
                r"(?P<time>\d+)" if loose else r"(?P<time>\d{4}(?:\d{2})?)"
            )
        else:
            regex_parts.append(re.escape(part))
    return re.compile("^" + "".join(regex_parts) + "$")


def compile_failure_patterns(raw: Any, where: str) -> tuple[re.Pattern[str], ...]:
    """Validate a task's `failure_patterns` list into compiled regexes."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: failure_patterns must be a list of regex strings")
    compiled = []
    for i, pattern in enumerate(raw):
        # An empty pattern matches every line, so every run would read FAILED.
        if not isinstance(pattern, str) or not pattern:
            raise ConfigError(
                f"{where}: failure_patterns[{i}] must be a non-empty regex string"
            )
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigError(
                f"{where}: failure_patterns[{i}] is not a valid regex: {exc}"
            ) from exc
    return tuple(compiled)


ARTEFACT_FORMATS = ("mtime", "jsonl")


def parse_artefact(entry: Any, base: Path, where: str) -> Artefact:
    """Validate one task's `artefact` object into an Artefact."""
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: artefact must be an object")

    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ConfigError(f"{where}: artefact needs a non-empty path")
    artefact_path = Path(raw_path)
    if not artefact_path.is_absolute():
        artefact_path = base / artefact_path

    fmt = entry.get("format", "mtime")
    if fmt not in ARTEFACT_FORMATS:
        raise ConfigError(
            f"{where}: artefact format must be one of {', '.join(ARTEFACT_FORMATS)}"
        )

    field = entry.get("timestamp_field", "ts")
    if not isinstance(field, str) or not field:
        raise ConfigError(
            f"{where}: artefact timestamp_field must be a non-empty string"
        )

    raw_match = entry.get("match", {})
    if not isinstance(raw_match, dict):
        raise ConfigError(f"{where}: artefact match must be an object")
    if raw_match and fmt != "jsonl":
        raise ConfigError(f"{where}: artefact match only applies to the jsonl format")
    match: list[tuple[str, re.Pattern[str]]] = []
    for key, pattern in raw_match.items():
        if not isinstance(pattern, str):
            raise ConfigError(f"{where}: artefact match {key!r} must be a regex string")
        try:
            match.append((key, re.compile(pattern)))
        except re.error as exc:
            raise ConfigError(
                f"{where}: artefact match {key!r} is not a valid regex: {exc}"
            ) from exc

    return Artefact(
        path=artefact_path,
        format=fmt,
        timestamp_field=field,
        match=tuple(match),
    )


def latest_artefact_time(
    artefact: Artefact,
    tz: dt.tzinfo,
    not_after: dt.datetime | None = None,
    skipped: list[dt.datetime] | None = None,
) -> dt.datetime | None:
    """When the artefact last changed, or None if it carries no usable signal.

    A missing file is not an error. It means this task has produced nothing
    yet, and the log-based path decides what that is worth.

    `not_after` rejects a record stamped further ahead of the
    clock than that instant as clock skew rather than reading it as a fresh
    run that has not happened yet; each rejected stamp is appended to
    `skipped` when the caller wants to report on it.
    """
    if not artefact.path.is_file():
        return None

    if artefact.format == "mtime":
        try:
            when = dt.datetime.fromtimestamp(artefact.path.stat().st_mtime, tz)
        except OSError:
            return None
        if not_after is not None and when > not_after:
            if skipped is not None:
                skipped.append(when)
            return None
        return when

    try:
        text = artefact.path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    newest: dt.datetime | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        # Matching on the record, not merely on the file changing, is what
        # keeps an unrelated write from reporting the lane fresh. A false
        # FRESH on a dead-man's switch is worse than the stale it replaces.
        if any(
            not pattern.search(str(record.get(key, "")))
            for key, pattern in artefact.match
        ):
            continue
        stamp = record.get(artefact.timestamp_field)
        if not isinstance(stamp, str):
            continue
        try:
            when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        when = as_aware(when, tz)
        if not_after is not None and when > not_after:
            if skipped is not None:
                skipped.append(when)
            continue
        if newest is None or when > newest:
            newest = when
    return newest


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
    reject_unknown_keys(raw, TOP_LEVEL_KEYS, str(path))

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
        label = f"task {name!r}" if isinstance(name, str) and name else f"tasks[{i}]"
        reject_unknown_keys(entry, TASK_KEYS, f"{path}: {label}")
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
        failure_patterns = compile_failure_patterns(
            entry.get("failure_patterns"), f"{path}: task {name!r}"
        )

        manual = entry.get("manual", False)
        if not isinstance(manual, bool):
            raise ConfigError(f"{path}: task {name!r} manual must be true or false")

        start_sentinel = entry.get("start_sentinel")
        if start_sentinel is not None and (
            not isinstance(start_sentinel, str) or not start_sentinel
        ):
            raise ConfigError(
                f"{path}: task {name!r} start_sentinel must be a non-empty "
                "string if present"
            )

        max_runtime_hours = entry.get("max_runtime_hours")
        if max_runtime_hours is not None:
            if start_sentinel is None:
                raise ConfigError(
                    f"{path}: task {name!r} sets max_runtime_hours but no "
                    "start_sentinel, so nothing marks when the run began"
                )
            if not isinstance(max_runtime_hours, (int, float)) or isinstance(
                max_runtime_hours, bool
            ):
                raise ConfigError(
                    f"{path}: task {name!r} max_runtime_hours must be numeric"
                )
            if max_runtime_hours <= 0:
                raise ConfigError(
                    f"{path}: task {name!r} max_runtime_hours must be positive"
                )
            max_runtime_hours = float(max_runtime_hours)

        line_stamp = entry.get("line_stamp", False)
        if not isinstance(line_stamp, bool):
            raise ConfigError(f"{path}: task {name!r} line_stamp must be true or false")

        raw_artefact = entry.get("artefact")
        artefact = (
            None
            if raw_artefact is None
            else parse_artefact(
                raw_artefact, path.resolve().parent, f"{path}: task {name!r}"
            )
        )

        tasks[name] = Task(
            name=name,
            max_age_hours=float(max_age_hours),
            sentinel=sentinel,
            failure_sentinel=failure_sentinel,
            manual=manual,
            start_sentinel=start_sentinel,
            max_runtime_hours=max_runtime_hours,
            artefact=artefact,
            failure_patterns=failure_patterns,
            line_stamp=line_stamp,
        )

    return Config(tasks=tasks, log_dir=log_dir, log_pattern=log_pattern, tzinfo=tzinfo)


# How far ahead of the clock a stamp may sit before it stops counting as
# evidence of a run. It covers two hosts whose clocks disagree slightly, and
# it applies to a log filename and to an artefact record alike.
FUTURE_SKEW_TOLERANCE = dt.timedelta(minutes=5)


def _stamp_time(match: re.Match[str], tz: dt.tzinfo) -> dt.datetime | None:
    """The run time a matched log filename encodes, or None if it has none."""
    stamp = match.group("time")
    fmt = "%Y-%m-%d-%H%M%S" if len(stamp) == 6 else "%Y-%m-%d-%H%M"
    try:
        return dt.datetime.strptime(f"{match.group('date')}-{stamp}", fmt).replace(
            tzinfo=tz
        )
    except ValueError:
        # Matched the digit shape (e.g. a filename with month=99) but is not
        # a real date. Treat as an unknown timestamp rather than crashing the
        # whole check run over one malformed filename.
        return None


def latest_log(
    task: str,
    log_dir: Path,
    log_pattern: str,
    tz: dt.tzinfo | None = None,
    now: dt.datetime | None = None,
    rejected: list[str] | None = None,
) -> Path | None:
    """This task's newest log by the run time in its filename, or None.

    A name only counts if a run time can be read from it and that time is
    not ahead of the clock. Picking the last name in sort order let one stray
    file decide the task: "job_2099-12-01-0000.log" gave a negative age and
    "job_2026-13-01-0000.log" gave none, the staleness test fired on neither,
    and a dead job read FRESH for as long as that file sat there holding the
    success line. Names set aside this way, and names whose time is some
    other width, are appended to `rejected` so the caller can report them.
    """
    if not log_dir.is_dir():
        return None
    tz = tz or local_timezone()
    now = as_aware(now, tz) if now else dt.datetime.now(tz)
    matcher = compile_log_pattern(log_pattern, task)
    lookalike = compile_log_pattern(log_pattern, task, loose=True)
    # The clock as an instant. Two aware datetimes in the same named zone
    # compare by wall time, which reads a log stamped just before the clocks
    # go back as most of an hour ahead; subtracting across zones compares the
    # instants, and cannot overflow on a name stamped in year 9999.
    now_utc = now.astimezone(dt.timezone.utc)
    candidates: list[tuple[dt.datetime, str, Path]] = []
    skipped: list[str] = []
    for entry in log_dir.iterdir():
        if not entry.is_file() or not lookalike.match(entry.name):
            continue
        match = matcher.match(entry.name)
        when = _stamp_time(match, tz) if match else None
        if when is None or when - now_utc > FUTURE_SKEW_TOLERANCE:
            skipped.append(entry.name)
            continue
        candidates.append((when, entry.name, entry))
    if rejected is not None:
        rejected.extend(sorted(skipped))
    if not candidates:
        return None
    # By run time, then by name. Sorting on the name alone is chronological
    # only while {date} leads {time} in the pattern and nothing sorts between
    # a four-digit and a six-digit time.
    return max(candidates, key=lambda found: found[:2])[2]


def parse_log_time(
    log: Path, task: str, log_pattern: str, tz: dt.tzinfo | None = None
) -> dt.datetime | None:
    """The timestamp encoded in a log filename, read as `tz` (default local)."""
    matcher = compile_log_pattern(log_pattern, task)
    match = matcher.match(log.name)
    if not match:
        return None
    return _stamp_time(match, tz or local_timezone())


def sentinel_matches(sentinel: str, body: str, line_stamp: bool = False) -> bool:
    """True when `sentinel` opens a line of `body`.

    A bare ``sentinel in body`` substring test passes a log that merely
    mentions the string: a run that quotes its own last failure, a summary
    line naming the sentinel it was looking for, a config file pasted into
    the output. That is a false FRESH on a dead-man's switch, which is worse
    than the false finding it avoids. Anchoring to the start of a line keeps
    the mention out while still accepting the decoration a real sentinel line
    picks up in practice.

    With `line_stamp`, one ISO 8601 time may sit in front of the sentinel.
    A producer that stamps every line never writes the sentinel at the start
    of one, so without this its runs read NO_SENTINEL however well they went.
    """
    head = body.lstrip("﻿")  # some producers open the file with a BOM
    # Stop a sentinel matching a longer token that starts with it, but only
    # when it ends in a word character -- \b after "OK!" would never match.
    tail = r"\b" if sentinel[-1:].isalnum() or sentinel.endswith("_") else ""
    lead = (_LINE_STAMP if line_stamp else "") + _SENTINEL_DECORATION
    return bool(re.search(rf"^{lead}{re.escape(sentinel)}{tail}", head, re.M))


def failure_reason(task: Task, body: str) -> str | None:
    """What marks this log as a failed run, or None when nothing does.

    Each of `failure_patterns` is tried with re.match against every line, so
    a pattern is anchored to the start of a line (not to its end). A pattern
    meant to match mid-line starts with `.*`.
    """
    if task.failure_sentinel is not None and sentinel_matches(
        task.failure_sentinel, body, task.line_stamp
    ):
        return "failure sentinel present"
    if task.failure_patterns:
        lines = body.lstrip("﻿").splitlines()
        for pattern in task.failure_patterns:
            if any(pattern.match(line) for line in lines):
                return f"failure pattern {pattern.pattern!r} matched"
    return None


# Artefact overrides the log only when it postdates that run's END by more
# than this margin: a record the SAME run emits partway through must not
# outrank that run's own FAILED or HUNG verdict. A record stamped
# further ahead of the clock than FUTURE_SKEW_TOLERANCE is never treated as
# evidence of a run that has not happened yet.
ARTEFACT_OVERRIDE_MARGIN = dt.timedelta(minutes=10)


def _logged_run_end(
    log: Path, log_time: dt.datetime | None, body: str, task: Task, tz: dt.tzinfo
) -> dt.datetime | None:
    """When the logged run ENDED, as far as the checker can tell.

    The log filename records when the run STARTED. A record the same run
    emits partway through is always "newer" than that start stamp, so
    comparing an artefact against the filename let a run's own emit hide
    that run's own FAILED or HUNG outcome. The log's mtime -- its last
    write -- is a much closer proxy for when it ended, once the run has
    actually finished (its success or failure sentinel is present). A log
    with neither may still be going, or be wedged, so it counts as running
    until start + max_runtime_hours rather than ending at its last byte.
    """
    try:
        end = dt.datetime.fromtimestamp(log.stat().st_mtime, tz)
    except OSError:
        end = log_time
    if log_time is not None and (end is None or end < log_time):
        end = log_time
    finished = (
        sentinel_matches(task.sentinel, body, task.line_stamp)
        or failure_reason(task, body) is not None
    )
    if not finished and task.max_runtime_hours is not None and log_time is not None:
        end = max(end, log_time + dt.timedelta(hours=task.max_runtime_hours))
    return end


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

    skewed: list[dt.datetime] = []
    rejected: list[str] = []
    status = _check_task(task, log_dir, log_pattern, now, tz, skewed, rejected)
    if rejected:
        # Say which names were set aside. A stray file is cheap to delete once
        # someone knows it is there, and a whole directory of them usually
        # means the producer and the config disagree about the clock.
        more = f" and {len(rejected) - 3} more" if len(rejected) > 3 else ""
        status = status._replace(
            detail=(
                f"{status.detail}; skipped {len(rejected)} log name(s) with no "
                f"usable run time: {', '.join(rejected[:3])}{more}"
            )
        )
    if skewed:
        status = status._replace(
            detail=(
                f"{status.detail}; ignored {len(skewed)} future-dated record(s), "
                f"newest {max(skewed):%Y-%m-%d %H:%M} (clock skew)"
            )
        )
    return status


def _check_task(
    task: Task,
    log_dir: Path,
    log_pattern: str,
    now: dt.datetime,
    tz: dt.tzinfo,
    skewed: list[dt.datetime],
    rejected: list[str],
) -> Status:
    artefact_time = (
        latest_artefact_time(
            task.artefact, tz, not_after=now + FUTURE_SKEW_TOLERANCE, skipped=skewed
        )
        if task.artefact is not None
        else None
    )

    def from_artefact(when: dt.datetime, note: str) -> Status:
        age = (now - when).total_seconds() / 3600.0
        label = task.artefact.path.name
        if age > task.max_age_hours:
            return Status(
                task.name,
                "STALE",
                when,
                age,
                f"newest {label} record is {age:.1f}h old "
                f"(max {task.max_age_hours:.1f}h)",
            )
        return Status(
            task.name, "FRESH", when, age, f"{label} updated {age:.1f}h ago; {note}"
        )

    log = latest_log(task.name, log_dir, log_pattern, tz, now, rejected)
    if log is None:
        if artefact_time is not None:
            return from_artefact(
                artefact_time,
                "no readable log file" if rejected else "no log file matches pattern",
            )
        if rejected:
            # The task has logs, but none gives a run time the checker can
            # trust, so "no log yet" would be false. On a manual task it would
            # stay false for good, because MANUAL_OK never goes stale.
            return Status(task.name, "LOG_UNREADABLE", None, None, "no readable log")
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

    # The artefact overrides the log only once it postdates that run's END
    # by more than the margin -- not merely the log's filename,
    # which is only when the run STARTED and so cannot tell a same-run emit
    # apart from a genuinely later, separate invocation.
    if artefact_time is not None:
        run_end = _logged_run_end(log, log_time, body, task, tz)
        if run_end is None or artefact_time > run_end + ARTEFACT_OVERRIDE_MARGIN:
            ended = f"{run_end:%Y-%m-%d %H:%M}" if run_end else "at an unknown time"
            return from_artefact(
                artefact_time, f"newer than the last logged run, which ended {ended}"
            )

    success = sentinel_matches(task.sentinel, body, task.line_stamp)
    failure = failure_reason(task, body)

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
            f"{failure}, no success sentinel",
        )
    # A log that opened but never reached its success string is a different
    # animal from one that says nothing at all: the job fired, then died or
    # wedged partway. Without a start sentinel both look like NO_SENTINEL,
    # and the operator cannot tell "the scheduler skipped it" from "it hung".
    if (
        not success
        and task.start_sentinel
        and sentinel_matches(task.start_sentinel, body, task.line_stamp)
    ):
        grace = task.max_runtime_hours
        if grace is not None and age_hours is not None and age_hours <= grace:
            return Status(
                task.name,
                "RUNNING",
                log_time,
                age_hours,
                f"started {age_hours:.1f}h ago, inside the {grace:.1f}h allowance",
            )
        started = (
            f"{age_hours:.1f}h ago" if age_hours is not None else "at an unknown time"
        )
        return Status(
            task.name,
            "HUNG",
            log_time,
            age_hours,
            f"started {started}, no success or failure sentinel since",
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
        # in this same throwaway log_dir (the newest run time in a filename
        # wins, same as latest_log() everywhere else -- see
        # test_picks_most_recent_by_filename in test_deadmans.py for the
        # behaviour this relies on).
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

        write_log(
            "hung-case", now - dt.timedelta(hours=3), "START_SENTINEL\nworking...\n"
        )
        expect(
            "started but never finished",
            Task(
                "hung-case",
                24.0,
                "OK_SENTINEL",
                "FAIL_SENTINEL",
                False,
                start_sentinel="START_SENTINEL",
                max_runtime_hours=1.0,
            ),
            "HUNG",
        )

        write_log(
            "running-case",
            now - dt.timedelta(minutes=10),
            "START_SENTINEL\nworking...\n",
        )
        expect(
            "started, still inside its runtime allowance",
            Task(
                "running-case",
                24.0,
                "OK_SENTINEL",
                "FAIL_SENTINEL",
                False,
                start_sentinel="START_SENTINEL",
                max_runtime_hours=1.0,
            ),
            "RUNNING",
        )

        expect(
            "never ran",
            Task("never-ran", 24.0, "OK_SENTINEL", None, False),
            "NEVER_RAN",
        )

        ledger = Path(tmp) / "artefact.jsonl"
        ledger.write_text(
            json.dumps({"ts": (now - dt.timedelta(hours=2)).isoformat()}) + "\n",
            encoding="utf-8",
        )
        expect(
            "no log, but the artefact is fresh",
            Task(
                "artefact-case",
                24.0,
                "OK_SENTINEL",
                None,
                False,
                artefact=Artefact(ledger, "jsonl", "ts", ()),
            ),
            "FRESH",
        )
        expect(
            "manual, no log",
            Task("manual-task", 24.0, "OK_SENTINEL", None, True),
            "MANUAL_OK",
        )

        # A stray name that sorts last must not stand in for the newest run.
        write_log("stray-case", now - dt.timedelta(hours=48), "hello\nOK_SENTINEL\n")
        for stray in (
            "stray-case_2099-12-01-0000.log",
            "stray-case_2026-13-01-0000.log",
        ):
            (log_dir / stray).write_text("OK_SENTINEL\n", encoding="utf-8")
        expect(
            "a future or impossible log name beside a stale run",
            scenario("stray-case"),
            "STALE",
        )

        (log_dir / "odd-width-case_2026-01-15-09301.log").write_text(
            "OK_SENTINEL\n", encoding="utf-8"
        )
        expect(
            "manual, with a log name no run time can be read from",
            Task("odd-width-case", 24.0, "OK_SENTINEL", None, True),
            "LOG_UNREADABLE",
        )

        (log_dir / "seconds-case_2026-01-15-100000.log").write_text(
            "OK_SENTINEL\n", encoding="utf-8"
        )
        expect("a six-digit time in the log name", scenario("seconds-case"), "FRESH")

        write_log(
            "stamped-case",
            now - dt.timedelta(hours=2),
            "2026-01-15T10:00:00Z OK_SENTINEL\n",
        )
        expect(
            "a line stamp in front of the sentinel, without line_stamp",
            scenario("stamped-case"),
            "NO_SENTINEL",
        )
        expect(
            "a line stamp in front of the sentinel, with line_stamp",
            scenario("stamped-case")._replace(line_stamp=True),
            "FRESH",
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
