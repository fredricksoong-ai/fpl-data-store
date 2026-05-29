"""
Run from inside fpl-data-store/.
Updates pipeline_config.json and pipeline.py with all improved autoflag logic.

Flags refined:
  corner_taker   — primary taker only + xgi filter
  penalty_taker  — minutes filter added
  xgi_elite      — threshold raised 0.55 -> 0.65

New flags:
  xgi_value      — strong xGI + affordable price (underlying gems)
  cbit_strong    — CBIT/90 for DEF/MID (defensive contribution)
  gk_shot_stopper — saves/90 for GKs at shot-heavy teams
  price_rising   — sustained price appreciation since season start
  rotation_risk  — negative signal: low avg minutes (avoid flag)
  cs_candidate   — DEF/GK with 10+ clean sheets (solid defensive team)

Usage: python3 apply_pipeline_updates.py
"""
import json

# ─────────────────────────────────────────────────────────
# 1. pipeline_config.json
# ─────────────────────────────────────────────────────────
print('1. Updating pipeline_config.json...')
with open('pipeline_config.json') as f:
    config = json.load(f)

config['auto_flags'] = {
    # Existing — refined
    'form_top_n': 20,
    'ep_next_top_n_per_position': 8,
    'transfers_in_event_min': 80000,
    'ownership_delta_min': 1.5,
    'value_gem_max_price': 6.0,
    'value_gem_min_form': 5.0,
    'xgi_per90_min': 0.65,
    'xgi_min_minutes': 450,
    'penalty_taker_order': 1,
    'penalty_taker_min_minutes': 600,
    'corner_taker_max_order': 1,
    'corner_taker_min_xgi_per90': 0.15,
    # New — xgi_value
    'xgi_value_max_price': 7.0,
    'xgi_value_min_xgi_per90': 0.55,
    # New — cbit_strong
    'cbit_per90_min': 10.0,
    'cbit_min_minutes': 450,
    # New — gk_shot_stopper
    'gk_saves_per90_min': 3.0,
    'gk_min_minutes': 450,
    # New — price_rising
    'price_rising_min_change': 3,
    'price_rising_min_form': 4.0,
    'price_rising_min_minutes': 450,
    # New — rotation_risk (negative signal — avg mins per GW)
    'rotation_risk_max_avg_mins': 55,
    'rotation_risk_min_total_mins': 900,
    # New — cs_candidate
    'cs_candidate_min_clean_sheets': 10,
    'cs_candidate_min_minutes': 1800
}

with open('pipeline_config.json', 'w') as f:
    json.dump(config, f, indent=2)
print('   ✅ Done')

# ─────────────────────────────────────────────────────────
# 2. pipeline.py
# ─────────────────────────────────────────────────────────
print('2. Updating pipeline.py...')
with open('pipeline.py') as f:
    code = f.read()

changes = 0

# ── 2a. Add new fields to candidates dict ────────────────
old_cands_end = (
    "            'penalties_order': safe_float(ci.get('penalties_order')),\n"
    "            'corners_order': safe_float(ci.get('corners_and_indirect_freekicks_order')),\n"
    "        })"
)
new_cands_end = (
    "            'penalties_order': safe_float(ci.get('penalties_order')),\n"
    "            'corners_order': safe_float(ci.get('corners_and_indirect_freekicks_order')),\n"
    "            # CI defensive metric (= (CBI + Tackles) / (mins/90))\n"
    "            'cbit_per90': safe_float(ci.get('defensive_contribution_per_90'), 0),\n"
    "            # FPL bootstrap fields\n"
    "            'saves': safe_int(e.get('saves'), 0),\n"
    "            'cost_change_start': safe_int(e.get('cost_change_start'), 0),\n"
    "            'clean_sheets': safe_int(e.get('clean_sheets'), 0),\n"
    "            'avg_mins_per_gw': round(safe_int(e.get('minutes'), 0) / 38, 1),\n"
    "        })"
)
if old_cands_end in code:
    code = code.replace(old_cands_end, new_cands_end)
    print('   ✅ New fields added to candidates dict')
    changes += 1
else:
    print('   ⚠️  candidates dict end block not found')

# ── 2b. Tighten corner_taker ─────────────────────────────
old_corner = (
    "        if p['corners_order'] is not None and p['corners_order'] <= t['corner_taker_max_order']:\n"
    "            add_flag(p['id'], 'corner_taker')"
)
new_corner = (
    "        if (p['corners_order'] is not None\n"
    "                and p['corners_order'] <= t['corner_taker_max_order']\n"
    "                and p['xgi_per90'] >= t.get('corner_taker_min_xgi_per90', 0)):\n"
    "            add_flag(p['id'], 'corner_taker')"
)
if old_corner in code:
    code = code.replace(old_corner, new_corner)
    print('   ✅ corner_taker xgi filter added')
    changes += 1
else:
    print('   ⚠️  corner_taker block not found')

# ── 2c. Add minutes filter to penalty_taker ──────────────
old_pen = (
    "        if p['penalties_order'] is not None and p['penalties_order'] <= t['penalty_taker_order']:\n"
    "            add_flag(p['id'], 'penalty_taker')"
)
new_pen = (
    "        if (p['penalties_order'] is not None\n"
    "                and p['penalties_order'] <= t['penalty_taker_order']\n"
    "                and p['minutes'] >= t.get('penalty_taker_min_minutes', 0)):\n"
    "            add_flag(p['id'], 'penalty_taker')"
)
if old_pen in code:
    code = code.replace(old_pen, new_pen)
    print('   ✅ penalty_taker minutes filter added')
    changes += 1
