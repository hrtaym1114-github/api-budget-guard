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

NOW = "2026-10-12T12:00:00+09:00"


class EdgeCaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "budget.json"
        self.ledger = self.root / "ledger.jsonl"
        self.env = mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": NOW})
        self.env.start()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.call("init", "--preset", "x-api"), 0)
        self.cfg = cli.load_config(self.config)
        self.tz = cli.tz_for(self.cfg)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def call(self, *args: str) -> int:
        return cli.main(["--config", str(self.config), "--ledger", str(self.ledger), *args])

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.call(*args)
        return code, out.getvalue(), err.getvalue()

    def at(self, value: str):
        return cli.parse_ts(value, self.tz)

    def phase(self, name: str) -> dict:
        return next(p for p in self.cfg["phases"] if p["name"] == name)

    def write_config(self, cfg: dict) -> None:
        self.config.write_text(json.dumps(cfg), encoding="utf-8")

    def spend(self, ts: str, routine: str, cost: float) -> None:
        cli.append_ledger(self.ledger, {"ts": ts, "kind": "spend", "routine": routine, "cost_usd": cost})

    def balance(self, ts: str, free: float, paid: float = 0.0) -> None:
        cli.append_ledger(self.ledger, {
            "ts": ts,
            "kind": "balance",
            "free_balance": free,
            "paid_balance": paid,
            "total_balance": free + paid,
        })

    def check(self, routine: str, estimate: float, when: str = NOW) -> dict:
        return cli.check_budget(self.cfg, cli.load_ledger(self.ledger), routine, estimate, self.at(when))


class PeriodTest(EdgeCaseBase):
    def test_period_label_month_only_for_exact_month(self) -> None:
        self.assertEqual(cli.period_label({"period": "month"}), "monthly")
        self.assertEqual(cli.period_label({}), "phase")
        self.assertEqual(cli.period_label({"period": "week"}), "phase")
        self.assertEqual(cli.period_label({"period": "Month"}), "phase")

    def test_phase_period_bounds_are_phase_bounds(self) -> None:
        start, end = cli.period_bounds(self.cfg, self.phase("launch-window"), self.at(NOW))
        self.assertEqual(start, self.at("2026-10-01T00:00:00+09:00"))
        self.assertEqual(end, self.at("2026-11-01T00:00:00+09:00"))

    def test_month_bounds_at_first_instant_and_last_second(self) -> None:
        steady = self.phase("steady")
        start, end = cli.period_bounds(self.cfg, steady, self.at("2026-11-01T00:00:00+09:00"))
        self.assertEqual((start, end), (self.at("2026-11-01T00:00:00+09:00"), self.at("2026-12-01T00:00:00+09:00")))
        start, end = cli.period_bounds(self.cfg, steady, self.at("2026-12-31T23:59:59+09:00"))
        self.assertEqual((start, end), (self.at("2026-12-01T00:00:00+09:00"), self.at("2027-01-01T00:00:00+09:00")))

    def test_month_bounds_use_config_timezone(self) -> None:
        # 2026-11-30T16:00Z is already December 1st in Asia/Tokyo.
        start, end = cli.period_bounds(self.cfg, self.phase("steady"), cli.parse_ts("2026-11-30T16:00:00Z"))
        self.assertEqual(start, self.at("2026-12-01T00:00:00+09:00"))
        self.assertEqual(end, self.at("2027-01-01T00:00:00+09:00"))

    def test_month_bounds_clipped_to_phase(self) -> None:
        phase = {"start": "2026-11-15T00:00:00+09:00", "end": "2026-11-25T00:00:00+09:00", "period": "month"}
        start, end = cli.period_bounds(self.cfg, phase, self.at("2026-11-20T00:00:00+09:00"))
        self.assertEqual(start, self.at("2026-11-15T00:00:00+09:00"))
        self.assertEqual(end, self.at("2026-11-25T00:00:00+09:00"))

    def test_current_phase_end_is_exclusive(self) -> None:
        self.assertEqual(cli.current_phase(self.cfg, self.at("2026-10-31T23:59:59+09:00"))["name"], "launch-window")
        self.assertEqual(cli.current_phase(self.cfg, self.at("2026-11-01T00:00:00+09:00"))["name"], "steady")
        self.assertIsNone(cli.current_phase(self.cfg, self.at("2028-01-01T00:00:00+09:00")))

    def test_spends_include_start_and_exclude_end(self) -> None:
        self.spend("2026-12-01T00:00:00+09:00", "keyword-watch", 0.1)
        self.spend("2027-01-01T00:00:00+09:00", "keyword-watch", 0.2)
        self.spend("2026-12-15T00:00:00+09:00", "mention-scan", 0.4)
        ledger = cli.load_ledger(self.ledger)
        start, end = self.at("2026-12-01T00:00:00+09:00"), self.at("2027-01-01T00:00:00+09:00")
        self.assertAlmostEqual(cli.spends(self.cfg, ledger, start, end), 0.5)
        self.assertAlmostEqual(cli.spends(self.cfg, ledger, start, end, "keyword-watch"), 0.1)

    def test_naive_ledger_timestamp_uses_config_timezone(self) -> None:
        self.spend("2026-12-01T00:30:00", "keyword-watch", 0.1)
        ledger = cli.load_ledger(self.ledger)
        start, end = self.at("2026-12-01T00:00:00+09:00"), self.at("2027-01-01T00:00:00+09:00")
        self.assertAlmostEqual(cli.spends(self.cfg, ledger, start, end), 0.1)


