# Changelog

## 0.1.2 - 2026-10-25

Hardening / bugfix release (JP + EN).

- `check` without `--call` or `--usd` now denies (breaking change from v0.1.1).
- `run` records its estimate even when the wrapped command fails.
- `balance --usd X --paid Y` derives free balance as `X - Y` when `--free` is omitted.
- `check --json` always includes `allowed` and filled keys; denied checks report `stop` rather than `ok`.
- Ledger loading rejects negative `cost_usd` values.
- Malformed config, ledger, or clock values deny with a clear message (must-fix H3/H4/H5/M2/H2).
- Reject negative and non-finite amounts.
- Docs: README aligned with actual CLI behavior; expanded edge-case tests.

## 0.1.1

- Deny message reflects cap period.
