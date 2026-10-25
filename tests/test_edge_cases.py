from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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

    def test_balance_defaults_free_to_remainder(self) -> None:
        code, out, _err = self.run_cli("balance", "--usd", "5", "--paid", "4.95")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out.strip(), "Balance recorded: free $0.05, paid $4.95, total $5.00")

    def test_balance_rejects_inconsistent_split(self) -> None:
        for message, args in (
            ("--free plus --paid must equal --usd", ("balance", "--usd", "5", "--free", "2", "--paid", "2")),
            ("--paid cannot exceed --usd", ("balance", "--usd", "5", "--paid", "6")),
        ):
            code, _out, err = self.run_cli(*args)
            self.assertEqual(code, cli.EXIT_DENY)
            self.assertIn(message, err)
        self.assertEqual(cli.load_ledger(self.ledger), [])


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

    def assert_status_denied(self, message: str) -> None:
        code, _out, err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)

    def test_malformed_ledger_values_deny(self) -> None:
        cases = {
            '{"ts": "yesterday", "kind": "spend"}': "ledger row has invalid ts at line 1",
            '{"ts": 123, "kind": "spend"}': "ledger row has invalid ts at line 1",
            f'{{"ts": "{NOW}", "kind": "spend", "cost_usd": "abc"}}': "ledger row has invalid cost_usd at line 1",
            f'{{"ts": "{NOW}", "kind": "spend", "cost_usd": NaN}}': "ledger row has invalid cost_usd at line 1",
            f'{{"ts": "{NOW}", "kind": "balance", "free_balance": null}}': "ledger row has invalid free_balance at line 1",
        }
        for line, message in cases.items():
            self.ledger.write_text(line + "\n", encoding="utf-8")
            self.assert_status_denied(message)

    def test_negative_ledger_cost_denies(self) -> None:
        self.ledger.write_text(f'{{"ts": "{NOW}", "kind": "spend", "cost_usd": -3.0}}\n', encoding="utf-8")
        self.assert_status_denied("ledger row has negative cost_usd at line 1")

    def test_malformed_config_values_deny(self) -> None:
        def mutate(change):
            cfg = json.loads(json.dumps(self.cfg))
            change(cfg)
            self.write_config(cfg)

        cases = [
            (lambda c: c.update(timezone=""), "unknown timezone in config"),
            (lambda c: c.update(timezone=9), "unknown timezone in config: 9"),
            (lambda c: c.update(phases={}), "config phases must be a list"),
            (lambda c: c.update(tools=[]), "config tools must be an object"),
            (lambda c: c["thresholds"].pop("stop"), "config threshold is missing or not a number: stop"),
            (lambda c: c["thresholds"].update(warn="x"), "config threshold is missing or not a number: warn"),
            (lambda c: c["phases"][0].pop("end"), "config phase 1 is missing required key: end"),
            (lambda c: c["phases"][1].update(start="soon"), "config phase steady has invalid start: soon"),
            (lambda c: c["phases"][0].update(cap_usd="lots"), "config phase launch-window has non-numeric cap_usd"),
            (lambda c: c["phases"][0]["routines"].update(adhoc=1), "config routine adhoc must be an object"),
            (lambda c: c["phases"][0]["routines"]["adhoc"].update(period_usd=None), "config routine adhoc has non-numeric period_usd"),
        ]
        for change, message in cases:
            mutate(change)
            self.assert_status_denied(message)
        self.config.write_text("[1]", encoding="utf-8")
        self.assert_status_denied("config file must contain a JSON object")

    def test_numeric_strings_in_config_still_accepted(self) -> None:
        self.cfg["phases"][0]["cap_usd"] = "3.0"
        self.cfg["thresholds"]["stop"] = "1"
        self.write_config(self.cfg)
        self.assertEqual(self.run_cli("check", "--routine", "keyword-watch", "--usd", "0.01")[0], cli.EXIT_OK)

    def test_invalid_now_override_denies(self) -> None:
        with mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "garbage"}):
            self.assert_status_denied("invalid API_BUDGET_GUARD_NOW: garbage")

    def test_status_text_with_routine_missing_caps(self) -> None:
        del self.cfg["phases"][0]["routines"]["adhoc"]["period_usd"]
        self.write_config(self.cfg)
        code, out, _err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("- adhoc: $0.000 / $0.000 (per run $0.250)", out)
        self.assertFalse(self.check("adhoc", 0.01)["allowed"])

    def test_init_refuses_to_overwrite(self) -> None:
        code, _out, err = self.run_cli("init")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("config file already exists", err)

    def test_status_without_phase(self) -> None:
        with mock.patch.dict(os.environ, {"API_BUDGET_GUARD_NOW": "2028-06-01T00:00:00+09:00"}):
            code, out, _err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(out.splitlines()[0], "API budget at 2028-06-01T00:00:00+09:00 phase=none")

    def test_run_returns_command_failure_and_records(self) -> None:
        code, _out, _err = self.run_cli(
            "run", "--routine", "keyword-watch", "--call", "search_posts_all:1", "--", "python3", "-c", "raise SystemExit(3)"
        )
        self.assertEqual(code, 3)
        ledger = cli.load_ledger(self.ledger)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["cost_usd"], 0.005)
        self.assertIn("recorded by run wrapper", ledger[0]["note"])

    def test_run_without_command_denies(self) -> None:
        code, _out, err = self.run_cli("run", "--routine", "keyword-watch", "--call", "search_posts_all:1")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("run requires a command after --", err)