class CapTest(EdgeCaseBase):
    def test_phase_cap_adds_oneoff_approval(self) -> None:
        self.assertEqual(cli.phase_cap({"cap_usd": 3, "oneoff_approved_usd": 0.5}), 3.5)
        self.assertEqual(cli.phase_cap({"cap_usd": 3}), 3.0)
        self.assertEqual(cli.phase_cap({}), 0.0)

    def test_oneoff_research_period_cap_comes_from_approval(self) -> None:
        phase = {"oneoff_approved_usd": 0.4, "routines": {"oneoff-research": {"per_run_usd": 0.2, "period_usd": 9}}}
        self.assertEqual(cli.routine_cap(phase, "oneoff-research"), {"per_run_usd": 0.2, "period_usd": 0.4})
        self.assertEqual(cli.routine_cap(phase, "other"), {})

    def test_oneoff_approval_raises_check_cap(self) -> None:
        self.phase("launch-window")["oneoff_approved_usd"] = 1.0
        self.phase("launch-window")["routines"]["oneoff-research"] = {"per_run_usd": 0.5}
        result = self.check("oneoff-research", 0.5)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["cap"], 4.0)
        self.assertEqual(result["routine_cap"], 1.0)

    def test_per_run_cap_is_inclusive(self) -> None:
        self.assertTrue(self.check("keyword-watch", 0.25)["allowed"])
        result = self.check("keyword-watch", 0.26)
        self.assertFalse(result["allowed"])
        self.assertIn("estimate exceeds per-run cap $0.250", result["reason"])

    def test_zero_period_routine_denies_paid_but_allows_free(self) -> None:
        when = "2026-11-12T12:00:00+09:00"
        self.assertFalse(self.check("daily-report", 0.005, when)["allowed"])
        self.assertTrue(self.check("daily-report", 0.0, when)["allowed"])

    def test_unknown_routine_denied_only_for_paid_estimate(self) -> None:
        result = self.check("nightly-unknown", 0.01)
        self.assertFalse(result["allowed"])
        self.assertIn("routine has no configured cap: nightly-unknown", result["reason"])
        self.assertTrue(self.check("nightly-unknown", 0.0)["allowed"])

    def test_zero_phase_cap_stops(self) -> None:
        self.phase("launch-window")["cap_usd"] = 0
        result = self.check("keyword-watch", 0.0)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["level"], "stop")

    def test_threshold_edges(self) -> None:
        thresholds = self.cfg["thresholds"]
        self.assertEqual(cli.level_for(0.49, thresholds), "ok")
        self.assertEqual(cli.level_for(0.5, thresholds), "warn")
        self.assertEqual(cli.level_for(0.8, thresholds), "degrade")
        self.assertEqual(cli.level_for(1.0, thresholds), "stop")

    def test_warn_level_still_exits_ok(self) -> None:
        self.spend(NOW, "mention-scan", 1.5)
        code, out, _err = self.run_cli("check", "--routine", "keyword-watch", "--usd", "0.01")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(out.startswith("[go] keyword-watch: estimate $0.010; spent $0.000 of $0.80;"))

    def test_paid_alarm_cleared_by_ack(self) -> None:
        cli.append_ledger(self.ledger, {"ts": NOW, "kind": "balance", "paid_touched": True})
        self.assertTrue(cli.paid_alarm(self.cfg, cli.load_ledger(self.ledger)))
        cli.append_ledger(self.ledger, {"ts": NOW, "kind": "ack-paid"})
        self.assertFalse(cli.paid_alarm(self.cfg, cli.load_ledger(self.ledger)))


