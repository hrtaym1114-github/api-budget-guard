# api-budget-guard

`api-budget-guard` is a small offline CLI brake for scheduled agents that call metered APIs. It estimates a run before it happens, checks routine and monthly caps, records actual spend to a JSONL ledger, and can wrap commands so unattended jobs stop before touching a hard budget.

It uses only the Python standard library and never calls provider APIs.

## Install

```bash
pipx install git+https://github.com/hrtaym1114-github/api-budget-guard.git
```

For local development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## 30s Demo

[![go vs deny](docs/media/api-budget-guard-strip.png)](docs/media/api-budget-guard-howto.gif)

- 🎞️ [GIF (GO ↔ DENY)](docs/media/api-budget-guard-howto.gif) · Release: [v0.1.0](https://github.com/hrtaym1114-github/api-budget-guard/releases/tag/v0.1.0)

```bash
api-budget-guard init --preset x-api
api-budget-guard check --routine keyword-watch --call search_posts_all:10
for i in $(seq 20); do
  api-budget-guard record --routine keyword-watch --tool search_posts_all --objects 10
done
api-budget-guard check --routine keyword-watch --call search_posts_all:10
api-budget-guard status
```

The first check is a go decision: `search_posts_all:10` costs `$0.05` against the example `keyword-watch` cap of `$0.80`. After the loop, the same check exits `20` because the routine cap is reached.

## Commands

```bash
api-budget-guard init --preset x-api
api-budget-guard estimate --call TOOL:N[:owned]
api-budget-guard check --routine NAME --call TOOL:N [--json]
api-budget-guard record --routine NAME --tool TOOL --objects N
api-budget-guard record --routine NAME --tool TOOL --usd X
api-budget-guard balance --usd X [--free X --paid X]
api-budget-guard status [--json]
api-budget-guard run --routine NAME --call TOOL:N -- command arg...
```

Global options:

```bash
api-budget-guard --config ./budget.json --ledger ./ledger.jsonl status
```

Defaults are `./budget.json` and `./ledger.jsonl`.

## Exit Codes

- `0`: go
- `10`: degrade, run a smaller or cheaper path
- `20`: deny, fail closed

`check --json` returns `level`, `cap`, `spent`, `remaining`, and `reason`.

## Config Reference

The config file is JSON:

- `timezone`: IANA timezone used for phases and monthly periods.
- `pricing.event`: per-object prices.
- `pricing.request`: per-request prices.
- `tools`: maps tool names to `per_object`, `owned_per_object`, `per_request`, or `free`.
- `thresholds`: usage ratios for `warn`, `degrade`, and `stop`.
- `safety_margin_usd`: free-balance margin that must remain untouched.
- `balance_max_age_hours`: stale balance snapshots degrade checks.
- `paid_baseline_usd`: paid or prepaid balance that should not drop.
- `paid_policy`: `stop` denies all calls once the paid baseline drops.
- `phases`: dated budget windows with `period` set to `month` or `phase`.
- `phases[].routines`: per-routine `per_run_usd` and `period_usd` caps.

The bundled `x-api` preset contains example prices as of 2026-10. Verify prices with the provider before using it for real operations.

## Wrapper Mode

```bash
api-budget-guard run --routine keyword-watch --call search_posts_all:10 -- ./job.sh
```

The wrapper checks first. It runs the command only for exit `0` or `10`, exports `BUDGET_LEVEL=ok` or `BUDGET_LEVEL=degrade`, and records the estimate after a successful command.

## Fail-Closed Behavior

Missing or corrupt config and ledger files exit `20`. Unknown tools, invalid call specs, inactive phases, routine cap overruns, period cap overruns, and paid-balance alarms also exit `20`.

## Non-Goals

- Calling provider APIs or scraping pricing.
- Dashboards, SaaS, billing integrations, or hosted storage.
- LLM features.
- Replacing provider billing alerts.

## 日本語メモ

`api-budget-guard` は、夜間や定期実行のエージェントが従量課金 API を使いすぎないためのローカル CLI です。設定ファイルの単価と上限だけで判定し、ネットワークには接続しません。`check` で実行前に止め、`record` で実績を JSONL に残し、`run` で既存ジョブを包めます。