class PriceValidationTest(EdgeCaseBase):
    """H2: NaN and negative config rates must not slip past the budget check."""

    def test_bad_prices_deny_before_check(self) -> None:
        cases = (
            ("event", "post_read", float("nan")),
            ("event", "post_read", float("inf")),
            ("event", "post_read", float("-inf")),
            ("event", "post_read", -0.01),
            ("event", "post_read", "NaN"),
            ("event", "post_read", "-1"),
            ("request", "counts_recent", float("nan")),
            ("request", "counts_recent", -0.5),
            ("event", "news_read", True),
        )
        for bucket, name, rate in cases:
            cfg = json.loads(self.config.read_text(encoding="utf-8"))
            cfg["pricing"][bucket][name] = rate
            self.write_config(cfg)
            code, out, err = self.run_cli("check", "--routine", "keyword-watch", "--call", "search_posts_all:10", "--json")
            self.assertEqual(code, cli.EXIT_DENY, (bucket, name, rate, err))
            self.assertNotIn('"allowed": true', out)
            self.assertIn("must be a non-negative number", err)
            self.assertNotIn("Traceback", err)
            self.assertEqual(self.ledger.read_text(encoding="utf-8"), "")

    def test_negative_price_cannot_reopen_a_full_routine(self) -> None:
        self.spend(NOW, "keyword-watch", 0.8)
        self.cfg["pricing"]["event"]["post_read"] = -1
        self.write_config(self.cfg)
        code, out, err = self.run_cli("check", "--routine", "keyword-watch", "--call", "search_posts_all:10")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertNotIn("[go]", out)
        self.assertIn("config price event.post_read", err)
        self.assertEqual(len(cli.load_ledger(self.ledger)), 1)
        code, out, err = self.run_cli("record", "--routine", "keyword-watch", "--tool", "search_posts_all", "--objects", "10")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertNotIn("Recorded", out)
        self.assertEqual(len(cli.load_ledger(self.ledger)), 1)

    def test_estimate_and_check_reject_bad_rates_in_memory(self) -> None:
        self.cfg["pricing"]["event"]["post_read"] = float("nan")
        with self.assertRaisesRegex(cli.GuardError, "config price event.post_read"):
            cli.estimate_call(self.cfg, "search_posts_all", 10)
        self.cfg["pricing"]["event"]["post_read"] = -1
        with self.assertRaisesRegex(cli.GuardError, "config price event.post_read"):
            cli.estimate_call(self.cfg, "search_posts_all", 10)
        self.cfg["pricing"]["request"]["counts_recent"] = float("-inf")
        with self.assertRaisesRegex(cli.GuardError, "config price request.counts_recent"):
            cli.unit_price(self.cfg, "get_posts_counts_recent")
        for value in (float("nan"), float("inf"), -0.25):
            with self.assertRaisesRegex(cli.GuardError, "estimate must be a non-negative number"):
                self.check("keyword-watch", value)

    def test_zero_and_numeric_string_prices_still_pass(self) -> None:
        self.cfg["pricing"]["event"]["post_read"] = "0.005"
        self.write_config(self.cfg)
        self.assertEqual(self.run_cli("check", "--routine", "keyword-watch", "--call", "search_posts_all:10")[0], cli.EXIT_OK)
        self.cfg["pricing"]["event"]["post_read"] = 0
        self.write_config(self.cfg)
        self.assertEqual(self.run_cli("estimate", "--call", "search_posts_all:10")[0], cli.EXIT_OK)
        self.assertEqual(self.run_cli("check", "--routine", "keyword-watch", "--call", "search_posts_all:10")[0], cli.EXIT_OK)

    def test_pricing_bucket_must_be_object(self) -> None:
        self.cfg["pricing"]["event"] = []
        self.write_config(self.cfg)
        code, _out, err = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertIn("config pricing.event must be an object", err)