else:
    print('   ⚠️  penalty_taker block not found')

# ── 2d. Replace xgi_elite block with all new flags ───────
old_xgi = (
    "    # xGI elite: high involvement per 90, enough minutes\n"
    "    for p in candidates:\n"
    "        if (p['xgi_per90'] >= t['xgi_per90_min']\n"
    "                and p['minutes'] >= t['xgi_min_minutes']):\n"
    "            add_flag(p['id'], 'xgi_elite')"
)
new_flags = (
    "    # xgi_elite: high xGI per 90, enough minutes\n"
    "    for p in candidates:\n"
    "        if (p['xgi_per90'] >= t['xgi_per90_min']\n"
    "                and p['minutes'] >= t['xgi_min_minutes']):\n"
    "            add_flag(p['id'], 'xgi_elite')\n"
    "\n"
    "    # xgi_value: strong underlying numbers + affordable price\n"
    "    # Catches players with elite xGI not yet premium-priced\n"
    "    for p in candidates:\n"
    "        if (p['xgi_per90'] >= t.get('xgi_value_min_xgi_per90', 0.55)\n"
    "                and p['price'] <= t.get('xgi_value_max_price', 7.0)\n"
    "                and p['minutes'] >= t['xgi_min_minutes']):\n"
    "            add_flag(p['id'], 'xgi_value')\n"
    "\n"
    "    # cbit_strong: high defensive contribution for DEF/MID\n"
    "    # Uses CI defensive_contribution_per_90 = (CBI + Tackles) / (mins/90)\n"
    "    for p in candidates:\n"
    "        if (p['position'] in (2, 3)\n"
    "                and p['cbit_per90'] >= t.get('cbit_per90_min', 10.0)\n"
    "                and p['minutes'] >= t.get('cbit_min_minutes', 450)):\n"
    "            add_flag(p['id'], 'cbit_strong')\n"
    "\n"
    "    # gk_shot_stopper: GKs with high saves per 90\n"
    "    # High saves = points (1 per 3) + BPS + bonus at shot-heavy teams\n"
    "    for p in candidates:\n"
    "        saves_per90 = (p['saves'] / p['minutes'] * 90) if p['minutes'] > 0 else 0\n"
    "        if (p['position'] == 1\n"
    "                and saves_per90 >= t.get('gk_saves_per90_min', 3.0)\n"
    "                and p['minutes'] >= t.get('gk_min_minutes', 450)):\n"
    "            add_flag(p['id'], 'gk_shot_stopper')\n"
    "\n"
    "    # price_rising: sustained price appreciation since season start\n"
    "    # Signals FPL popularity + form momentum (live season only)\n"
    "    for p in candidates:\n"
    "        if (p['cost_change_start'] >= t.get('price_rising_min_change', 3)\n"
    "                and p['fpl_form'] >= t.get('price_rising_min_form', 4.0)\n"
    "                and p['minutes'] >= t.get('price_rising_min_minutes', 450)):\n"
    "            add_flag(p['id'], 'price_rising')\n"
    "\n"
    "    # rotation_risk: negative signal — low avg minutes per GW\n"
    "    # Surfaces players with rotation concerns for the dashboard to highlight\n"
    "    for p in candidates:\n"
    "        if (p['avg_mins_per_gw'] <= t.get('rotation_risk_max_avg_mins', 55)\n"
    "                and p['minutes'] >= t.get('rotation_risk_min_total_mins', 900)):\n"
    "            add_flag(p['id'], 'rotation_risk')\n"
    "\n"
    "    # cs_candidate: DEF/GK at teams with strong clean sheet record\n"
    "    # Targets players at defensively solid teams for structural point floor\n"
    "    for p in candidates:\n"
    "        if (p['position'] in (1, 2)\n"
    "                and p['clean_sheets'] >= t.get('cs_candidate_min_clean_sheets', 10)\n"
    "                and p['minutes'] >= t.get('cs_candidate_min_minutes', 1800)):\n"
    "            add_flag(p['id'], 'cs_candidate')"
)
if old_xgi in code:
    code = code.replace(old_xgi, new_flags)
    print('   ✅ xgi_value added')
    print('   ✅ cbit_strong added')
    print('   ✅ gk_shot_stopper added')
    print('   ✅ price_rising added')
    print('   ✅ rotation_risk added')
    print('   ✅ cs_candidate added')
    changes += 1
else:
    print('   ⚠️  xgi_elite block not found')

with open('pipeline.py', 'w') as f:
    f.write(code)

print()
print(f'✅ {changes}/4 change blocks applied successfully')
print()
print('Next steps:')
print('  python3 pipeline.py --mode=backtest --gw=10')
print('  python3 pipeline.py --mode=backtest --gw=20')
print('  python3 pipeline.py --mode=backtest --gw=30')
print('  git add . && git commit -m "9 autoflags: CBIT, xgi_value, GK saves, price, rotation, CS" && git push')
