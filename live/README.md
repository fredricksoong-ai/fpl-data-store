# /live

This folder is the single source of truth for all skills and dashboards.

Files here are always overwritten by the pipeline on each run.
Never edit these files manually.

## Files (populated by pipeline)

| File | Updated by | Frequency |
|---|---|---|
| player_scan.json | Pipeline | Twice daily |
| player_universe.json | Pipeline | Twice daily |
| player_history.json | Pipeline | On demand |
| manager_history.json | Pipeline | Post-GW |
| season_decisions.json | Pipeline | Post-GW |
| fixtures.json | Pipeline | Weekly |
| league_table.json | Pipeline | Post-GW |
| my_squad.json | Pipeline | Pre-deadline |
| rival_squads.json | Pipeline | Pre-deadline |
| live_gw.json | Pipeline | Matchday |
| gw_meta.json | Pipeline | Twice daily |

## Current state

See `meta/season_state.json` for current mode and GW.