class FreeBalanceTest(EdgeCaseBase):
    def test_free_left_none_without_balance(self) -> None:
        self.assertIsNone(cli.free_left(self.cfg, []))

    def test_free_left_subtracts_only_later_spend(self) -> None:
        self.spend("2026-10-12T09:00:00+09:00", "keyword-watch", 0.3)
        self.balance("2026-10-12T10:00:00+09:00", 2.0)
        self.spend("2026-10-12T11:00:00+09:00", "keyword-watch", 0.5)
        self.assertAlmostEqual(cli.free_left(self.cfg, cli.load_ledger(self.ledger)), 1.5)

    def test_free_left_respects_paid_baseline(self) -> None:
        self.cfg["paid_baseline_usd"] = 1.0
        self.balance("2026-10-12T10:00:00+09:00", 2.0, 0.5)
        self.assertAlmostEqual(cli.free_left(self.cfg, cli.load_ledger(self.ledger)), 1.5)

    def test_safety_margin_stops_check(self) -> None:
        self.balance("2026-10-12T10:00:00+09:00", 0.6)
        self.assertTrue(self.check("keyword-watch", 0.1)["allowed"])
        result = self.check("keyword-watch", 0.2)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["level"], "stop")
        self.assertIn("estimate would cross free balance safety margin", result["reason"])

    def test_balance_row_charged_bucket(self) -> None:
        at = self.at(NOW)
        self.balance("2026-10-12T10:00:00+09:00", 10.0, 5.0)
        ledger = cli.load_ledger(self.ledger)
        self.assertEqual(cli.balance_row(self.cfg, ledger, 9.0, 5.0, at)["charged_bucket"], "free")
        self.assertEqual(cli.balance_row(self.cfg, ledger, 10.0, 4.0, at)["charged_bucket"], "paid")
        self.assertEqual(cli.balance_row(self.cfg, ledger, 9.0, 4.0, at)["charged_bucket"], "both")
        self.assertEqual(cli.balance_row(self.cfg, ledger, 10.0, 5.0, at)["charged_bucket"], "none")
        self.assertNotIn("charged_bucket", cli.balance_row(self.cfg, [], 1.0, 0.0, at))

    def test_balance_defaults_paid_to_remainder(self) -> None:
        code, out, _err = self.run_cli("balance", "--usd", "5", "--free", "3")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out.strip(), "Balance recorded: free $3.00, paid $2.00, total $5.00")


class InputTest(EdgeCaseBase):
    def test_parse_call_variants(self) -> None:
        self.assertEqual(cli.parse_call("t:3:OWNED"), ("t", 3, True))
        for spec in ("t:abc", "t:1:mine", "t:1:owned:x", "t"):
            with self.assertRaises(cli.GuardError):
                cli.parse_call(spec)

    def test_estimate_output_format(self) -> None:
        code, out, _err = self.run_cli(
            "estimate", "--call", "get_users_posts:20:owned", "--call", "search_posts_all:10"
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out.splitlines(), [
            "get_users_posts:20 owned $0.020",
            "search_posts_all:10 $0.050",
            "Total $0.070",
        ])

    def test_tool_without_pricing_rule_denies(self) -> None:
        self.cfg["tools"]["broken"] = {}
        self.cfg["tools"]["dangling"] = {"per_object": "nope"}
        with self.assertRaisesRegex(cli.GuardError, "tool has no pricing rule: broken"):
            cli.estimate_call(self.cfg, "broken", 1)
        with self.assertRaisesRegex(cli.GuardError, "missing event price: nope"):
            cli.estimate_call(self.cfg, "dangling", 1)

    def test_owned_falls_back_to_regular_price(self) -> None:
        self.assertAlmostEqual(cli.estimate_call(self.cfg, "search_posts_all", 10, owned=True), 0.05)

    def test_record_objects_for_free_tool_costs_zero(self) -> None:
        self.assertEqual(self.run_cli("record", "--routine", "adhoc", "--tool", "get_usage_credits", "--objects", "5")[0], 0)
        row = cli.load_ledger(self.ledger)[-1]
        self.assertEqual((row["objects"], row["cost_usd"]), (5, 0.0))