class DurableSpendTest(EdgeCaseBase):
    """H3: spend is reserved before the command, or the command does not start."""

    def test_run_persists_spend_before_command_starts(self) -> None:
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "rows = [json.loads(line) for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines() if line.strip()]\n"
            "ok = rows and rows[-1].get('kind') == 'spend' and abs(float(rows[-1].get('cost_usd', -1)) - 0.005) < 1e-9\n"
            "raise SystemExit(0 if ok else 4)\n"
        )
        code, _out, err = self.run_cli(
            "run", "--routine", "keyword-watch", "--call", "search_posts_all:1",
            "--", sys.executable, "-c", script, str(self.ledger),
        )
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertAlmostEqual(cli.load_ledger(self.ledger)[0]["cost_usd"], 0.005)
        self.assertEqual(cli.load_ledger(self.ledger)[0]["note"], "recorded by run wrapper")

    def test_sigterm_during_command_keeps_spend(self) -> None:
        ready = self.root / "ready"
        script = (
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('ready')\n"
            "time.sleep(30)\n"
        )
        env = os.environ.copy()
        src = str(Path(cli.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "api_budget_guard",
                "--config", str(self.config), "--ledger", str(self.ledger),
                "run", "--routine", "keyword-watch", "--call", "search_posts_all:1", "--",
                sys.executable, "-c", script, str(ready),
            ],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not ready.exists():
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            early = proc.poll() is not None
            err = proc.communicate()[1] if early else ""
            self.assertTrue(ready.exists(), err)
            self.assertEqual(len(cli.load_ledger(self.ledger)), 1)
            self.assertAlmostEqual(cli.load_ledger(self.ledger)[0]["cost_usd"], 0.005)
            if not early:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=5)
            rows = cli.load_ledger(self.ledger)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["cost_usd"], 0.005)
        finally:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=5)

    def test_readonly_ledger_does_not_start_command(self) -> None:
        marker = self.root / "ran.txt"
        os.chmod(self.ledger, 0o444)
        try:
            code, _out, err = self.run_cli(
                "run", "--routine", "keyword-watch", "--call", "search_posts_all:1",
                "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')",
            )
        finally:
            os.chmod(self.ledger, 0o644)
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertFalse(marker.exists())
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), "")

    def test_failed_fsync_does_not_acknowledge_or_start(self) -> None:
        marker = self.root / "ran.txt"
        calls = {"n": 0}

        def flaky(_fd: int) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("fsync failed")

        with mock.patch("api_budget_guard.cli.os.fsync", side_effect=flaky):
            code, out, err = self.run_cli(
                "record", "--routine", "keyword-watch", "--tool", "search_posts_all", "--objects", "10"
            )
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertNotIn("Recorded", out)
        self.assertIn("fsync failed", err)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), "")

        calls["n"] = 0
        with mock.patch("api_budget_guard.cli.os.fsync", side_effect=flaky):
            code, _out, err = self.run_cli(
                "run", "--routine", "keyword-watch", "--call", "search_posts_all:1",
                "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')",
            )
        self.assertEqual(code, cli.EXIT_DENY)
        self.assertFalse(marker.exists())
        self.assertIn("fsync failed", err)
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), "")

    def test_partial_append_rolls_back_previous_rows(self) -> None:
        original = '{"cost_usd": 0.2, "kind": "spend", "ts": "%s"}\n' % NOW
        self.ledger.write_text(original, encoding="utf-8")
        real_write = os.write
        state = {"n": 0}

        def flaky(fd: int, data: bytes) -> int:
            state["n"] += 1
            if state["n"] == 1:
                real_write(fd, data[:1])
                raise OSError("disk full")
            return real_write(fd, data)

        with mock.patch("api_budget_guard.cli.os.write", side_effect=flaky):
            with self.assertRaises(OSError):
                cli.append_ledger(self.ledger, {"ts": NOW, "kind": "spend", "routine": "keyword-watch", "cost_usd": 0.1})
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), original)
        self.assertEqual(len(cli.load_ledger(self.ledger)), 1)


