# New Season Checklist — 2026/27

Run this checklist when CI publishes `data/2026-2027/` (expected Aug 2026).

---

## Step 1 — Update pipeline_config.json

- [ ] `season.current` → `"2026-2027"`
- [ ] `core_insights.season` → `"2026-2027"`
- [ ] `output_paths.season_final` → `"seasons/2026-2027/final/"`
- [ ] `output_paths.snapshots` → `"seasons/2026-2027/snapshots/"`
- [ ] `season.mode` → `"preseason"` (then `"live"` at GW1 deadline)
- [ ] Review `rival_entry_ids` — any new managers in Ballon d'FPL?

## Step 2 — Update meta/season_state.json

- [ ] `current_season` → `"2026-2027"`
- [ ] `current_gw` → `1`
- [ ] `previous_gw` → `null`
- [ ] `mode` → `"preseason"` then `"live"`
- [ ] `last_pipeline_run` → `null`

## Step 3 — Carry over watchlist

- [ ] Open `pipeline_config.json` watchlist section
- [ ] Remove players who left the league (check FPL bootstrap)
- [ ] Keep players who survived the transfer window
- [ ] Add any known summer signings worth tracking early

## Step 4 — Archive last season

- [ ] Confirm `seasons/2025-2026/final/` is complete (all 10 files present)
- [ ] Create `seasons/2026-2027/final/` and `seasons/2026-2027/snapshots/` folders

## Step 5 — First pipeline run

- [ ] Run `python pipeline.py --mode=preseason`
- [ ] Verify `live/player_scan.json` reflects new season prices
- [ ] Verify `live/gw_meta.json` shows GW1 deadline
- [ ] Spot-check 3 players — name, team, price all correct?

## Step 6 — Validate autoflag thresholds

- [ ] At preseason, form/xGI flags won't fire (no data yet) — expected
- [ ] Confirm price-based flags (value_gem) still fire correctly
- [ ] Adjust `auto_flags` thresholds if needed based on new season price inflation

---

## Known Data Behaviours at Season Start

- `element-summary/{id}/history` will be empty until GW1 completes
- `player_history.json` will have 0 GW entries — normal
- CI will not have `playerstats.csv` data until first GW snapshot
- `live_gw.json` should not be fetched until GW1 kicks off
- `form_last5` will be 0 for all players — expected
