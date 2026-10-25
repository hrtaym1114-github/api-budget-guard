# Changelog

## Unreleased / v0.1.2-pre

- `check` without `--call` or `--usd` now denies (breaking change from v0.1.1).
- `run` records its estimate even when the wrapped command fails.
- `balance --usd X --paid Y` derives free balance as `X - Y` when `--free` is omitted.
- `check --json` always includes `allowed` and filled keys; denied checks report `stop` rather than `ok`.
- Ledger loading rejects negative `cost_usd` values.
