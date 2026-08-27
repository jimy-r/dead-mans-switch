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

    def test_example_config_file_loads_cleanly(self) -> None:
        # deadmans.example.json ships in the repo root, next to this test.
        example = Path(__file__).resolve().parent / "deadmans.example.json"
        config = deadmans.load_config(example)
        self.assertEqual(set(config.tasks), {"nightly-report", "weekly-audit"})


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
        parsed = deadmans.parse_log_time(log, "t", deadmans.DEFAULT_LOG_PATTERN)
        self.assertEqual(parsed, dt.datetime(2026, 3, 4, 15, 30))

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
