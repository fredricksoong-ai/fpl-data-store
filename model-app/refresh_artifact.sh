#!/usr/bin/env bash
# refresh_artifact.sh — regenerate FPLanner feeds and assemble the Cowork artifact HTML.
#
# Replaces the retired Cloudflare/GitHub-Actions pipeline. Runs the same Python builders against live
# FPL data (recent-seasons model fit, plus the Coventry/Hull-era files so promoted clubs are modelled),
# keeps the last-known feed for any builder that fails, then bakes everything into a self-contained
# HTML via build_artifact.py. The caller publishes the result with the update_artifact tool.
#
# Usage: bash refresh_artifact.sh [out_html]   (default: ./fplanner.html)
#   arena.json is NOT regenerated here — it's the weekly fpl-arena roll's output; whatever is in
#   site/arena.json is carried through.
set -uo pipefail
MA="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$MA/fplanner.html}"
python3 -c "import scipy" 2>/dev/null || pip install scipy --break-system-packages -q
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/data/pl" "$WORK/sitefeed"
ln -s "$MA/engine" "$WORK/engine"
cp "$MA/data/Fixtures-2026-27.csv" "$WORK/data/" 2>/dev/null || true
for s in 2021 2122 2223 2324 2425 2526 0001 9900 1617; do cp "$MA/data/pl/E0_$s.csv" "$WORK/data/pl/" 2>/dev/null || true; done
cp "$MA"/build_*.py "$MA/site/player_enrich.json" "$WORK/"
cp "$MA/site"/*.json "$WORK/sitefeed/" 2>/dev/null || true   # last-known fallback per feed
cd "$WORK"
V2_OUT=sitefeed/v2.json                              python3 build_v2.py        2>&1 | tail -1 || true
ENRICH_PATH=player_enrich.json PLAYERS_OUT=sitefeed/players.json python3 build_players.py 2>&1 | tail -1 || true
REC_OUT=sitefeed/recommend.json PLAYERS_OUT=sitefeed/players.json python3 build_recommend.py 2>&1 | tail -1 || true
XG_OUT=sitefeed/xg.json                              python3 build_xg.py        2>&1 | tail -1 || true
FIXTURES_OUT=sitefeed/fixtures.json                  python3 build_fixtures.py  2>&1 | tail -1 || true
CHIPS_OUT=sitefeed/chips.json                        python3 build_chips.py     2>&1 | tail -1 || true
SQUAD_OUT=sitefeed/squad.json                        python3 build_squad.py     2>&1 | tail -1 || true
cp "$MA/site/index.html" sitefeed/
python3 build_artifact.py sitefeed "$OUT"
