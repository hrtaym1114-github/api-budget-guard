from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXIT_OK = 0
EXIT_DEGRADE = 10
EXIT_DENY = 20


class GuardError(Exception):
    pass


def fail(message: str) -> int:
    print(f"Denied: {message}", file=sys.stderr)
    return EXIT_DENY


def usd_amount(value: float, label: str) -> float:
    if not math.isfinite(value) or value < 0:
        raise GuardError(f"{label} must be a non-negative number: {value}")
    return value


def is_finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def tz_for(cfg: dict) -> ZoneInfo:
    name = cfg.get("timezone", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise GuardError(f"unknown timezone in config: {name}") from exc


def parse_ts(value: str, tz: ZoneInfo | None = None) -> datetime:
    if not isinstance(value, str):
        raise GuardError(f"invalid timestamp: {value!r}")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardError(f"invalid timestamp: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or ZoneInfo("UTC"))
    return dt


def now_for(cfg: dict | None = None) -> datetime:
    tz = tz_for(cfg or {"timezone": "UTC"})
    override = os.environ.get("API_BUDGET_GUARD_NOW")
    if not override:
        return datetime.now(tz)
    try:
        return parse_ts(override, tz).astimezone(tz)
    except GuardError as exc:
        raise GuardError(f"invalid API_BUDGET_GUARD_NOW: {override}") from exc


def load_config(path: Path) -> dict:
    if not path.exists():
        raise GuardError(f"config file not found: {path}")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"config file is unreadable or corrupt: {path}") from exc
    if not isinstance(cfg, dict):
        raise GuardError(f"config file must contain a JSON object: {path}")
    for key in ("pricing", "tools", "thresholds", "phases"):
        if key not in cfg:
            raise GuardError(f"config file is missing required key: {key}")
    for key in ("pricing", "tools", "thresholds"):
        if not isinstance(cfg[key], dict):
            raise GuardError(f"config {key} must be an object")
    for key in ("warn", "degrade", "stop"):
        if not is_finite_number(cfg["thresholds"].get(key)):
            raise GuardError(f"config threshold is missing or not a number: {key}")
    if not isinstance(cfg["phases"], list):
        raise GuardError("config phases must be a list")
    for index, phase in enumerate(cfg["phases"], 1):
        if not isinstance(phase, dict):
            raise GuardError(f"config phase {index} must be an object")
        for key in ("name", "start", "end"):
            if key not in phase:
                raise GuardError(f"config phase {index} is missing required key: {key}")
        for key in ("start", "end"):
            try:
                parse_ts(phase[key])
            except GuardError as exc:
                raise GuardError(f"config phase {phase['name']} has invalid {key}: {phase[key]}") from exc
        for key in ("cap_usd", "oneoff_approved_usd"):
            if key in phase and not is_finite_number(phase[key]):
                raise GuardError(f"config phase {phase['name']} has non-numeric {key}")
        if not isinstance(phase.get("routines", {}), dict):
            raise GuardError(f"config phase {phase['name']} routines must be an object")
        for name, routine in phase.get("routines", {}).items():
            if not isinstance(routine, dict):
                raise GuardError(f"config routine {name} must be an object")
            for key in ("per_run_usd", "period_usd"):
                if key in routine and not is_finite_number(routine[key]):
                    raise GuardError(f"config routine {name} has non-numeric {key}")
    return cfg


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        raise GuardError(f"ledger file not found: {path}")
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GuardError(f"ledger file is unreadable: {path}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GuardError(f"ledger file is corrupt at line {number}: {path}") from exc
        if not isinstance(row, dict) or "kind" not in row or "ts" not in row:
            raise GuardError(f"ledger row is invalid at line {number}: {path}")
        try:
            parse_ts(row["ts"])
        except GuardError as exc:
            raise GuardError(f"ledger row has invalid ts at line {number}: {path}") from exc
        for key in ("cost_usd", "free_balance", "paid_balance", "total_balance"):
            if key in row and not is_finite_number(row[key]):
                raise GuardError(f"ledger row has invalid {key} at line {number}: {path}")
        rows.append(row)
    return rows


def append_ledger(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def unit_price(cfg: dict, tool: str, owned: bool = False) -> tuple[float, str]:
    tools = cfg["tools"]
    if tool not in tools:
        raise GuardError(f"unknown tool: {tool}")
    info = tools[tool]
    if info.get("free"):
        return 0.0, "free"
    if "per_request" in info:
        key = info["per_request"]
        try:
            return float(cfg["pricing"]["request"][key]), "request"
        except KeyError as exc:
            raise GuardError(f"missing request price: {key}") from exc
    key = info.get("owned_per_object") if owned and info.get("owned_per_object") else info.get("per_object")
    if not key:
        raise GuardError(f"tool has no pricing rule: {tool}")
    try:
        return float(cfg["pricing"]["event"][key]), "object"
    except KeyError as exc:
        raise GuardError(f"missing event price: {key}") from exc


def estimate_call(cfg: dict, tool: str, n: int, owned: bool = False) -> float:
    price, _kind = unit_price(cfg, tool, owned)
    return round(price * max(int(n), 0), 6)


def parse_call(spec: str) -> tuple[str, int, bool]:
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise GuardError(f"invalid call format, expected TOOL:N[:owned]: {spec}")
    try:
        n = int(parts[1])
    except ValueError as exc:
        raise GuardError(f"call count must be an integer: {spec}") from exc
    if n < 0:
        raise GuardError(f"call count must be non-negative: {spec}")
    owned = len(parts) == 3 and parts[2].lower() == "owned"
    if len(parts) == 3 and not owned:
        raise GuardError(f"unknown call modifier: {parts[2]}")
    return parts[0], n, owned


def estimate_calls(cfg: dict, calls: list[str]) -> float:
    return round(sum(estimate_call(cfg, *parse_call(spec)) for spec in calls), 6)


def current_phase(cfg: dict, at: datetime) -> dict | None:
    tz = tz_for(cfg)
    for phase in cfg["phases"]:
        if parse_ts(phase["start"], tz) <= at < parse_ts(phase["end"], tz):
            return phase
    return None


def period_bounds(cfg: dict, phase: dict, at: datetime) -> tuple[datetime, datetime]:
    tz = tz_for(cfg)
    phase_start = parse_ts(phase["start"], tz).astimezone(tz)
    phase_end = parse_ts(phase["end"], tz).astimezone(tz)
    if phase.get("period") == "month":
        local = at.astimezone(tz)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        return max(start, phase_start), min(end, phase_end)
    return phase_start, phase_end


def period_label(phase: dict) -> str:
    """Human label for the cap window: 'monthly' for period=month, else 'phase'."""
    return "monthly" if phase.get("period") == "month" else "phase"


def phase_cap(phase: dict) -> float:
    return float(phase.get("cap_usd", 0)) + float(phase.get("oneoff_approved_usd", 0))


def routine_cap(phase: dict, routine: str) -> dict:
    config = dict(phase.get("routines", {}).get(routine, {}))
    if routine == "oneoff-research":
        config["period_usd"] = float(phase.get("oneoff_approved_usd", 0))
    return config


def spends(cfg: dict, ledger: list[dict], start: datetime, end: datetime, routine: str | None = None) -> float:
    tz = tz_for(cfg)
    total = 0.0
    for row in ledger:
        if row.get("kind") != "spend":
            continue
        ts = parse_ts(row["ts"], tz)
        if start <= ts < end and (routine is None or row.get("routine") == routine):
            total += float(row.get("cost_usd", 0))
    return round(total, 6)


def latest_balance(ledger: list[dict]) -> dict | None:
    balances = [row for row in ledger if row.get("kind") == "balance"]
    return balances[-1] if balances else None


def level_for(ratio: float, thresholds: dict) -> str:
    if ratio >= float(thresholds["stop"]):
        return "stop"
    if ratio >= float(thresholds["degrade"]):
        return "degrade"
    if ratio >= float(thresholds["warn"]):
        return "warn"
    return "ok"


def paid_alarm(cfg: dict, ledger: list[dict]) -> bool:
    alarm = False
    for row in ledger:
        if row.get("kind") == "balance" and row.get("paid_touched"):
            alarm = True
        if row.get("kind") == "ack-paid":
            alarm = False
    return alarm


def free_left(cfg: dict, ledger: list[dict]) -> float | None:
    bal = latest_balance(ledger)
    if not bal:
        return None
    tz = tz_for(cfg)
    since = parse_ts(bal["ts"], tz)
    spent_after = sum(
        float(row.get("cost_usd", 0))
        for row in ledger
        if row.get("kind") == "spend" and parse_ts(row["ts"], tz) > since
    )
    reported = float(bal.get("free_balance", 0))
    by_total = float(bal.get("total_balance", 0)) - float(cfg.get("paid_baseline_usd", 0))
    return round(min(reported, by_total) - spent_after, 6)


def check_budget(cfg: dict, ledger: list[dict], routine: str, estimate_usd: float, at: datetime | None = None) -> dict:
    at = at or now_for(cfg)
    result = {
        "routine": routine,
        "level": "ok",
        "cap": None,
        "spent": None,
        "remaining": None,
        "estimate_usd": round(estimate_usd, 6),
        "allowed": True,
        "reason": "within budget",
        "reasons": [],
    }
    phase = current_phase(cfg, at)
    if phase is None:
        result.update(level="stop", allowed=False, reason="no active budget phase")
        result["reasons"].append(result["reason"])
        return result

    start, end = period_bounds(cfg, phase, at)
    cap = phase_cap(phase)
    spent_total = spends(cfg, ledger, start, end)
    spent_routine = spends(cfg, ledger, start, end, routine)
    rc = routine_cap(phase, routine)
    routine_period = rc.get("period_usd")
    remaining = None if routine_period is None else round(float(routine_period) - spent_routine, 6)
    ratio_after = (spent_total + estimate_usd) / cap if cap > 0 else 1.0
    level = level_for(ratio_after, cfg["thresholds"])

    result.update(
        phase=phase["name"],
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        cap=cap,
        spent=spent_total,
        remaining=round(cap - spent_total, 6),
        routine_cap=routine_period,
        routine_spent=spent_routine,
        routine_remaining=remaining,
        level=level,
        usage_ratio_after=round(ratio_after, 4),
    )

    if paid_alarm(cfg, ledger) and cfg.get("paid_policy", "stop") == "stop":
        result.update(level="stop", allowed=False)
        result["reasons"].append("paid balance dropped below baseline")

    if estimate_usd > 0 and not rc:
        result["allowed"] = False
        result["reasons"].append(f"routine has no configured cap: {routine}")
    if rc:
        per_run = float(rc.get("per_run_usd", 0))
        period = float(rc.get("period_usd", 0))
        if estimate_usd > per_run + 1e-9:
            result["allowed"] = False
            result["reasons"].append(f"estimate exceeds per-run cap ${per_run:.3f}")
        if spent_routine + estimate_usd > period + 1e-9:
            result["allowed"] = False
            result["reasons"].append(
                f"routine {period_label(phase)} cap reached: ${spent_routine:.3f} spent of ${period:.3f}"
            )

    if level == "stop":
        result["allowed"] = False
        result["reasons"].append(f"{period_label(phase)} total cap reached: ${spent_total:.3f} spent of ${cap:.3f}")

    left = free_left(cfg, ledger)
    result["free_left_usd"] = left
    if left is not None and estimate_usd > left - float(cfg.get("safety_margin_usd", 0)) + 1e-9:
        result.update(level="stop", allowed=False)
        result["reasons"].append("estimate would cross free balance safety margin")

    bal = latest_balance(ledger)
    if bal:
        age_hours = (at - parse_ts(bal["ts"], tz_for(cfg))).total_seconds() / 3600
        result["balance_age_hours"] = round(age_hours, 2)
        if age_hours > float(cfg.get("balance_max_age_hours", 36)) and result["allowed"]:
            result["level"] = "degrade"
            result["reasons"].append("balance snapshot is stale")

    if not result["reasons"]:
        result["reasons"].append("within budget")
    result["reason"] = "; ".join(result["reasons"])
    return result


def exit_code_for(result: dict) -> int:
    if not result.get("allowed"):
        return EXIT_DENY
    if result.get("level") == "degrade":
        return EXIT_DEGRADE
    return EXIT_OK


def balance_row(cfg: dict, ledger: list[dict], free: float, paid: float, at: datetime) -> dict:
    total = free + paid
    row = {
        "ts": at.isoformat(timespec="seconds"),
        "kind": "balance",
        "free_balance": round(float(free), 6),
        "paid_balance": round(float(paid), 6),
        "total_balance": round(float(total), 6),
    }
    baseline = float(cfg.get("paid_baseline_usd", 0))
    row["paid_touched"] = row["paid_balance"] < baseline - 0.0049
    prev = latest_balance(ledger)
    if prev:
        row["delta_free"] = round(row["free_balance"] - float(prev.get("free_balance", 0)), 6)
        row["delta_paid"] = round(row["paid_balance"] - float(prev.get("paid_balance", 0)), 6)
        paid_charged = row["delta_paid"] < -0.0049
        free_charged = row["delta_free"] < -0.0049
        row["charged_bucket"] = "both" if free_charged and paid_charged else "free" if free_charged else "paid" if paid_charged else "none"
    return row


def status(cfg: dict, ledger: list[dict], at: datetime | None = None) -> dict:
    at = at or now_for(cfg)
    phase = current_phase(cfg, at)
    out = {"at": at.isoformat(timespec="seconds"), "phase": phase["name"] if phase else None}
    if phase:
        start, end = period_bounds(cfg, phase, at)
        cap = phase_cap(phase)
        spent = spends(cfg, ledger, start, end)
        out.update(
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            cap_usd=cap,
            spent_usd=spent,
            remaining_usd=round(cap - spent, 6),
            level=level_for(spent / cap, cfg["thresholds"]) if cap > 0 else "stop",
            routines={
                name: {
                    "spent_usd": spends(cfg, ledger, start, end, name),
                    "period_usd": routine_cap(phase, name).get("period_usd"),
                    "per_run_usd": routine_cap(phase, name).get("per_run_usd"),
                }
                for name in phase.get("routines", {})
            },
        )
    bal = latest_balance(ledger)
    out["latest_balance"] = {
        key: bal.get(key)
        for key in ("ts", "free_balance", "paid_balance", "total_balance", "charged_bucket")
    } if bal else None
    out["free_left_usd"] = free_left(cfg, ledger)
    out["paid_alarm"] = paid_alarm(cfg, ledger)
    return out


def format_status(st: dict) -> str:
    lines = [f"API budget at {st['at']} phase={st.get('phase') or 'none'}"]
    if st.get("phase"):
        lines.append(
            f"Period {st['period_start'][:10]} to {st['period_end'][:10]}: "
            f"${st['spent_usd']:.3f} / ${st['cap_usd']:.2f} ({st['level']})"
        )
        for name, item in st["routines"].items():
            lines.append(
                f"- {name}: ${item['spent_usd']:.3f} / ${float(item['period_usd'] or 0):.3f} "
                f"(per run ${float(item['per_run_usd'] or 0):.3f})"
            )
    bal = st.get("latest_balance")
    if bal:
        lines.append(
            f"Balance {bal['ts']}: free ${bal['free_balance']:.2f}, "
            f"paid ${bal['paid_balance']:.2f}, total ${bal['total_balance']:.2f}"
        )
    else:
        lines.append("No balance snapshot recorded.")
    if st.get("free_left_usd") is not None:
        lines.append(f"Estimated free balance left: ${st['free_left_usd']:.2f}")
    if st.get("paid_alarm"):
        lines.append("Paid-balance alarm is active; all calls are denied.")
    return "\n".join(lines)


def init_files(config: Path, ledger: Path, preset: str) -> int:
    if config.exists():
        return fail(f"config file already exists: {config}")
    if ledger.exists():
        return fail(f"ledger file already exists: {ledger}")
    source = resources.files("api_budget_guard").joinpath("presets", f"{preset}.json")
    config.parent.mkdir(parents=True, exist_ok=True)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with resources.as_file(source) as preset_path:
        shutil.copyfile(preset_path, config)
    ledger.write_text("", encoding="utf-8")
    print(f"Created {config}")
    print(f"Created {ledger}")
    return EXIT_OK


def command_run(args: argparse.Namespace, cfg: dict, ledger: list[dict], ledger_path: Path) -> int:
    estimate = estimate_calls(cfg, args.call)
    result = check_budget(cfg, ledger, args.routine, estimate)
    code = exit_code_for(result)
    if code == EXIT_DENY:
        print(f"[deny] {args.routine}: {result['reason']}", file=sys.stderr)
        return EXIT_DENY
    level = "degrade" if code == EXIT_DEGRADE else "ok"
    env = os.environ.copy()
    env["BUDGET_LEVEL"] = level
    completed = subprocess.run(args.command, env=env)
    if completed.returncode != 0:
        return completed.returncode
    row = {
        "ts": now_for(cfg).isoformat(timespec="seconds"),
        "kind": "spend",
        "routine": args.routine,
        "tool": "estimate",
        "objects": 0,
        "owned": False,
        "cost_usd": estimate,
        "note": "recorded by run wrapper",
    }
    append_ledger(ledger_path, row)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="api-budget-guard", description="Offline guardrail for metered API budgets.")
    parser.add_argument("--config", default="budget.json")
    parser.add_argument("--ledger", default="ledger.jsonl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--preset", choices=["x-api", "generic"], default="generic")

    p = sub.add_parser("estimate")
    p.add_argument("--call", action="append", required=True)

    p = sub.add_parser("check")
    p.add_argument("--routine", required=True)
    p.add_argument("--call", action="append", default=[])
    p.add_argument("--usd", type=float)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("record")
    p.add_argument("--routine", required=True)
    p.add_argument("--tool", required=True)
    amount = p.add_mutually_exclusive_group(required=True)
    amount.add_argument("--objects", type=int)
    amount.add_argument("--usd", type=float)
    p.add_argument("--owned", action="store_true")
    p.add_argument("--note", default="")

    p = sub.add_parser("balance")
    p.add_argument("--usd", type=float, required=True)
    p.add_argument("--free", type=float)
    p.add_argument("--paid", type=float)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run")
    p.add_argument("--routine", required=True)
    p.add_argument("--call", action="append", required=True)
    p.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    ledger_path = Path(args.ledger)

    if args.cmd == "init":
        return init_files(config_path, ledger_path, args.preset)

    try:
        cfg = load_config(config_path)
        ledger = load_ledger(ledger_path)
        at = now_for(cfg)

        if args.cmd == "estimate":
            total = 0.0
            for spec in args.call:
                tool, n, owned = parse_call(spec)
                cost = estimate_call(cfg, tool, n, owned)
                total += cost
                suffix = " owned" if owned else ""
                print(f"{tool}:{n}{suffix} ${cost:.3f}")
            print(f"Total ${total:.3f}")
            return EXIT_OK

        if args.cmd == "check":
            if args.usd is None and not args.call:
                raise GuardError("check requires --call or --usd")
            estimate = usd_amount(args.usd, "--usd") if args.usd is not None else estimate_calls(cfg, args.call)
            result = check_budget(cfg, ledger, args.routine, estimate, at)
            if args.json:
                print(json.dumps({
                    "level": result["level"],
                    "cap": result["routine_cap"],
                    "spent": result["routine_spent"],
                    "remaining": result["routine_remaining"],
                    "reason": result["reason"],
                }, indent=2, sort_keys=True))
            else:
                label = "go" if exit_code_for(result) == EXIT_OK else result["level"] if result["allowed"] else "deny"
                cap = result.get("routine_cap")
                spent = result.get("routine_spent")
                cap_text = f" of ${float(cap):.2f}" if cap is not None else ""
                spent_text = f"; spent ${float(spent):.3f}{cap_text}" if spent is not None else ""
                print(f"[{label}] {args.routine}: estimate ${estimate:.3f}{spent_text}; {result['reason']}")
            return exit_code_for(result)

        if args.cmd == "record":
            if args.usd is not None:
                cost = round(usd_amount(args.usd, "--usd"), 6)
                objects = 0
            else:
                objects = args.objects or 0
                if objects < 0:
                    raise GuardError(f"--objects must be non-negative: {objects}")
                cost = estimate_call(cfg, args.tool, objects, args.owned)
            row = {
                "ts": at.isoformat(timespec="seconds"),
                "kind": "spend",
                "routine": args.routine,
                "tool": args.tool,
                "objects": objects,
                "owned": args.owned,
                "cost_usd": cost,
            }
            if args.note:
                row["note"] = args.note
            append_ledger(ledger_path, row)
            print(f"Recorded {args.routine} {args.tool}: ${cost:.3f}")
            return EXIT_OK

        if args.cmd == "balance":
            usd = usd_amount(args.usd, "--usd")
            free = usd_amount(args.free, "--free") if args.free is not None else usd
            paid = usd_amount(args.paid, "--paid") if args.paid is not None else max(usd - free, 0)
            row = balance_row(cfg, ledger, free, paid, at)
            append_ledger(ledger_path, row)
            print(f"Balance recorded: free ${free:.2f}, paid ${paid:.2f}, total ${free + paid:.2f}")
            if row["paid_touched"]:
                print("Denied: paid balance dropped below baseline.", file=sys.stderr)
                return EXIT_DENY
            return EXIT_OK

        if args.cmd == "status":
            st = status(cfg, ledger, at)
            print(json.dumps(st, indent=2, sort_keys=True) if args.json else format_status(st))
            return EXIT_OK

        if args.cmd == "run":
            command = args.command
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                return fail("run requires a command after --")
            args.command = command
            return command_run(args, cfg, ledger, ledger_path)

    except GuardError as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail(str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        return fail(f"invalid value in config or ledger: {exc!r}")

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
