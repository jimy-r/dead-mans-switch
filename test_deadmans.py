"""Offline unittest suite for deadmans.py. No network, temp dirs only."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import deadmans


def write_log(log_dir: Path, name: str, body: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text(body, encoding="utf-8")
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

    def test_example_config_file_loads_cleanly(self) -> None:
        # deadmans.example.json ships in the repo root, next to this test.
        example = Path(__file__).resolve().parent / "deadmans.example.json"
        config = deadmans.load_config(example)
        self.assertEqual(set(config.tasks), {"nightly-report", "weekly-audit"})


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
        # The log is old enough to breach the window on its own.
        write_log(self.log_dir, "scheduled_2026-01-10-1000.log", "OK_SENTINEL\n")
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