class AmountValidationTest(EdgeCaseBase):
    def assert_denied(self, message: str, *args: str) -> None:
        code, _out, err = self.run_cli(*args)
        self.assertEqual(code, cli.EXIT_DENY, args)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)

    def test_check_rejects_non_finite_and_negative_usd(self) -> None:
        for value in ("nan", "inf", "-5"):
            self.assert_denied("--usd must be a non-negative number", "check", "--routine", "keyword-watch", "--usd", value)

    def test_check_requires_call_or_usd(self) -> None:
        self.assert_denied("check requires --call or --usd", "check", "--routine", "keyword-watch")

    def test_check_accepts_zero_usd(self) -> None:
        self.assertEqual(self.run_cli("check", "--routine", "keyword-watch", "--usd", "0")[0], cli.EXIT_OK)

    def test_negative_call_count_rejected(self) -> None:
        with self.assertRaisesRegex(cli.GuardError, "call count must be non-negative"):
            cli.parse_call("search_posts_all:-5")
        self.assert_denied("call count must be non-negative", "estimate", "--call", "search_posts_all:-5")
        self.assertEqual(cli.parse_call("search_posts_all:0"), ("search_posts_all", 0, False))

    def test_record_rejects_bad_amounts_without_writing(self) -> None:
        self.assert_denied("--usd must be a non-negative number", "record", "--routine", "adhoc", "--tool", "x", "--usd", "nan")
        self.assert_denied("--usd must be a non-negative number", "record", "--routine", "adhoc", "--tool", "x", "--usd", "-1")
        self.assert_denied("--objects must be non-negative", "record", "--routine", "adhoc", "--tool", "search_posts_all", "--objects", "-3")
        self.assertEqual(cli.load_ledger(self.ledger), [])

    def test_balance_rejects_bad_amounts_without_writing(self) -> None:
        self.assert_denied("--usd must be a non-negative number", "balance", "--usd", "nan")
        self.assert_denied("--free must be a non-negative number", "balance", "--usd", "5", "--free", "-1")
        self.assert_denied("--paid must be a non-negative number", "balance", "--usd", "5", "--paid", "inf")
        self.assertEqual(cli.load_ledger(self.ledger), [])


class LedgerAndConfigTest(EdgeCaseBase):
    def test_blank_ledger_lines_are_skipped(self) -> None:
        self.ledger.write_text(f'\n  \n{{"ts": "{NOW}", "kind": "spend", "cost_usd": 0.1}}\n\n', encoding="utf-8")
        self.assertEqual(len(cli.load_ledger(self.ledger)), 1)

    def test_ledger_row_missing_fields_reports_line(self) -> None:
        self.ledger.write_text(f'{{"ts": "{NOW}", "kind": "spend"}}\n{{"ts": "{NOW}"}}\n', encoding="utf-8")
        code, _out, err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("ledger row is invalid at line 2", err)

    def test_ledger_non_object_row_denies(self) -> None:
        self.ledger.write_text("[1, 2]\n", encoding="utf-8")
        code, _out, err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("ledger row is invalid at line 1", err)

    def test_missing_ledger_denies_check(self) -> None:
        self.ledger.unlink()
        code, _out, err = self.run_cli("check", "--routine", "keyword-watch", "--usd", "0.01")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("ledger file not found", err)

    def test_missing_config_key_denies(self) -> None:
        for key in ("pricing", "tools", "thresholds", "phases"):
            cfg = {k: v for k, v in self.cfg.items() if k != key}
            self.write_config(cfg)
            code, _out, err = self.run_cli("status")
            self.assertEqual(code, cli.EXIT_DENY)
            self.assertIn(f"config file is missing required key: {key}", err)

    def test_unknown_timezone_denies(self) -> None:
        self.cfg["timezone"] = "Mars/Base"
        self.write_config(self.cfg)
        code, _out, err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("unknown timezone in config: Mars/Base", err)

    def test_init_refuses_to_overwrite(self) -> None:
        code, _out, err = self.run_cli("init")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("config file already exists", err)

    def test_status_without_phase(self) -> None:
        with mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "2028-06-01T00:00:00+09:00"}):
            code, out, _err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out.splitlines()[0], "API budget at 2028-06-01T00:00:00+09:00 phase=none")

    def test_run_returns_command_failure_without_recording(self) -> None:
        code, _out, _err = self.run_cli(
            "run", "--routine", "keyword-watch", "--call", "search_posts_all:1", "--", "python3", "-c", "raise SystemExit(3)"
        )
        self.assertEqual(code, 3)
        self.assertEqual(cli.load_ledger(self.ledger), [])

    def test_run_without_command_denies(self) -> None:
        code, _out, err = self.run_cli("run", "--routine", "keyword-watch", "--call", "search_posts_all:1")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("run requires a command after --", err)


if __name__ == "__main__":
    unittest.main()
