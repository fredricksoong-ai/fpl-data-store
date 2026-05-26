# 2025-2026 Season — GW Snapshots

Per-gameweek pipeline outputs for backtesting.

Snapshots are generated on demand by running:
  python pipeline.py --mode=backtest --gw=N

Each subfolder contains the full pipeline output as it would have
looked at that gameweek's deadline.

## Structure

snapshots/
  GW01/   ← pipeline output at GW1 deadline
  GW10/   ← pipeline output at GW10 deadline
  GW20/   ← pipeline output at GW20 deadline
  GW38/   ← pipeline output at GW38 (same as final/)

## Suggested backtest GWs to run first

- GW10  — early season, test autoflag cold start
- GW20  — mid season, test form signals
- GW32  — wildcard week, test decision audit trail
- GW38  — full season, validate against known outcomes
