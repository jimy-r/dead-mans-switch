"""Offline unittest suite for deadmans.py. No network, temp dirs only."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import deadmans


def write_log(
    log_dir: Path, name: str, body: str, mtime: dt.datetime | None = None
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        # A tz-aware instant's own .timestamp() is host-timezone-independent,
        # unlike stat()'s default (the real filesystem clock at test time),
        # which is what a run-end comparison needs to be exact.
        stamp = mtime.timestamp()
        os.utime(path, (stamp, stamp))
    return path


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)


class TestCompileLogPattern(unittest.TestCase):
    def test_default_pattern_matches_expected_filename(self) -> None:
        matcher = deadmans.compile_log_pattern(
            deadmans.DEFAULT_LOG_PATTERN, "nightly-report"
        )
        match = matcher.match("nightly-report_2026-01-15-0930.log")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("date"), "2026-01-15")
        self.assertEqual(match.group("time"), "0930")

    def test_pattern_only_matches_its_own_task(self) -> None:
        matcher = deadmans.compile_log_pattern(
            deadmans.DEFAULT_LOG_PATTERN, "nightly-report"
        )
        self.assertIsNone(matcher.match("weekly-audit_2026-01-15-0930.log"))

    def test_missing_date_placeholder_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            deadmans.compile_log_pattern("{task}_{time}.log", "t")

    def test_missing_time_placeholder_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            deadmans.compile_log_pattern("{task}_{date}.log", "t")

    def test_missing_task_placeholder_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            deadmans.compile_log_pattern("{date}-{time}.log", "t")

    def test_custom_ordering_and_separators(self) -> None:
        matcher = deadmans.compile_log_pattern("log-{date}T{time}-{task}.txt", "sync")
        match = matcher.match("log-2026-02-01T2359-sync.txt")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("date"), "2026-02-01")
        self.assertEqual(match.group("time"), "2359")

    def test_task_name_with_regex_special_characters_is_escaped(self) -> None:
        matcher = deadmans.compile_log_pattern(
            deadmans.DEFAULT_LOG_PATTERN, "task.v1+x"
        )
        self.assertIsNotNone(matcher.match("task.v1+x_2026-01-15-0930.log"))
        # A regex-unsafe name should not accidentally match a different,
        # unrelated task via metacharacter interpretation.
        self.assertIsNone(matcher.match("taskAv1xx_2026-01-15-0930.log"))

    def test_time_accepts_seconds(self) -> None:
        matcher = deadmans.compile_log_pattern(deadmans.DEFAULT_LOG_PATTERN, "t")
        match = matcher.match("t_2026-01-15-093015.log")
        self.assertIsNotNone(match)
        self.assertEqual(match.group("time"), "093015")

    def test_time_of_any_other_width_does_not_match(self) -> None:
        matcher = deadmans.compile_log_pattern(deadmans.DEFAULT_LOG_PATTERN, "t")
        for name in (
            "t_2026-01-15-930.log",
            "t_2026-01-15-09301.log",
            "t_2026-01-15-0930150.log",
        ):
            with self.subTest(name=name):
                self.assertIsNone(matcher.match(name))

    def test_loose_pattern_matches_a_time_of_any_width(self) -> None:
        matcher = deadmans.compile_log_pattern(
            deadmans.DEFAULT_LOG_PATTERN, "t", loose=True
        )
        for name in (
            "t_2026-01-15-9.log",
            "t_2026-01-15-09301.log",
            "t_2026-01-15-0930.log",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(matcher.match(name))
        # Still this task's names only, and still a date in the date slot.
        self.assertIsNone(matcher.match("other_2026-01-15-0930.log"))
        self.assertIsNone(matcher.match("t_20260115-0930.log"))


class TestLoadConfig(TempDirCase):
    def config_path(self) -> Path:
        return self.tmp_path / "deadmans.json"

    def write_config(self, obj: dict) -> Path:
        path = self.config_path()
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def minimal_task(self, **overrides) -> dict:
        task = {
            "name": "nightly-report",
            "max_age_hours": 30,
            "sentinel": "NIGHTLY_REPORT_OK",
        }
        task.update(overrides)
        return task

    def test_valid_minimal_config_loads(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task()]})
        config = deadmans.load_config(path)
        self.assertIn("nightly-report", config.tasks)
        task = config.tasks["nightly-report"]
        self.assertEqual(task.max_age_hours, 30.0)
        self.assertEqual(task.sentinel, "NIGHTLY_REPORT_OK")
        self.assertIsNone(task.failure_sentinel)
        self.assertFalse(task.manual)

    def test_defaults_applied_when_log_dir_and_pattern_omitted(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task()]})
        config = deadmans.load_config(path)
        self.assertEqual(config.log_dir, path.resolve().parent / "logs")
        self.assertEqual(config.log_pattern, deadmans.DEFAULT_LOG_PATTERN)

    def test_relative_log_dir_resolves_against_config_directory(self) -> None:
        path = self.write_config(
            {"log_dir": "somewhere/logs", "tasks": [self.minimal_task()]}
        )
        config = deadmans.load_config(path)
        self.assertEqual(config.log_dir, path.resolve().parent / "somewhere" / "logs")

    def test_absolute_log_dir_is_kept_as_is(self) -> None:
        absolute = (self.tmp_path / "elsewhere").resolve()
        path = self.write_config(
            {"log_dir": str(absolute), "tasks": [self.minimal_task()]}
        )
        config = deadmans.load_config(path)
        self.assertEqual(config.log_dir, absolute)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(self.tmp_path / "does-not-exist.json")

    def test_invalid_json_raises(self) -> None:
        path = self.config_path()
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_top_level_must_be_object(self) -> None:
        path = self.write_config_raw("[1, 2, 3]")
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def write_config_raw(self, text: str) -> Path:
        path = self.config_path()
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_tasks_key_raises(self) -> None:
        path = self.write_config({"log_dir": "logs"})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_empty_tasks_list_raises(self) -> None:
        path = self.write_config({"tasks": []})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_missing_name_raises(self) -> None:
        task = self.minimal_task()
        del task["name"]
        path = self.write_config({"tasks": [task]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_duplicate_task_name_raises(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task(), self.minimal_task()]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_missing_max_age_hours_raises(self) -> None:
        task = self.minimal_task()
        del task["max_age_hours"]
        path = self.write_config({"tasks": [task]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_non_numeric_max_age_hours_raises(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task(max_age_hours="soon")]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_boolean_max_age_hours_raises(self) -> None:
        # bool is a subclass of int in Python; must be rejected explicitly.
        path = self.write_config({"tasks": [self.minimal_task(max_age_hours=True)]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_zero_max_age_hours_raises(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task(max_age_hours=0)]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_missing_sentinel_raises(self) -> None:
        task = self.minimal_task()
        del task["sentinel"]
        path = self.write_config({"tasks": [task]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_bad_failure_sentinel_type_raises(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task(failure_sentinel=123)]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_task_bad_manual_type_raises(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task(manual="yes")]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_manual_defaults_false(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task()]})
        config = deadmans.load_config(path)
        self.assertFalse(config.tasks["nightly-report"].manual)

    def test_failure_sentinel_optional(self) -> None:
        path = self.write_config(
            {"tasks": [self.minimal_task(failure_sentinel="NIGHTLY_REPORT_FAILED")]}
        )
        config = deadmans.load_config(path)
        self.assertEqual(
            config.tasks["nightly-report"].failure_sentinel, "NIGHTLY_REPORT_FAILED"
        )

    def test_timezone_defaults_to_local(self) -> None:
        path = self.write_config({"tasks": [self.minimal_task()]})
        config = deadmans.load_config(path)
        self.assertEqual(
            config.tzinfo.utcoffset(None), deadmans.local_timezone().utcoffset(None)
        )

    def test_timezone_key_is_honoured(self) -> None:
        path = self.write_config({"timezone": "UTC", "tasks": [self.minimal_task()]})
        self.assertEqual(
            deadmans.load_config(path).tzinfo.utcoffset(None), dt.timedelta(0)
        )

    def test_unknown_timezone_raises(self) -> None:
        path = self.write_config(
            {"timezone": "Not/AZone", "tasks": [self.minimal_task()]}
        )
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_non_string_timezone_raises(self) -> None:
        path = self.write_config({"timezone": 10, "tasks": [self.minimal_task()]})
        with self.assertRaises(deadmans.ConfigError):
            deadmans.load_config(path)

    def test_unknown_top_level_key_raises_naming_it(self) -> None:
        path = self.write_config(
            {"log_directory": "logs", "tasks": [self.minimal_task()]}
        )
        with self.assertRaises(deadmans.ConfigError) as caught:
            deadmans.load_config(path)
        message = str(caught.exception)
        self.assertIn("'log_directory'", message)
        self.assertIn("did you mean 'log_dir'", message)
        self.assertIn("log_pattern, timezone, tasks", message)

    def test_unknown_task_key_raises_naming_it_and_the_allowed_set(self) -> None:
        # The US spelling used to be dropped without a word, leaving the
        # artefact signal switched off for good.
        path = self.write_config(
            {"tasks": [self.minimal_task(artifact={"path": "out.txt"})]}
        )
        with self.assertRaises(deadmans.ConfigError) as caught:
            deadmans.load_config(path)
        message = str(caught.exception)
        self.assertIn("task 'nightly-report'", message)
        self.assertIn("'artifact' (did you mean 'artefact'?)", message)
        for key in deadmans.TASK_KEYS:
            self.assertIn(key, message)

    def test_unknown_key_on_a_nameless_task_names_its_index(self) -> None:
        path = self.write_config(
            {"tasks": [{"nmae": "x", "max_age_hours": 1, "sentinel": "OK"}]}
        )
        with self.assertRaises(deadmans.ConfigError) as caught:
            deadmans.load_config(path)
        self.assertIn("tasks[0]: unknown key 'nmae'", str(caught.exception))

    def test_example_config_file_loads_cleanly(self) -> None:
        # deadmans.example.json ships in the repo root, next to this test.
        example = Path(__file__).resolve().parent / "deadmans.example.json"
        config = deadmans.load_config(example)
        self.assertEqual(set(config.tasks), {"nightly-report", "weekly-audit"})

    def test_example_config_file_matches_what_init_writes(self) -> None:
        # A new key goes into both, so the shipped example and the starter
        # config `init` writes cannot drift apart.
        example = Path(__file__).resolve().parent / "deadmans.example.json"
        self.assertEqual(
            json.loads(example.read_text(encoding="utf-8")), deadmans.EXAMPLE_CONFIG
        )


class TestResolveTimezone(unittest.TestCase):
    def test_none_and_local_give_the_host_offset(self) -> None:
        expected = deadmans.local_timezone().utcoffset(None)
        for spec in (None, "local", "LOCAL", "  "):
            with self.subTest(spec=spec):
                self.assertEqual(
                    deadmans.resolve_timezone(spec).utcoffset(None), expected
                )

    def test_utc_aliases(self) -> None:
        for spec in ("UTC", "utc", "Z", "z"):
            with self.subTest(spec=spec):
                self.assertEqual(
                    deadmans.resolve_timezone(spec).utcoffset(None), dt.timedelta(0)
                )

    def test_fixed_offsets_with_and_without_a_colon(self) -> None:
        self.assertEqual(
            deadmans.resolve_timezone("+10:00").utcoffset(None),
            dt.timedelta(hours=10),
        )
        self.assertEqual(
            deadmans.resolve_timezone("-0530").utcoffset(None),
            dt.timedelta(hours=-5, minutes=-30),
        )

    def test_unknown_zone_raises_rather_than_falling_back(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            deadmans.resolve_timezone("Not/AZone")

    def test_out_of_range_offset_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            deadmans.resolve_timezone("+99:00")


class TestTimezoneSkew(TempDirCase):
    """The bug this key exists for: a UTC-stamping producer read from a
    UTC+10 host used to report every job ten hours fresher than it was."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.task = deadmans.Task(
            name="scheduled",
            max_age_hours=6.0,
            sentinel="OK_SENTINEL",
            failure_sentinel=None,
            manual=False,
        )

    def test_utc_stamps_read_as_utc_are_stale(self) -> None:
        # Log stamped 00:00 UTC; checker's clock is 20:00 on the same day in
        # UTC+10, i.e. 10:00 UTC. That is 10h of real age against a 6h window.
        write_log(self.log_dir, "scheduled_2026-01-15-0000.log", "OK_SENTINEL\n")
        now = dt.datetime(
            2026, 1, 15, 20, 0, tzinfo=dt.timezone(dt.timedelta(hours=10))
        )
        result = deadmans.check_task(
            self.task,
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=now,
            tz=dt.timezone.utc,
        )
        self.assertEqual(result.state, "STALE")
        self.assertAlmostEqual(result.age_hours, 10.0, places=3)

    def test_same_log_read_as_local_hides_the_age(self) -> None:
        # Identical inputs, tz left at the checker's own clock: the naive
        # comparison the old code made, and the false FRESH it produced.
        write_log(self.log_dir, "scheduled_2026-01-15-0000.log", "OK_SENTINEL\n")
        plus_ten = dt.timezone(dt.timedelta(hours=10))
        now = dt.datetime(2026, 1, 15, 4, 0, tzinfo=plus_ten)
        result = deadmans.check_task(
            self.task,
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=now,
            tz=plus_ten,
        )
        self.assertEqual(result.state, "FRESH")
        self.assertAlmostEqual(result.age_hours, 4.0, places=3)

    def test_naive_now_is_read_in_the_configured_zone(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-0000.log", "OK_SENTINEL\n")
        result = deadmans.check_task(
            self.task,
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=dt.datetime(2026, 1, 15, 4, 0),
            tz=dt.timezone.utc,
        )
        self.assertEqual(result.state, "FRESH")
        self.assertAlmostEqual(result.age_hours, 4.0, places=3)


class TestLatestLogAndParseTime(TempDirCase):
    def test_missing_log_dir_returns_none(self) -> None:
        missing = self.tmp_path / "nope"
        self.assertIsNone(
            deadmans.latest_log("t", missing, deadmans.DEFAULT_LOG_PATTERN)
        )

    def test_empty_log_dir_returns_none(self) -> None:
        log_dir = self.tmp_path / "logs"
        log_dir.mkdir()
        self.assertIsNone(
            deadmans.latest_log("t", log_dir, deadmans.DEFAULT_LOG_PATTERN)
        )

    def test_picks_most_recent_by_filename(self) -> None:
        log_dir = self.tmp_path / "logs"
        write_log(log_dir, "t_2026-01-01-0900.log", "old")
        newest = write_log(log_dir, "t_2026-01-03-0900.log", "newest")
        write_log(log_dir, "t_2026-01-02-0900.log", "middle")
        found = deadmans.latest_log("t", log_dir, deadmans.DEFAULT_LOG_PATTERN)
        self.assertEqual(found, newest)

    def test_ignores_other_tasks_files(self) -> None:
        log_dir = self.tmp_path / "logs"
        write_log(log_dir, "other-task_2026-01-05-0900.log", "not mine")
        self.assertIsNone(
            deadmans.latest_log("t", log_dir, deadmans.DEFAULT_LOG_PATTERN)
        )

    def test_ignores_files_not_matching_pattern(self) -> None:
        log_dir = self.tmp_path / "logs"
        write_log(log_dir, "t.log", "no timestamp")
        write_log(log_dir, "readme.txt", "irrelevant")
        self.assertIsNone(
            deadmans.latest_log("t", log_dir, deadmans.DEFAULT_LOG_PATTERN)
        )

    def test_ignores_subdirectories(self) -> None:
        log_dir = self.tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "t_2026-01-01-0900.log").mkdir()
        self.assertIsNone(
            deadmans.latest_log("t", log_dir, deadmans.DEFAULT_LOG_PATTERN)
        )

    def test_parse_log_time_round_trips(self) -> None:
        log_dir = self.tmp_path / "logs"
        log = write_log(log_dir, "t_2026-03-04-1530.log", "body")
        parsed = deadmans.parse_log_time(
            log, "t", deadmans.DEFAULT_LOG_PATTERN, dt.timezone.utc
        )
        self.assertEqual(
            parsed, dt.datetime(2026, 3, 4, 15, 30, tzinfo=dt.timezone.utc)
        )

    def test_parse_log_time_defaults_to_the_local_zone(self) -> None:
        log_dir = self.tmp_path / "logs"
        log = write_log(log_dir, "t_2026-03-04-1530.log", "body")
        parsed = deadmans.parse_log_time(log, "t", deadmans.DEFAULT_LOG_PATTERN)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), deadmans.local_timezone().utcoffset(None))

    def test_parse_log_time_invalid_date_returns_none(self) -> None:
        log_dir = self.tmp_path / "logs"
        # Matches the {4}-{2}-{2} digit shape but month=99 is not a real date.
        log = write_log(log_dir, "t_9999-99-99-9999.log", "body")
        self.assertIsNone(
            deadmans.parse_log_time(log, "t", deadmans.DEFAULT_LOG_PATTERN)
        )

    def test_parse_log_time_reads_a_six_digit_time(self) -> None:
        log_dir = self.tmp_path / "logs"
        log = write_log(log_dir, "t_2026-03-04-153045.log", "body")
        parsed = deadmans.parse_log_time(
            log, "t", deadmans.DEFAULT_LOG_PATTERN, dt.timezone.utc
        )
        self.assertEqual(
            parsed, dt.datetime(2026, 3, 4, 15, 30, 45, tzinfo=dt.timezone.utc)
        )

    def test_skips_a_name_with_an_impossible_date(self) -> None:
        # Month 13 sorts after every real month, so taking the last name in
        # sort order used to hand back this file instead of the real run.
        log_dir = self.tmp_path / "logs"
        real = write_log(log_dir, "t_2026-01-03-0900.log", "real")
        write_log(log_dir, "t_2026-13-01-0000.log", "stray")
        rejected: list[str] = []
        found = deadmans.latest_log(
            "t", log_dir, deadmans.DEFAULT_LOG_PATTERN, rejected=rejected
        )
        self.assertEqual(found, real)
        self.assertEqual(rejected, ["t_2026-13-01-0000.log"])

    def test_skips_a_name_stamped_ahead_of_the_clock(self) -> None:
        log_dir = self.tmp_path / "logs"
        real = write_log(log_dir, "t_2026-01-03-0900.log", "real")
        write_log(log_dir, "t_2099-12-01-0000.log", "stray")
        rejected: list[str] = []
        found = deadmans.latest_log(
            "t",
            log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            tz=dt.timezone.utc,
            now=dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc),
            rejected=rejected,
        )
        self.assertEqual(found, real)
        self.assertEqual(rejected, ["t_2099-12-01-0000.log"])

    def test_a_stamp_inside_the_skew_tolerance_still_counts(self) -> None:
        # Two hosts rarely agree to the second. A name three minutes ahead of
        # the checker's clock is a run; one six minutes ahead is not.
        log_dir = self.tmp_path / "logs"
        near = write_log(log_dir, "t_2026-01-15-1203.log", "three minutes ahead")
        write_log(log_dir, "t_2026-01-15-1206.log", "six minutes ahead")
        found = deadmans.latest_log(
            "t",
            log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            tz=dt.timezone.utc,
            now=dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(found, near)

    def test_a_log_from_just_before_the_clocks_go_back_is_not_ahead(self) -> None:
        # 01:50 happens twice on the morning the clocks go back. Stamped in
        # the first pass and checked twenty minutes later, in the second, the
        # wall clock reads 01:10, which looks like forty minutes before the log.
        try:
            zone = ZoneInfo("Europe/London")
        except ZoneInfoNotFoundError:
            self.skipTest("no tz database on this host")
        log_dir = self.tmp_path / "logs"
        log = write_log(log_dir, "t_2026-10-25-0150.log", "body")
        rejected: list[str] = []
        found = deadmans.latest_log(
            "t",
            log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            tz=zone,
            now=dt.datetime(2026, 10, 25, 1, 10, fold=1, tzinfo=zone),
            rejected=rejected,
        )
        self.assertEqual(found, log)
        self.assertEqual(rejected, [])

    def test_a_name_stamped_in_year_9999_is_skipped_without_overflow(self) -> None:
        # Converting that stamp to UTC from a zone behind it would run past
        # the last year a datetime can hold.
        log_dir = self.tmp_path / "logs"
        real = write_log(log_dir, "t_2026-01-03-0900.log", "real")
        write_log(log_dir, "t_9999-12-31-2359.log", "stray")
        behind = dt.timezone(dt.timedelta(hours=-5))
        rejected: list[str] = []
        found = deadmans.latest_log(
            "t",
            log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            tz=behind,
            now=dt.datetime(2026, 1, 15, 12, 0, tzinfo=behind),
            rejected=rejected,
        )
        self.assertEqual(found, real)
        self.assertEqual(rejected, ["t_9999-12-31-2359.log"])

    def test_picks_by_run_time_where_the_names_sort_another_way(self) -> None:
        # With the time ahead of the date in the pattern, the last name in
        # sort order is the latest time of day, whichever date it is from.
        log_dir = self.tmp_path / "logs"
        pattern = "{time}_{date}_{task}.log"
        write_log(log_dir, "2359_2026-01-01_t.log", "old")
        newest = write_log(log_dir, "0001_2026-01-15_t.log", "newest")
        self.assertEqual(deadmans.latest_log("t", log_dir, pattern), newest)

    def test_a_later_six_digit_time_outranks_a_four_digit_one(self) -> None:
        # As text "0930_run" sorts after "093015_run". As a time it is
        # fifteen seconds earlier.
        log_dir = self.tmp_path / "logs"
        pattern = "{task}_{date}-{time}_run.log"
        write_log(log_dir, "t_2026-01-15-0930_run.log", "earlier")
        newest = write_log(log_dir, "t_2026-01-15-093015_run.log", "later")
        self.assertEqual(deadmans.latest_log("t", log_dir, pattern), newest)

    def test_reports_every_name_it_set_aside(self) -> None:
        log_dir = self.tmp_path / "logs"
        write_log(log_dir, "t_2026-01-15-09301.log", "five-digit time")
        write_log(log_dir, "t_2026-13-01-0000.log", "month 13")
        write_log(log_dir, "t_2099-12-01-0000.log", "decades ahead")
        write_log(log_dir, "other_2026-01-15-09301.log", "another task's log")
        rejected: list[str] = []
        found = deadmans.latest_log(
            "t",
            log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            tz=dt.timezone.utc,
            now=dt.datetime(2026, 1, 15, 12, 0, tzinfo=dt.timezone.utc),
            rejected=rejected,
        )
        self.assertIsNone(found)
        self.assertEqual(
            rejected,
            [
                "t_2026-01-15-09301.log",
                "t_2026-13-01-0000.log",
                "t_2099-12-01-0000.log",
            ],
        )


class TestCheckTask(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0)
        self.scheduled = deadmans.Task(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel="FAIL_SENTINEL",
            manual=False,
        )

    def check(self) -> deadmans.Status:
        return deadmans.check_task(
            self.scheduled, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )

    def test_fresh_with_success_sentinel(self) -> None:
        write_log(
            self.log_dir, "scheduled_2026-01-15-1000.log", "run ok\nOK_SENTINEL\n"
        )
        self.assertEqual(self.check().state, "FRESH")

    def test_stale_when_older_than_window(self) -> None:
        write_log(
            self.log_dir, "scheduled_2026-01-13-1000.log", "run ok\nOK_SENTINEL\n"
        )
        self.assertEqual(self.check().state, "STALE")

    def test_stale_beats_failure_sentinel(self) -> None:
        # A log that is BOTH outside the freshness window AND carries the
        # failure sentinel is reported STALE, not FAILED: the sample's
        # check_task() tests age before content and returns immediately.
        write_log(
            self.log_dir, "scheduled_2026-01-13-1000.log", "run ok\nFAIL_SENTINEL\n"
        )
        self.assertEqual(self.check().state, "STALE")

    def test_missing_log_not_manual_is_never_ran(self) -> None:
        self.assertEqual(self.check().state, "NEVER_RAN")

    def test_missing_log_manual_is_manual_ok(self) -> None:
        self.scheduled = self.scheduled._replace(manual=True)
        self.assertEqual(self.check().state, "MANUAL_OK")

    def test_failure_sentinel_within_window_is_failed(self) -> None:
        write_log(
            self.log_dir, "scheduled_2026-01-15-1000.log", "run ok\nFAIL_SENTINEL\n"
        )
        self.assertEqual(self.check().state, "FAILED")

    def test_no_sentinel_within_window_is_no_sentinel(self) -> None:
        write_log(
            self.log_dir, "scheduled_2026-01-15-1000.log", "nothing to see here\n"
        )
        self.assertEqual(self.check().state, "NO_SENTINEL")

    def test_both_sentinels_present_is_fresh(self) -> None:
        # Success is checked with "not success" gating the failure branch,
        # so a log carrying both strings (e.g. a retry appended to the same
        # file) reads as success, matching the sample's precedence.
        write_log(
            self.log_dir,
            "scheduled_2026-01-15-1000.log",
            "FAIL_SENTINEL\nretried\nOK_SENTINEL\n",
        )
        self.assertEqual(self.check().state, "FRESH")

    def test_task_without_failure_sentinel_configured(self) -> None:
        task = deadmans.Task("solo", 24.0, "OK_SENTINEL", None, False)
        write_log(self.log_dir, "solo_2026-01-15-1000.log", "no sentinel string\n")
        result = deadmans.check_task(
            task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )
        self.assertEqual(result.state, "NO_SENTINEL")

    def test_log_unreadable_on_os_error(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("permission denied")
        ):
            result = self.check()
        self.assertEqual(result.state, "LOG_UNREADABLE")

    def test_age_hours_reported_on_fresh(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        result = self.check()
        self.assertAlmostEqual(result.age_hours, 2.0, places=3)

    def test_default_now_is_used_when_omitted(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        result = deadmans.check_task(
            self.scheduled, self.log_dir, deadmans.DEFAULT_LOG_PATTERN
        )
        self.assertEqual(result.state, "STALE")


class TestSentinelAnchoring(TempDirCase):
    """A log that merely mentions the sentinel is not a log that reports it."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0)
        self.task = deadmans.Task(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel="FAIL_SENTINEL",
            manual=False,
        )

    def check(self, body: str) -> deadmans.Status:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", body)
        return deadmans.check_task(
            self.task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )

    def test_mid_sentence_mention_is_not_a_success(self) -> None:
        body = "checked the log and found no OK_SENTINEL anywhere\n"
        self.assertEqual(self.check(body).state, "NO_SENTINEL")

    def test_mid_sentence_mention_of_the_failure_string_is_not_a_failure(self) -> None:
        body = "the previous run left FAIL_SENTINEL behind; this one recovered\n"
        self.assertEqual(self.check(body).state, "NO_SENTINEL")

    def test_sentinel_opening_its_own_line_passes(self) -> None:
        self.assertEqual(self.check("run started\nOK_SENTINEL\n").state, "FRESH")

    def test_markdown_and_indent_decoration_is_tolerated(self) -> None:
        for line in (
            "`OK_SENTINEL`",
            "  OK_SENTINEL",
            "**OK_SENTINEL**",
            "_OK_SENTINEL",
        ):
            with self.subTest(line=line):
                self.assertEqual(self.check(f"header\n{line}\n").state, "FRESH")

    def test_leading_byte_order_mark_does_not_hide_the_sentinel(self) -> None:
        self.assertEqual(self.check("﻿OK_SENTINEL\n").state, "FRESH")

    def test_longer_token_starting_with_the_sentinel_does_not_match(self) -> None:
        self.assertEqual(self.check("OK_SENTINEL_PENDING\n").state, "NO_SENTINEL")

    def test_failure_sentinel_on_its_own_line_still_fails(self) -> None:
        self.assertEqual(self.check("boom\nFAIL_SENTINEL\n").state, "FAILED")

    def test_non_word_final_character_still_matches(self) -> None:
        task = self.task._replace(sentinel="DONE!")
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "DONE!\n")
        result = deadmans.check_task(
            task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )
        self.assertEqual(result.state, "FRESH")


class TestFailurePatterns(TempDirCase):
    """`failure_patterns` marks a run FAILED the way `failure_sentinel` does,
    for failures the job reports in words nobody chose as a sentinel."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0)

    def check(self, body: str, *patterns: str) -> deadmans.Status:
        task = deadmans.Task(
            "scheduled",
            24.0,
            "OK_SENTINEL",
            "FAIL_SENTINEL",
            False,
            failure_patterns=deadmans.compile_failure_patterns(list(patterns), "t"),
        )
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", body)
        return deadmans.check_task(
            task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )

    def test_matching_line_is_failed_and_named(self) -> None:
        result = self.check("start\nAPI Error: 401 Unauthorized\n", "API Error: 401")
        self.assertEqual(result.state, "FAILED")
        self.assertIn("'API Error: 401'", result.detail)

    def test_pattern_is_anchored_to_the_start_of_the_line(self) -> None:
        body = "start\nretrying after API Error: 401\n"
        self.assertEqual(self.check(body, "API Error: 401").state, "NO_SENTINEL")
        self.assertEqual(self.check(body, ".*API Error: 401").state, "FAILED")

    def test_success_sentinel_still_wins(self) -> None:
        body = "API Error: 401\nretried\nOK_SENTINEL\n"
        self.assertEqual(self.check(body, "API Error: 401").state, "FRESH")

    def test_failure_sentinel_keeps_working_beside_patterns(self) -> None:
        result = self.check("FAIL_SENTINEL\n", "never matches")
        self.assertEqual(result.state, "FAILED")
        self.assertEqual(result.detail, "failure sentinel present, no success sentinel")

    def test_no_patterns_changes_nothing(self) -> None:
        self.assertEqual(self.check("API Error: 401\n").state, "NO_SENTINEL")


class TestFailurePatternsConfig(TempDirCase):
    def load(self, patterns) -> deadmans.Config:
        task = {"name": "t", "max_age_hours": 1, "sentinel": "OK"}
        if patterns is not None:
            task["failure_patterns"] = patterns
        path = self.tmp_path / "deadmans.json"
        path.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
        return deadmans.load_config(path)

    def test_absent_means_no_patterns(self) -> None:
        self.assertEqual(self.load(None).tasks["t"].failure_patterns, ())

    def test_patterns_compile_at_load(self) -> None:
        compiled = self.load(["API Error: 401", ".*bearer"]).tasks["t"].failure_patterns
        self.assertEqual([p.pattern for p in compiled], ["API Error: 401", ".*bearer"])

    def test_bad_values_raise(self) -> None:
        for bad in ("API Error", [401], [""], ["("], {"a": "b"}):
            with self.subTest(bad=bad), self.assertRaises(deadmans.ConfigError):
                self.load(bad)


class TestStartSentinel(TempDirCase):
    """A job that opens its log and never reaches the success string is hung,
    not merely sentinel-less."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0)
        self.task = deadmans.Task(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel="FAIL_SENTINEL",
            manual=False,
            start_sentinel="START_SENTINEL",
            max_runtime_hours=1.0,
        )

    def check(self, body: str, stamp: str = "1000", **overrides) -> deadmans.Status:
        write_log(self.log_dir, f"scheduled_2026-01-15-{stamp}.log", body)
        task = self.task._replace(**overrides) if overrides else self.task
        return deadmans.check_task(
            task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )

    def test_started_past_the_allowance_is_hung(self) -> None:
        result = self.check("START_SENTINEL\nworking...\n")
        self.assertEqual(result.state, "HUNG")
        self.assertIn("2.0h ago", result.detail)

    def test_started_inside_the_allowance_is_running(self) -> None:
        result = self.check("START_SENTINEL\nworking...\n", stamp="1145")
        self.assertEqual(result.state, "RUNNING")

    def test_running_is_not_a_finding(self) -> None:
        self.assertIn("RUNNING", deadmans.OK_STATES)

    def test_hung_is_a_finding(self) -> None:
        self.assertNotIn("HUNG", deadmans.OK_STATES)

    def test_start_then_success_is_still_fresh(self) -> None:
        self.assertEqual(self.check("START_SENTINEL\nOK_SENTINEL\n").state, "FRESH")

    def test_start_then_failure_is_still_failed(self) -> None:
        self.assertEqual(self.check("START_SENTINEL\nFAIL_SENTINEL\n").state, "FAILED")

    def test_no_start_sentinel_in_the_log_is_still_no_sentinel(self) -> None:
        self.assertEqual(self.check("nothing conclusive here\n").state, "NO_SENTINEL")

    def test_unconfigured_start_sentinel_leaves_behaviour_unchanged(self) -> None:
        result = self.check(
            "START_SENTINEL\nworking...\n", start_sentinel=None, max_runtime_hours=None
        )
        self.assertEqual(result.state, "NO_SENTINEL")

    def test_without_an_allowance_any_unfinished_start_is_hung(self) -> None:
        result = self.check(
            "START_SENTINEL\nworking...\n", stamp="1155", max_runtime_hours=None
        )
        self.assertEqual(result.state, "HUNG")

    def test_stale_still_wins_over_hung(self) -> None:
        write_log(
            self.log_dir, "scheduled_2026-01-13-1000.log", "START_SENTINEL\nworking\n"
        )
        result = deadmans.check_task(
            self.task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )
        self.assertEqual(result.state, "STALE")

    def test_mid_sentence_start_mention_does_not_count_as_a_start(self) -> None:
        result = self.check("grepping for START_SENTINEL in yesterday's log\n")
        self.assertEqual(result.state, "NO_SENTINEL")


class TestStartSentinelConfig(TempDirCase):
    def config(self, **task_keys) -> deadmans.Config:
        path = self.tmp_path / "deadmans.json"
        task = {"name": "t", "max_age_hours": 24, "sentinel": "OK"}
        task.update(task_keys)
        path.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
        return deadmans.load_config(path)

    def test_defaults_to_absent(self) -> None:
        task = self.config().tasks["t"]
        self.assertIsNone(task.start_sentinel)
        self.assertIsNone(task.max_runtime_hours)

    def test_both_keys_load(self) -> None:
        task = self.config(start_sentinel="GO", max_runtime_hours=2).tasks["t"]
        self.assertEqual(task.start_sentinel, "GO")
        self.assertEqual(task.max_runtime_hours, 2.0)

    def test_empty_start_sentinel_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config(start_sentinel="")

    def test_runtime_without_a_start_sentinel_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config(max_runtime_hours=2)

    def test_non_positive_runtime_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config(start_sentinel="GO", max_runtime_hours=0)

    def test_boolean_runtime_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config(start_sentinel="GO", max_runtime_hours=True)


class TestArtefactFreshness(TempDirCase):
    """A task invoked outside its logging wrapper still produces its output.
    Keying freshness on that output stops a permanent false-stale."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.ledger = self.tmp_path / "findings.jsonl"

    def task(self, **artefact_keys) -> deadmans.Task:
        artefact = deadmans.parse_artefact(
            {"path": str(self.ledger), **artefact_keys}, self.tmp_path, "test"
        )
        return deadmans.Task(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel=None,
            manual=False,
            artefact=artefact,
        )

    def write_ledger(self, *records: dict) -> None:
        self.ledger.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    def check(self, task: deadmans.Task) -> deadmans.Status:
        return deadmans.check_task(
            task,
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=self.now,
            tz=dt.timezone.utc,
        )

    def test_fresh_artefact_rescues_a_task_with_no_log_at_all(self) -> None:
        self.write_ledger({"ts": "2026-01-15T10:00:00", "event": "emit"})
        result = self.check(self.task(format="jsonl"))
        self.assertEqual(result.state, "FRESH")
        self.assertAlmostEqual(result.age_hours, 2.0, places=3)

    def test_without_an_artefact_the_same_task_never_ran(self) -> None:
        plain = self.task(format="jsonl")._replace(artefact=None)
        self.assertEqual(self.check(plain).state, "NEVER_RAN")

    def test_stale_artefact_and_no_log_is_stale(self) -> None:
        self.write_ledger({"ts": "2026-01-10T10:00:00", "event": "emit"})
        self.assertEqual(self.check(self.task(format="jsonl")).state, "STALE")

    def test_artefact_newer_than_the_log_wins(self) -> None:
        # The log is old enough to breach the window on its own, and it
        # finished (carries the success sentinel) right when its filename
        # says, so pinning its mtime there keeps the run-end comparison
        # exact regardless of the host running this test.
        write_log(
            self.log_dir,
            "scheduled_2026-01-10-1000.log",
            "OK_SENTINEL\n",
            mtime=dt.datetime(2026, 1, 10, 10, 0, tzinfo=dt.timezone.utc),
        )
        self.write_ledger({"ts": "2026-01-15T09:00:00", "event": "emit"})
        result = self.check(self.task(format="jsonl"))
        self.assertEqual(result.state, "FRESH")
        self.assertIn("newer than the last log", result.detail)

    def test_older_artefact_leaves_the_log_in_charge(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        self.write_ledger({"ts": "2026-01-11T09:00:00", "event": "emit"})
        result = self.check(self.task(format="jsonl"))
        self.assertEqual(result.state, "FRESH")
        self.assertEqual(result.detail, "ok")

    def test_missing_artefact_file_is_not_an_error(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        self.assertEqual(self.check(self.task(format="jsonl")).state, "FRESH")

    def test_match_filters_records_by_field(self) -> None:
        # Only the audit-run record should count; the drain record is newer
        # but does not mean the tracked job ran.
        self.write_ledger(
            {"ts": "2026-01-10T09:00:00", "source": "nightly-2026-01-10"},
            {"ts": "2026-01-15T09:00:00", "source": "manual-drain"},
        )
        task = self.task(format="jsonl", match={"source": r"^nightly-\d{4}"})
        self.assertEqual(self.check(task).state, "STALE")

    def test_match_admits_the_record_it_names(self) -> None:
        self.write_ledger(
            {"ts": "2026-01-15T09:00:00", "source": "nightly-2026-01-15"},
        )
        task = self.task(format="jsonl", match={"source": r"^nightly-\d{4}"})
        self.assertEqual(self.check(task).state, "FRESH")

    def test_malformed_lines_are_skipped_not_fatal(self) -> None:
        self.ledger.write_text(
            "\n".join(
                [
                    "not json at all",
                    "[1, 2, 3]",
                    json.dumps({"ts": "nonsense"}),
                    json.dumps({"nots": "2026-01-15T09:00:00"}),
                    json.dumps({"ts": "2026-01-15T09:00:00"}),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.check(self.task(format="jsonl")).state, "FRESH")

    def test_naive_record_stamps_are_read_in_the_configured_zone(self) -> None:
        self.write_ledger({"ts": "2026-01-15T10:00:00"})
        aware = self.task(format="jsonl")
        self.assertAlmostEqual(self.check(aware).age_hours, 2.0, places=3)

    def test_zulu_suffix_parses(self) -> None:
        self.write_ledger({"ts": "2026-01-15T10:00:00Z"})
        self.assertAlmostEqual(
            self.check(self.task(format="jsonl")).age_hours, 2.0, places=3
        )

    def test_mtime_format_uses_the_files_modification_time(self) -> None:
        self.ledger.write_text("anything at all", encoding="utf-8")
        result = deadmans.check_task(
            self.task(),
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            tz=dt.timezone.utc,
        )
        self.assertEqual(result.state, "FRESH")
        self.assertLess(result.age_hours, 1.0)


class TestSameRunArtefactPrecedence(TempDirCase):
    """An artefact the SAME run emits must not outrank that run's own FAILED
    or HUNG verdict. The old code compared the artefact against
    the log FILENAME's start stamp, so a record emitted anywhere during a
    run always read as "newer than the log" and hid whatever that run went
    on to record. The fix judges the artefact against when the run ENDED
    (the log's own mtime, or start + max_runtime_hours while a run with no
    sentinel yet is still within its allowance), only overrides once the
    artefact clears that by ARTEFACT_OVERRIDE_MARGIN, and ignores a record
    stamped more than FUTURE_SKEW_TOLERANCE ahead of the clock outright."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.ledger = self.tmp_path / "emit.jsonl"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.run_start = self.now - dt.timedelta(hours=6)

    def write_ledger(self, *whens: dt.datetime) -> None:
        self.ledger.write_text(
            "\n".join(json.dumps({"ts": w.isoformat()}) for w in whens) + "\n",
            encoding="utf-8",
        )

    def task(self, **overrides) -> deadmans.Task:
        artefact = deadmans.parse_artefact(
            {"path": str(self.ledger), "format": "jsonl"}, self.tmp_path, "test"
        )
        fields = dict(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel="FAIL_SENTINEL",
            manual=False,
            start_sentinel="START_SENTINEL",
            max_runtime_hours=5.0,
            artefact=artefact,
        )
        fields.update(overrides)
        return deadmans.Task(**fields)

    def check(self, task: deadmans.Task | None = None) -> deadmans.Status:
        return deadmans.check_task(
            task or self.task(),
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=self.now,
            tz=dt.timezone.utc,
        )

    def test_emit_partway_through_a_failed_run_does_not_read_fresh(self) -> None:
        # Started 6h ago, emitted to its artefact 20min in, then failed 5min
        # later (the log's mtime -- its last write -- is that failure
        # instant). The old comparison against the 6h-old filename stamp
        # read the 20-minute mark as newer than the log and hid the FAILED
        # verdict entirely.
        write_log(
            self.log_dir,
            "scheduled_2026-01-15-0600.log",
            "START_SENTINEL\nworking\nFAIL_SENTINEL\n",
            mtime=self.run_start + dt.timedelta(minutes=25),
        )
        self.write_ledger(self.run_start + dt.timedelta(minutes=20))
        self.assertEqual(self.check().state, "FAILED")

    def test_emit_partway_through_a_wedged_run_does_not_read_fresh(self) -> None:
        # Same shape, but the run never reached a sentinel at all -- it
        # wrote its start line, emitted once, then wedged. 6h after start is
        # past the 5h allowance, so the correct verdict is HUNG, not FRESH.
        write_log(
            self.log_dir,
            "scheduled_2026-01-15-0600.log",
            "START_SENTINEL\nworking...\n",
            mtime=self.run_start + dt.timedelta(minutes=1),
        )
        self.write_ledger(self.run_start + dt.timedelta(minutes=20))
        self.assertEqual(self.check().state, "HUNG")

    def test_future_dated_record_is_ignored_as_clock_skew(self) -> None:
        # A record stamped 30 days ahead is not evidence of a fresh run --
        # it is a clock problem -- so with no log at all the task reads as
        # though the record were not there.
        self.write_ledger(self.now + dt.timedelta(days=30))
        task = self.task(start_sentinel=None, max_runtime_hours=None)
        result = self.check(task)
        self.assertEqual(result.state, "NEVER_RAN")
        self.assertIn("clock skew", result.detail)

    def test_a_record_comfortably_past_the_margin_still_overrides(self) -> None:
        # The margin exists for the SAME run's own emit, not as a general
        # dead zone: a record from a later, distinct invocation still
        # overrides once it clears ARTEFACT_OVERRIDE_MARGIN past the run's
        # end.
        write_log(
            self.log_dir,
            "scheduled_2026-01-15-0600.log",
            "START_SENTINEL\nFAIL_SENTINEL\n",
            mtime=self.run_start + dt.timedelta(minutes=2),
        )
        self.write_ledger(self.run_start + dt.timedelta(minutes=17))
        result = self.check()
        self.assertEqual(result.state, "FRESH")
        self.assertIn("newer than the last logged run", result.detail)

    def test_a_failure_pattern_also_marks_the_run_finished(self) -> None:
        # Same as above with the failure reported by pattern. If the pattern
        # did not count as the run finishing, the run's end would stretch to
        # start + max_runtime_hours and the later record could not override.
        write_log(
            self.log_dir,
            "scheduled_2026-01-15-0600.log",
            "START_SENTINEL\nAPI Error: 401\n",
            mtime=self.run_start + dt.timedelta(minutes=2),
        )
        self.write_ledger(self.run_start + dt.timedelta(minutes=17))
        task = self.task(
            failure_sentinel=None,
            failure_patterns=deadmans.compile_failure_patterns(["API Error"], "t"),
        )
        self.assertEqual(self.check(task).state, "FRESH")


class TestArtefactConfig(TempDirCase):
    def config(self, artefact) -> deadmans.Config:
        path = self.tmp_path / "deadmans.json"
        task = {
            "name": "t",
            "max_age_hours": 24,
            "sentinel": "OK",
            "artefact": artefact,
        }
        path.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
        return deadmans.load_config(path)

    def test_absent_by_default(self) -> None:
        path = self.tmp_path / "deadmans.json"
        path.write_text(
            json.dumps(
                {"tasks": [{"name": "t", "max_age_hours": 1, "sentinel": "OK"}]}
            ),
            encoding="utf-8",
        )
        self.assertIsNone(deadmans.load_config(path).tasks["t"].artefact)

    def test_relative_path_resolves_against_the_config_directory(self) -> None:
        task = self.config({"path": "state/out.jsonl", "format": "jsonl"}).tasks["t"]
        self.assertEqual(task.artefact.path, self.tmp_path / "state" / "out.jsonl")

    def test_absolute_path_is_kept(self) -> None:
        absolute = (self.tmp_path / "out.jsonl").resolve()
        task = self.config({"path": str(absolute)}).tasks["t"]
        self.assertEqual(task.artefact.path, absolute)

    def test_defaults_to_mtime_and_ts(self) -> None:
        task = self.config({"path": "out.json"}).tasks["t"]
        self.assertEqual(task.artefact.format, "mtime")
        self.assertEqual(task.artefact.timestamp_field, "ts")
        self.assertEqual(task.artefact.match, ())

    def test_non_object_artefact_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config("out.jsonl")

    def test_missing_path_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config({"format": "jsonl"})

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config({"path": "out.jsonl", "format": "sqlite"})

    def test_empty_timestamp_field_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config({"path": "o.jsonl", "format": "jsonl", "timestamp_field": ""})

    def test_match_on_a_non_jsonl_artefact_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config({"path": "out.txt", "match": {"a": "b"}})

    def test_bad_regex_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config({"path": "o.jsonl", "format": "jsonl", "match": {"a": "("}})

    def test_non_string_regex_raises(self) -> None:
        with self.assertRaises(deadmans.ConfigError):
            self.config({"path": "o.jsonl", "format": "jsonl", "match": {"a": 7}})


class TestLogNameGuards(TempDirCase):
    """One stray log name must not decide a task, and a directory of names
    the checker cannot read is not the same thing as no log yet."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.task = deadmans.Task(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel=None,
            manual=False,
        )

    def check(self, task: deadmans.Task | None = None) -> deadmans.Status:
        return deadmans.check_task(
            task or self.task,
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=self.now,
            tz=dt.timezone.utc,
        )

    def test_future_stamped_name_does_not_hide_a_dead_job(self) -> None:
        # The job last ran five days ago. A stray name stamped decades ahead
        # carries the success line and sorts last, and it used to read FRESH
        # with a negative age for as long as the file sat there.
        write_log(self.log_dir, "scheduled_2026-01-10-1000.log", "OK_SENTINEL\n")
        write_log(self.log_dir, "scheduled_2099-12-01-0000.log", "OK_SENTINEL\n")
        result = self.check()
        self.assertEqual(result.state, "STALE")
        self.assertAlmostEqual(result.age_hours, 122.0, places=3)
        self.assertIn("scheduled_2099-12-01-0000.log", result.detail)

    def test_impossible_date_name_does_not_hide_a_dead_job(self) -> None:
        # Month 13 gave no age at all, so the staleness test never fired.
        write_log(self.log_dir, "scheduled_2026-01-10-1000.log", "OK_SENTINEL\n")
        write_log(self.log_dir, "scheduled_2026-13-01-0000.log", "OK_SENTINEL\n")
        result = self.check()
        self.assertEqual(result.state, "STALE")
        self.assertIn("scheduled_2026-13-01-0000.log", result.detail)

    def test_stray_name_beside_a_fresh_run_stays_fresh_and_is_named(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        write_log(self.log_dir, "scheduled_2099-12-01-0000.log", "anything\n")
        result = self.check()
        self.assertEqual(result.state, "FRESH")
        self.assertAlmostEqual(result.age_hours, 2.0, places=3)
        self.assertIn("skipped 1 log name(s)", result.detail)
        self.assertIn("scheduled_2099-12-01-0000.log", result.detail)

    def test_clean_directory_adds_nothing_to_the_detail(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", "OK_SENTINEL\n")
        self.assertEqual(self.check().detail, "ok")

    def test_manual_task_with_only_unreadable_names_is_a_finding(self) -> None:
        # A producer that names its logs with a five-digit time has run, and
        # MANUAL_OK ("no log yet") would never go stale.
        write_log(self.log_dir, "scheduled_2026-01-15-09301.log", "OK_SENTINEL\n")
        result = self.check(self.task._replace(manual=True))
        self.assertEqual(result.state, "LOG_UNREADABLE")
        self.assertIsNone(result.age_hours)
        self.assertIn("scheduled_2026-01-15-09301.log", result.detail)

    def test_scheduled_task_with_only_unreadable_names_is_not_never_ran(self) -> None:
        write_log(self.log_dir, "scheduled_2026-13-01-0000.log", "OK_SENTINEL\n")
        self.assertEqual(self.check().state, "LOG_UNREADABLE")

    def test_only_future_stamped_names_is_log_unreadable(self) -> None:
        write_log(self.log_dir, "scheduled_2099-12-01-0000.log", "OK_SENTINEL\n")
        self.assertEqual(self.check().state, "LOG_UNREADABLE")

    def test_another_tasks_odd_names_leave_a_manual_task_alone(self) -> None:
        write_log(self.log_dir, "other_2026-01-15-09301.log", "OK_SENTINEL\n")
        result = self.check(self.task._replace(manual=True))
        self.assertEqual(result.state, "MANUAL_OK")
        self.assertEqual(result.detail, "manual task; no log yet")

    def test_log_unreadable_is_a_finding(self) -> None:
        self.assertNotIn("LOG_UNREADABLE", deadmans.OK_STATES)

    def test_six_digit_time_gives_the_age_to_the_second(self) -> None:
        write_log(self.log_dir, "scheduled_2026-01-15-103015.log", "OK_SENTINEL\n")
        result = self.check()
        self.assertEqual(result.state, "FRESH")
        self.assertAlmostEqual(result.age_hours, 1.0 + 29.75 / 60.0, places=3)

    def test_fresh_artefact_still_decides_when_no_log_is_readable(self) -> None:
        ledger = self.tmp_path / "out.jsonl"
        ledger.write_text(
            json.dumps({"ts": "2026-01-15T10:00:00+00:00"}) + "\n", encoding="utf-8"
        )
        write_log(self.log_dir, "scheduled_2026-01-15-09301.log", "OK_SENTINEL\n")
        task = self.task._replace(artefact=deadmans.Artefact(ledger, "jsonl", "ts", ()))
        result = self.check(task)
        self.assertEqual(result.state, "FRESH")
        self.assertIn("no readable log file", result.detail)
        self.assertIn("scheduled_2026-01-15-09301.log", result.detail)

    def test_a_long_list_of_skipped_names_is_counted_not_printed(self) -> None:
        for minute in range(5):
            write_log(self.log_dir, f"scheduled_2099-12-01-000{minute}.log", "x\n")
        detail = self.check().detail
        self.assertIn("skipped 5 log name(s)", detail)
        self.assertIn("and 2 more", detail)
        self.assertNotIn("scheduled_2099-12-01-0004.log", detail)


class TestLineStamp(TempDirCase):
    """A producer that stamps every line never writes its sentinel at the
    start of one. `line_stamp` lets one ISO time sit in front of it."""

    def setUp(self) -> None:
        super().setUp()
        self.log_dir = self.tmp_path / "logs"
        self.now = dt.datetime(2026, 1, 15, 12, 0, 0)
        self.task = deadmans.Task(
            name="scheduled",
            max_age_hours=24.0,
            sentinel="OK_SENTINEL",
            failure_sentinel="FAIL_SENTINEL",
            manual=False,
            line_stamp=True,
        )

    def check(self, body: str, **overrides) -> deadmans.Status:
        write_log(self.log_dir, "scheduled_2026-01-15-1000.log", body)
        task = self.task._replace(**overrides) if overrides else self.task
        return deadmans.check_task(
            task, self.log_dir, deadmans.DEFAULT_LOG_PATTERN, now=self.now
        )

    def test_stamped_sentinel_is_not_a_success_by_default(self) -> None:
        body = "2026-01-15T10:00:00Z OK_SENTINEL\n"
        self.assertEqual(self.check(body, line_stamp=False).state, "NO_SENTINEL")

    def test_stamped_sentinel_passes_with_line_stamp(self) -> None:
        for stamp in (
            "2026-01-15T10:00:00",
            "2026-01-15T10:00:00Z",
            "2026-01-15T10:00:00.123+10:00",
            "2026-01-15T10:00:00-0500",
            "2026-01-15 10:00:00",
            "2026-01-15 10:00:00,123",
        ):
            with self.subTest(stamp=stamp):
                body = f"{stamp} starting\n{stamp} OK_SENTINEL\n"
                self.assertEqual(self.check(body).state, "FRESH")

    def test_unstamped_sentinel_still_passes_with_line_stamp(self) -> None:
        self.assertEqual(self.check("run ok\nOK_SENTINEL\n").state, "FRESH")

    def test_decoration_after_the_stamp_is_tolerated(self) -> None:
        body = "2026-01-15T10:00:00Z `OK_SENTINEL`\n"
        self.assertEqual(self.check(body).state, "FRESH")

    def test_mention_further_along_a_stamped_line_is_not_a_success(self) -> None:
        body = "2026-01-15T10:00:00Z looked for OK_SENTINEL and found none\n"
        self.assertEqual(self.check(body).state, "NO_SENTINEL")

    def test_only_one_stamp_is_allowed_in_front(self) -> None:
        body = "2026-01-15T10:00:00Z 2026-01-15T10:00:01Z OK_SENTINEL\n"
        self.assertEqual(self.check(body).state, "NO_SENTINEL")

    def test_a_date_alone_is_not_a_stamp(self) -> None:
        self.assertEqual(self.check("2026-01-15 OK_SENTINEL\n").state, "NO_SENTINEL")

    def test_stamped_failure_sentinel_fails(self) -> None:
        body = "2026-01-15T10:00:00Z FAIL_SENTINEL\n"
        self.assertEqual(self.check(body).state, "FAILED")

    def test_stamped_start_sentinel_reads_as_hung(self) -> None:
        result = self.check(
            "2026-01-15T10:00:00Z START_SENTINEL\n", start_sentinel="START_SENTINEL"
        )
        self.assertEqual(result.state, "HUNG")

    def test_stamped_finish_line_ends_the_run_for_the_artefact(self) -> None:
        # A stamped failure line still marks the run finished. If it did not,
        # the run's end would stretch to start + max_runtime_hours and a later
        # record from a separate invocation could not override the log.
        utc = dt.timezone.utc
        start = dt.datetime(2026, 1, 15, 6, 0, tzinfo=utc)
        ledger = self.tmp_path / "out.jsonl"
        ledger.write_text(
            json.dumps({"ts": (start + dt.timedelta(minutes=17)).isoformat()}) + "\n",
            encoding="utf-8",
        )
        write_log(
            self.log_dir,
            "scheduled_2026-01-15-0600.log",
            "2026-01-15T06:00:00Z START_SENTINEL\n2026-01-15T06:02:00Z FAIL_SENTINEL\n",
            mtime=start + dt.timedelta(minutes=2),
        )
        task = self.task._replace(
            start_sentinel="START_SENTINEL",
            max_runtime_hours=5.0,
            artefact=deadmans.Artefact(ledger, "jsonl", "ts", ()),
        )
        result = deadmans.check_task(
            task,
            self.log_dir,
            deadmans.DEFAULT_LOG_PATTERN,
            now=dt.datetime(2026, 1, 15, 12, 0, tzinfo=utc),
            tz=utc,
        )
        self.assertEqual(result.state, "FRESH")
        self.assertIn("newer than the last logged run", result.detail)


class TestLineStampConfig(TempDirCase):
    def config(self, **task_keys) -> deadmans.Config:
        path = self.tmp_path / "deadmans.json"
        task = {"name": "t", "max_age_hours": 24, "sentinel": "OK"}
        task.update(task_keys)
        path.write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
        return deadmans.load_config(path)

    def test_defaults_to_off(self) -> None:
        self.assertFalse(self.config().tasks["t"].line_stamp)

    def test_true_loads(self) -> None:
        self.assertTrue(self.config(line_stamp=True).tasks["t"].line_stamp)

    def test_non_boolean_raises(self) -> None:
        for value in ("yes", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(deadmans.ConfigError):
                    self.config(line_stamp=value)


class TestCLI(TempDirCase):
    def write_config(self, obj: dict) -> Path:
        path = self.tmp_path / "deadmans.json"
        path.write_text(json.dumps(obj), encoding="utf-8")
        return path

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = deadmans.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_check_all_fresh_exits_zero(self) -> None:
        config_path = self.write_config(
            {
                "tasks": [
                    {
                        "name": "solo",
                        "max_age_hours": 24,
                        "sentinel": "OK",
                        "manual": True,
                    }
                ]
            }
        )
        code, out, _err = self.run_main(["check", "--config", str(config_path)])
        self.assertEqual(code, 0)
        self.assertIn("solo", out)
        self.assertIn("MANUAL_OK", out)

    def test_check_stale_exits_one(self) -> None:
        config_path = self.write_config(
            {
                "log_dir": "logs",
                "tasks": [
                    {
                        "name": "solo",
                        "max_age_hours": 1,
                        "sentinel": "OK",
                        "manual": False,
                    }
                ],
            }
        )
        log_dir = config_path.parent / "logs"
        old = dt.datetime.now() - dt.timedelta(days=3)
        write_log(log_dir, f"solo_{old:%Y-%m-%d}-{old:%H%M}.log", "OK\n")
        code, out, _err = self.run_main(["check", "--config", str(config_path)])
        self.assertEqual(code, 1)
        self.assertIn("STALE", out)

    def test_check_unreadable_log_names_exit_one(self) -> None:
        config_path = self.write_config(
            {
                "log_dir": "logs",
                "tasks": [
                    {
                        "name": "solo",
                        "max_age_hours": 24,
                        "sentinel": "OK",
                        "manual": True,
                    }
                ],
            }
        )
        write_log(config_path.parent / "logs", "solo_2026-01-15-09301.log", "OK\n")
        code, out, _err = self.run_main(["check", "--config", str(config_path)])
        self.assertEqual(code, 1)
        self.assertIn("LOG_UNREADABLE", out)
        self.assertIn("solo_2026-01-15-09301.log", out)

    def test_check_json_output_is_valid_and_exit_matches(self) -> None:
        config_path = self.write_config(
            {
                "tasks": [
                    {
                        "name": "solo",
                        "max_age_hours": 24,
                        "sentinel": "OK",
                        "manual": True,
                    }
                ]
            }
        )
        code, out, _err = self.run_main(
            ["check", "--config", str(config_path), "--json"]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertIn("checked_at", payload)
        self.assertEqual(payload["tasks"][0]["task"], "solo")
        self.assertEqual(payload["tasks"][0]["state"], "MANUAL_OK")

    def test_check_task_filter_limits_to_one_task(self) -> None:
        config_path = self.write_config(
            {
                "tasks": [
                    {
                        "name": "a",
                        "max_age_hours": 24,
                        "sentinel": "A_OK",
                        "manual": True,
                    },
                    {
                        "name": "b",
                        "max_age_hours": 24,
                        "sentinel": "B_OK",
                        "manual": True,
                    },
                ]
            }
        )
        code, out, _err = self.run_main(
            ["check", "--config", str(config_path), "--task", "a", "--json"]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["tasks"][0]["task"], "a")

    def test_check_unknown_task_filter_exits_two(self) -> None:
        config_path = self.write_config(
            {
                "tasks": [
                    {
                        "name": "a",
                        "max_age_hours": 24,
                        "sentinel": "A_OK",
                        "manual": True,
                    }
                ]
            }
        )
        code, _out, err = self.run_main(
            ["check", "--config", str(config_path), "--task", "nonexistent"]
        )
        self.assertEqual(code, 2)
        self.assertIn("nonexistent", err)

    def test_check_missing_config_exits_two(self) -> None:
        code, _out, err = self.run_main(
            ["check", "--config", str(self.tmp_path / "missing.json")]
        )
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_check_malformed_config_exits_two(self) -> None:
        config_path = self.tmp_path / "deadmans.json"
        config_path.write_text("{broken", encoding="utf-8")
        code, _out, err = self.run_main(["check", "--config", str(config_path)])
        self.assertEqual(code, 2)
        self.assertIn("error", err)

    def test_check_unknown_config_key_exits_two(self) -> None:
        config_path = self.write_config(
            {
                "tasks": [
                    {
                        "name": "solo",
                        "max_age_hours": 24,
                        "sentinel": "OK",
                        "manual": True,
                        "artifact": {"path": "out.txt"},
                    }
                ]
            }
        )
        code, out, err = self.run_main(["check", "--config", str(config_path)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("unknown key 'artifact'", err)

    def test_init_writes_config(self) -> None:
        config_path = self.tmp_path / "deadmans.json"
        code, out, _err = self.run_main(["init", "--config", str(config_path)])
        self.assertEqual(code, 0)
        self.assertTrue(config_path.exists())
        self.assertIn("wrote", out)
        # The written file should itself be a loadable config.
        config = deadmans.load_config(config_path)
        self.assertEqual(set(config.tasks), {"nightly-report", "weekly-audit"})

    def test_init_refuses_to_overwrite(self) -> None:
        config_path = self.tmp_path / "deadmans.json"
        config_path.write_text('{"tasks": []}', encoding="utf-8")
        code, _out, err = self.run_main(["init", "--config", str(config_path)])
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        self.assertEqual(config_path.read_text(encoding="utf-8"), '{"tasks": []}')

    def test_selftest_exits_zero(self) -> None:
        code, out, _err = self.run_main(["selftest"])
        self.assertEqual(code, 0)
        self.assertIn("DEADMANS_SELFTEST_OK", out)

    def test_no_command_exits_nonzero(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                deadmans.main([])
        self.assertNotEqual(ctx.exception.code, 0)


class TestSelftestFunction(unittest.TestCase):
    def test_cmd_selftest_returns_zero(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = deadmans.cmd_selftest()
        self.assertEqual(code, 0)
        self.assertIn("DEADMANS_SELFTEST_OK", out.getvalue())


if __name__ == "__main__":
    unittest.main()