class ConcurrentRunTest(EdgeCaseBase):
    """M2: overlapping run processes cannot share one remaining cap slot."""

    def _spawn(self, marker: Path, call: str, pause: float) -> subprocess.Popen[str]:
        script = (
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).write_text('ran')\n"
            "time.sleep(float(sys.argv[2]))\n"
        )
        env = os.environ.copy()
        src = str(Path(cli.__file__).resolve().parents[1])
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.Popen(
            [
                sys.executable, "-m", "api_budget_guard",
                "--config", str(self.config), "--ledger", str(self.ledger),
                "run", "--routine", "keyword-watch", "--call", call, "--",
                sys.executable, "-c", script, str(marker), str(pause),
            ],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _parallel(self, count: int, call: str, pause: float) -> tuple[list[tuple[int, str, str]], list[Path]]:
        markers = [self.root / f"ran-{index}" for index in range(count)]
        procs = [self._spawn(marker, call, pause) for marker in markers]
        results = []
        for proc in procs:
            try:
                out, err = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                self.fail(f"parallel run timed out\n{err}")
            results.append((proc.returncode, out, err))
        return results, [marker for marker in markers if marker.exists()]

    def test_parallel_runs_cannot_share_one_slot(self) -> None:
        routine = self.cfg["phases"][0]["routines"]["keyword-watch"]
        routine["per_run_usd"] = 0.05
        routine["period_usd"] = 0.05
        self.write_config(self.cfg)
        results, ran = self._parallel(4, "search_posts_all:10", 0.4)
        codes = sorted(code for code, _out, _err in results)
        detail = "\n".join(err for _code, _out, err in results)
        self.assertEqual(codes, [cli.EXIT_OK, cli.EXIT_DENY, cli.EXIT_DENY, cli.EXIT_DENY], detail)
        self.assertEqual(len(ran), 1, detail)
        rows = cli.load_ledger(self.ledger)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.05)
        self.assertTrue(any("cap reached" in err for _code, _out, err in results), detail)

    def test_parallel_runs_both_record_when_under_cap(self) -> None:
        results, ran = self._parallel(2, "search_posts_all:10", 0.0)
        detail = "\n".join(err for _code, _out, err in results)
        self.assertEqual([code for code, _out, _err in results], [cli.EXIT_OK, cli.EXIT_OK], detail)
        self.assertEqual(len(ran), 2)
        rows = cli.load_ledger(self.ledger)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(sum(row["cost_usd"] for row in rows), 0.1)


if __name__ == "__main__":
    unittest.main()
