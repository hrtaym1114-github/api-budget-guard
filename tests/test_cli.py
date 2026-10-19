from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api_budget_guard import cli


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "budget.json"
        self.ledger = self.root / "ledger.jsonl"
        self.env = mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "2026-10-12T12:00:00+09:00"})
        self.env.start()
        self.assertEqual(self.call("init", "--preset", "x-api"), 0)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def call(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--ledger", str(self.ledger), *args])

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_estimate_prices(self) -> None:
        cfg = cli.load_config(self.config)
        self.assertAlmostEqual(cli.estimate_call(cfg, "search_posts_all", 10), 0.05)
        self.assertAlmostEqual(cli.estimate_call(cfg, "get_users_posts", 20, owned=True), 0.02)
        self.assertAlmostEqual(cli.estimate_call(cfg, "get_users_by_usernames", 4), 0.04)
        self.assertAlmostEqual(cli.estimate_call(cfg, "get_posts_counts_recent", 3), 0.015)
        self.assertEqual(cli.estimate_call(cfg, "get_usage_credits", 1), 0)

    def test_parse_call(self) -> None:
        self.assertEqual(cli.parse_call("get_users_posts:20:owned"), ("get_users_posts", 20, True))
        self.assertEqual(cli.parse_call("search_posts_all:10"), ("search_posts_all", 10, False))
        with self.assertRaises(cli.GuardError):
            cli.parse_call("search_posts_all")

    def test_unknown_tool_fails_closed(self) -> None:
        self.assertEqual(self.call("estimate", "--call", "missing:1"), cli.EXIT_DENY)

    def test_check_allows_small_run(self) -> None:
        self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:10"), cli.EXIT_OK)

    def test_demo_loop_flips_go_to_deny(self) -> None:
        self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:10"), cli.EXIT_OK)
        for _ in range(20):
            self.assertEqual(
                self.call("record", "--routine", "keyword-watch", "--tool", "search_posts_all", "--objects", "10"),
                cli.EXIT_OK,
            )
        self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:10"), cli.EXIT_DENY)

    def test_degrade_at_threshold(self) -> None:
        cfg = cli.load_config(self.config)
        at = cli.now_for(cfg)
        cli.append_ledger(self.ledger, {"ts": at.isoformat(), "kind": "spend", "routine": "mention-scan", "cost_usd": 2.36})
        result = cli.check_budget(cfg, cli.load_ledger(self.ledger), "keyword-watch", 0.05, at)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["level"], "degrade")
        self.assertEqual(cli.exit_code_for(result), cli.EXIT_DEGRADE)

    def test_monthly_period_resets(self) -> None:
        cli.append_ledger(self.ledger, {"ts": "2026-11-20T12:00:00+09:00", "kind": "spend", "routine": "keyword-watch", "cost_usd": 0.8})
        with mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "2026-12-01T12:00:00+09:00"}):
            self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:10"), 0)

    def test_no_phase_denies(self) -> None:
        with mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "2028-01-01T00:00:00+09:00"}):
            self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:1"), cli.EXIT_DENY)

    def test_paid_baseline_drop_denies_all(self) -> None:
        cfg = cli.load_config(self.config)
        cfg["paid_baseline_usd"] = 10.0
        self.config.write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(self.call("balance", "--usd", "14", "--free", "5", "--paid", "9"), cli.EXIT_DENY)
        self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "get_usage_credits:1"), cli.EXIT_DENY)

    def test_stale_balance_degrades(self) -> None:
        cli.append_ledger(self.ledger, {"ts": "2026-10-10T00:00:00+09:00", "kind": "balance", "free_balance": 10, "paid_balance": 0, "total_balance": 10})
        self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:10"), cli.EXIT_DEGRADE)

    def test_fail_closed_missing_and_corrupt_files(self) -> None:
        self.assertEqual(
            cli.main(["--config", str(self.root / "missing.json"), "--ledger", str(self.root / "missing.jsonl"), "status"]),
            cli.EXIT_DENY,
        )
        self.ledger.write_text("{bad json\n", encoding="utf-8")
        self.assertEqual(self.call("status"), cli.EXIT_DENY)

    def test_record_usd_and_status_json(self) -> None:
        self.assertEqual(self.call("record", "--routine", "adhoc", "--tool", "search_posts_all", "--usd", "0.123"), 0)
        self.assertEqual(self.rows()[0]["cost_usd"], 0.123)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.call("status", "--json"), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["phase"], "launch-window")

    def test_check_json_shape(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.call("check", "--routine", "keyword-watch", "--call", "search_posts_all:10", "--json"), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(set(data), {"level", "cap", "spent", "remaining", "reason"})
        self.assertEqual(data["level"], "ok")
        self.assertEqual(data["cap"], 0.8)

    def test_run_wrapper_exports_level_and_records(self) -> None:
        marker = self.root / "level.txt"
        code = self.call(
            "run",
            "--routine",
            "keyword-watch",
            "--call",
            "search_posts_all:10",
            "--",
            "python3",
            "-c",
            f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text(os.environ['BUDGET_LEVEL'])",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(marker.read_text(encoding="utf-8"), "ok")
        self.assertEqual(self.rows()[-1]["note"], "recorded by run wrapper")

    def test_run_wrapper_skips_command_on_deny(self) -> None:
        marker = self.root / "ran.txt"
        self.assertEqual(
            self.call(
                "run",
                "--routine",
                "keyword-watch",
                "--call",
                "search_posts_all:1000",
                "--",
                "python3",
                "-c",
                f"open({str(marker)!r}, 'w').write('ran')",
            ),
            cli.EXIT_DENY,
        )
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()


class DenyWordingTest(unittest.TestCase):
    """Deny reasons must name the real cap window (phase vs monthly)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / "budget.json"
        self.ledger = root / "ledger.jsonl"
        with mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "2026-10-12T12:00:00+09:00"}):
            cli.main(["--config", str(self.config), "--ledger", str(self.ledger), "init", "--preset", "x-api"])
        self.cfg = cli.load_config(self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reason_at(self, when: str, routine: str, cost: float) -> str:
        tz = cli.tz_for(self.cfg)
        at = cli.parse_ts(when, tz)
        cli.append_ledger(self.ledger, {"ts": at.isoformat(), "kind": "spend", "routine": routine, "cost_usd": cost})
        result = cli.check_budget(self.cfg, cli.load_ledger(self.ledger), "keyword-watch", 0.05, at)
        self.assertFalse(result["allowed"])
        return result["reason"]

    def test_phase_period_routine_cap_says_phase(self) -> None:
        reason = self.reason_at("2026-10-12T12:00:00+09:00", "keyword-watch", 1.0)
        self.assertIn("routine phase cap reached", reason)
        self.assertNotIn("monthly", reason)

    def test_month_period_routine_cap_says_monthly(self) -> None:
        reason = self.reason_at("2026-11-12T12:00:00+09:00", "keyword-watch", 5.0)
        self.assertIn("routine monthly cap reached", reason)

    def test_phase_period_total_cap_says_phase(self) -> None:
        reason = self.reason_at("2026-10-12T12:00:00+09:00", "mention-scan", 50.0)
        self.assertIn("phase total cap reached", reason)
        self.assertNotIn("monthly", reason)

    def test_month_period_total_cap_says_monthly(self) -> None:
        reason = self.reason_at("2026-11-12T12:00:00+09:00", "mention-scan", 500.0)
        self.assertIn("monthly total cap reached", reason)

    def test_period_label(self) -> None:
        self.assertEqual(cli.period_label({"period": "month"}), "monthly")
        self.assertEqual(cli.period_label({"period": "phase"}), "phase")
