"""
FPL Data Pipeline
Fetches, transforms and writes all data files for skills and dashboards.

Usage:
  python pipeline.py --mode=offseason          # full season archive
  python pipeline.py --mode=live               # current GW
  python pipeline.py --mode=backtest --gw=20   # replay any GW
  python pipeline.py --mode=preseason          # new season setup

Requirements:
  pip install requests
"""

import json
import csv
import io
import os
import sys
import argparse
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

FPL_BASE = 'https://fantasy.premierleague.com/api'
HEADERS = {'User-Agent': 'Mozilla/5.0'}


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_config(path='pipeline_config.json'):
    with open(path) as f:
        return json.load(f)

def safe_float(val, default=None):
    try:
        return float(val) if val not in (None, '', 'None') else default
    except (ValueError, TypeError):
        return default

def safe_int(val, default=None):
    try:
        return int(float(val)) if val not in (None, '', 'None') else default
    except (ValueError, TypeError):
        return default

def to_sgt(iso_str):
    """Convert ISO UTC timestamp to Singapore Time string."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        sgt = dt + timedelta(hours=8)
        return sgt.strftime('%Y-%m-%d %H:%M SGT')
    except Exception:
        return iso_str

def make_envelope(source, current_gw, data):
    """Standard metadata wrapper applied to every output file."""
    return {
        'meta': {
            'schema_version': '1.0',
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'current_gw': current_gw,
            'source': source
        },
        'data': data
    }

def write_json(data, path):
    """Write JSON to file, creating directories as needed."""
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    size_kb = os.path.getsize(path) / 1024
    print(f'  ✅ {path} ({size_kb:.1f} KB)')


# ══════════════════════════════════════════════════════════════════════════════
# FETCH LAYER — raw API calls, no transformation
# ══════════════════════════════════════════════════════════════════════════════

def fetch_json(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_csv_url(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))

def fetch_bootstrap():
    return fetch_json(f'{FPL_BASE}/bootstrap-static/')

def fetch_fixtures_raw():
    return fetch_json(f'{FPL_BASE}/fixtures/')

def fetch_element_summary(player_id):
    return fetch_json(f'{FPL_BASE}/element-summary/{player_id}/')

def fetch_entry_history(entry_id):
    return fetch_json(f'{FPL_BASE}/entry/{entry_id}/history/')

def fetch_entry_picks(entry_id, gw):
    return fetch_json(f'{FPL_BASE}/entry/{entry_id}/event/{gw}/picks/')

def fetch_league_standings(league_id):
    return fetch_json(f'{FPL_BASE}/leagues-classic/{league_id}/standings/')

def fetch_live_gw(gw):
    return fetch_json(f'{FPL_BASE}/event/{gw}/live/')

def fetch_ci_file(config, filename):
    base = config['core_insights']['base_url']
    season = config['core_insights']['season']
    return fetch_csv_url(f'{base}/{season}/{filename}')


# ══════════════════════════════════════════════════════════════════════════════
# LOOKUP BUILDERS — index raw data for fast access
# ══════════════════════════════════════════════════════════════════════════════

def build_ci_latest(playerstats_rows):
    """
    Returns dict: player_id (str) -> most recent GW row from CI playerstats.
    Handles players with missing early GWs (mid-season arrivals).
    """
    latest = {}
    for row in playerstats_rows:
        pid = row['id']
        gw = int(row['gw'])
        if pid not in latest or gw > int(latest[pid]['gw']):
            latest[pid] = row
    return latest

def build_fpl_team_lookup(bootstrap):
    """Returns dict: team_id (int) -> team dict (short_name, strength, etc)."""
    return {t['id']: t for t in bootstrap['teams']}

def build_ci_team_lookup(ci_teams_rows):
    """Returns dict: team_id (str) -> CI team row (includes elo)."""
    return {row['id']: row for row in ci_teams_rows}


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-FLAGGING — promotes players from scan to focus universe
# ══════════════════════════════════════════════════════════════════════════════

def compute_auto_flags(bootstrap_elements, ci_latest, config):
    """
    Scores every player against configured thresholds.
    Returns dict: player_id (int) -> list of flag strings.

    Edge cases handled:
    - Players with 0 minutes filtered by scan_min_minutes
    - set_piece_threat field is empty in CI — uses penalties_order instead
    - All numeric CI fields cast safely
    """
    t = config['auto_flags']
    min_minutes = config['windows']['scan_min_minutes']

    # Build scored candidates (active only)
    candidates = []
    for e in bootstrap_elements:
        pid = str(e['id'])
        ci = ci_latest.get(pid, {})
        minutes = int(e.get('minutes') or 0)

        # Keep if has played OR is available/doubtful (new signing at season start)
        if e['status'] == 'u' and minutes < min_minutes:
            continue

        candidates.append({
            'id': e['id'],
            'position': e['element_type'],
            'price': e['now_cost'] / 10,
            'form': safe_float(e.get('form'), 0),
            'ep_next': safe_float(e.get('ep_next'), 0),
            'transfers_in_event': safe_int(e.get('transfers_in_event'), 0),
            'xgi_per90': safe_float(e.get('expected_goal_involvements_per_90'), 0),
            'minutes': minutes,
            'penalties_order': safe_float(ci.get('penalties_order')),
            'corners_order': safe_float(ci.get('corners_and_indirect_freekicks_order')),
            # CI defensive metric (= (CBI + Tackles) / (mins/90))
            'cbit_per90': safe_float(ci.get('defensive_contribution_per_90'), 0),
            # FPL bootstrap fields
            'saves': safe_int(e.get('saves'), 0),
            'cost_change_start': safe_int(e.get('cost_change_start'), 0),
            'clean_sheets': safe_int(e.get('clean_sheets'), 0),
            'avg_mins_per_gw': round(safe_int(e.get('minutes'), 0) / 38, 1),
        })

    flagged = {}  # player_id -> [flag, ...]

    def add_flag(player_id, flag):
        flagged.setdefault(player_id, []).append(flag)

    # Form top N
    for p in sorted(candidates, key=lambda x: x['form'], reverse=True)[:t['form_top_n']]:
        add_flag(p['id'], 'form_top20')

    # ep_next top N per position
    for pos in [1, 2, 3, 4]:
        pos_group = sorted(
            [p for p in candidates if p['position'] == pos],
            key=lambda x: x['ep_next'], reverse=True
        )[:t['ep_next_top_n_per_position']]
        for p in pos_group:
            add_flag(p['id'], 'ep_top_pos')

    # Trending transfers in
    for p in candidates:
        if p['transfers_in_event'] >= t['transfers_in_event_min']:
            add_flag(p['id'], 'trending_in')

    # Value gems: cheap + good form + played enough
    for p in candidates:
        if (p['price'] <= t['value_gem_max_price']
                and p['form'] >= t['value_gem_min_form']
                and p['minutes'] >= 450):
            add_flag(p['id'], 'value_gem')

    # xgi_elite: high xGI per 90, enough minutes
    for p in candidates:
        if (p['xgi_per90'] >= t['xgi_per90_min']
                and p['minutes'] >= t['xgi_min_minutes']):
            add_flag(p['id'], 'xgi_elite')

    # xgi_value: strong underlying numbers + mid-price range
    # Catches players with elite xGI in £6-7 range — value_gem misses these
    for p in candidates:
        if (p['xgi_per90'] >= t.get('xgi_value_min_xgi_per90', 0.55)
                and p['price'] <= t.get('xgi_value_max_price', 7.0)
                and p['price'] >= t.get('xgi_value_min_price', 6.0)
                and p['minutes'] >= t['xgi_min_minutes']):
            add_flag(p['id'], 'xgi_value')

    # cbit_strong: high defensive contribution for DEF/MID
    # Uses CI defensive_contribution_per_90 = (CBI + Tackles) / (mins/90)
    for p in candidates:
        if (p['position'] in (2, 3)
                and p['cbit_per90'] >= t.get('cbit_per90_min', 10.0)
                and p['minutes'] >= t.get('cbit_min_minutes', 450)):
            add_flag(p['id'], 'cbit_strong')

    # gk_shot_stopper: GKs with high saves per 90
    # High saves = points (1 per 3) + BPS + bonus at shot-heavy teams
    for p in candidates:
        saves_per90 = (p['saves'] / p['minutes'] * 90) if p['minutes'] > 0 else 0
        if (p['position'] == 1
                and saves_per90 >= t.get('gk_saves_per90_min', 3.0)
                and p['minutes'] >= t.get('gk_min_minutes', 450)):
            add_flag(p['id'], 'gk_shot_stopper')

    # price_rising: sustained price appreciation since season start
    # Signals FPL popularity + form momentum (live season only)
    for p in candidates:
        if (p['cost_change_start'] >= t.get('price_rising_min_change', 3)
                and p['form'] >= t.get('price_rising_min_form', 4.0)
                and p['minutes'] >= t.get('price_rising_min_minutes', 450)):
            add_flag(p['id'], 'price_rising')

    # rotation_risk: negative signal — low avg minutes per GW
    # Surfaces players with rotation concerns for the dashboard to highlight
    for p in candidates:
        if (p['avg_mins_per_gw'] <= t.get('rotation_risk_max_avg_mins', 55)
                and p['minutes'] >= t.get('rotation_risk_min_total_mins', 900)):
            add_flag(p['id'], 'rotation_risk')

    # cs_candidate: DEF/GK at teams with strong clean sheet record
    # Targets players at defensively solid teams for structural point floor
    for p in candidates:
        if (p['position'] in (1, 2)
                and p['clean_sheets'] >= t.get('cs_candidate_min_clean_sheets', 10)
                and p['minutes'] >= t.get('cs_candidate_min_minutes', 1800)):
            add_flag(p['id'], 'cs_candidate')

    # Set piece takers (using penalties_order and corners_order — set_piece_threat is empty)
    for p in candidates:
        if (p['penalties_order'] is not None
                and p['penalties_order'] <= t['penalty_taker_order']
                and p['minutes'] >= t.get('penalty_taker_min_minutes', 0)):
            add_flag(p['id'], 'penalty_taker')
        if (p['corners_order'] is not None
                and p['corners_order'] <= t['corner_taker_max_order']
                and p['xgi_per90'] >= t.get('corner_taker_min_xgi_per90', 0)):
            add_flag(p['id'], 'corner_taker')

    return flagged


# ══════════════════════════════════════════════════════════════════════════════
# OUTPUT BUILDERS — one function per output file
# ══════════════════════════════════════════════════════════════════════════════

def build_player_scan(bootstrap, ci_latest, auto_flags, current_gw, config):
    """
    player_scan.json — all active players, 15 fields, for discovery.
    Excludes unavailable players with 0 minutes (phantom registrations).
    """
    min_minutes = config['windows']['scan_min_minutes']
    players = []

    for e in bootstrap['elements']:
        pid = str(e['id'])
        minutes = int(e.get('minutes') or 0)

        if e['status'] == 'u' and minutes < min_minutes:
            continue

        players.append({
            'id': e['id'],
            'name': e['web_name'],
            'team_id': e['team'],
            'position': e['element_type'],
            'price': e['now_cost'] / 10,          # normalised to £
            'status': e['status'],
            'total_pts': e['total_points'],
            'fpl_form': safe_float(e.get('form'), 0),
            'ppg': safe_float(e.get('points_per_game'), 0),
            'ep_next': safe_float(e.get('ep_next'), 0),
            'selected_pct': safe_float(e.get('selected_by_percent'), 0),
            'transfers_in_event': safe_int(e.get('transfers_in_event'), 0),
            'transfers_out_event': safe_int(e.get('transfers_out_event'), 0),
            'minutes': minutes,
            'xgi_per90': safe_float(e.get('expected_goal_involvements_per_90'), 0),
            'flags': auto_flags.get(e['id'], []),
            'flag_count': len(auto_flags.get(e['id'], []))
        })

    return make_envelope('fpl-api', current_gw, {'players': players})


def build_player_universe(bootstrap, ci_latest, fpl_teams, ci_teams,
                          universe_ids, auto_flags, current_gw):
    """
    player_universe.json — focus players (~100-160), full field set.
    form_last5 is None here; backfilled after player_history is built.

    CRITICAL edge cases handled:
    - Price: FPL now_cost / 10 → £  (CI price is already £, so use FPL for consistency)
    - set_piece_threat: empty in CI, replaced with is_penalty_taker / is_corner_taker
    - xg_delta: actual goals minus expected goals (season-level over/underperformance)
    """
    players = []

    for e in bootstrap['elements']:
        if e['id'] not in universe_ids:
            continue

        pid = str(e['id'])
        ci = ci_latest.get(pid, {})
        team = fpl_teams.get(e['team'], {})
        ci_team = ci_teams.get(str(e['team']), {})

        # Set piece indicators (set_piece_threat is empty — use order fields)
        pen_order = safe_float(ci.get('penalties_order'))
        corner_order = safe_float(ci.get('corners_and_indirect_freekicks_order'))
        is_penalty_taker = pen_order is not None and pen_order <= 1
        is_corner_taker = corner_order is not None and corner_order <= 2

        # xg_delta: overperformance vs expected (season total)
        actual_goals = safe_float(e.get('goals_scored'), 0)
        expected_goals = safe_float(e.get('expected_goals'), 0)
        xg_delta = round(actual_goals - expected_goals, 2) if expected_goals else None

        players.append({
            'id': e['id'],
            'name': e['web_name'],
            'team_id': e['team'],
            'team_short': team.get('short_name', ''),
            'position': e['element_type'],
            'price': e['now_cost'] / 10,
            'status': e['status'],
            'cop_next': e.get('chance_of_playing_next_round'),
            'news': e.get('news', ''),
            # Points
            'total_pts': e['total_points'],
            'fpl_form': safe_float(e.get('form'), 0),
            'ppg': safe_float(e.get('points_per_game'), 0),
            'ep_next': safe_float(e.get('ep_next'), 0),
            # Ownership
            'selected_pct': safe_float(e.get('selected_by_percent'), 0),
            'transfers_in_event': safe_int(e.get('transfers_in_event'), 0),
            'transfers_out_event': safe_int(e.get('transfers_out_event'), 0),
            # Season stats
            'minutes': safe_int(e.get('minutes'), 0),
            'goals': safe_int(e.get('goals_scored'), 0),
            'assists': safe_int(e.get('assists'), 0),
            'clean_sheets': safe_int(e.get('clean_sheets'), 0),
            'bonus': safe_int(e.get('bonus'), 0),
            # xG metrics (FPL API)
            'xg_per90': safe_float(e.get('expected_goals_per_90'), 0),
            'xa_per90': safe_float(e.get('expected_assists_per_90'), 0),
            'xgi_per90': safe_float(e.get('expected_goal_involvements_per_90'), 0),
            'xgc_per90': safe_float(e.get('expected_goals_conceded_per_90'), 0),
            # CI-only metrics (defensive / set piece)
            'cbi_per90': safe_float(ci.get('clearances_blocks_interceptions_per_90')),
            'defensive_contribution_per90': safe_float(ci.get('defensive_contribution_per_90')),
            'tackles': safe_int(ci.get('tackles')),
            'is_penalty_taker': is_penalty_taker,
            'is_corner_taker': is_corner_taker,
            # Derived
            'form_last5': None,   # backfilled after player_history is built
            'xg_delta': xg_delta,
            # Team context
            'team_elo': safe_float(ci_team.get('elo')),
            'team_strength_att_home': safe_int(team.get('strength_attack_home')),
            'team_strength_att_away': safe_int(team.get('strength_attack_away')),
            'team_strength_def_home': safe_int(team.get('strength_defence_home')),
            'team_strength_def_away': safe_int(team.get('strength_defence_away')),
            # Flags
            'flags': auto_flags.get(e['id'], []),
            'flag_count': len(auto_flags.get(e['id'], []))
        })

    return make_envelope('fpl-api+core-insights', current_gw, {'players': players})


def build_player_history(universe_ids, current_gw, config, target_gw=None):
    """
    player_history.json — last N GWs of discrete per-GW data for focus players.

    Source: FPL element-summary/{id}/ — already discrete, no subtraction needed.

    Edge cases handled:
    - DGW: two fixtures in one round are summed into a single GW entry
    - xG fields are strings in FPL API — cast to float explicitly
    - Partial history: new players may have < window GWs — stored as-is
    - Missing players: logged as warning, skipped cleanly
    """
    gw_window = config['windows']['player_history_gws']
    max_gw = target_gw or current_gw
    min_gw = max(1, max_gw - gw_window + 1)

    players_out = {}

    total = len(universe_ids)
    for i, player_id in enumerate(universe_ids, 1):
        if i % 20 == 0 or i == total:
            print(f'    element-summary: {i}/{total} players fetched', end='\r')

        try:
            summary = fetch_element_summary(player_id)
            history = summary.get('history', [])

            # Group by round — handles DGW by summing both fixtures
            by_round = defaultdict(lambda: {
                'gw': 0, 'pts': 0, 'mins': 0,
                'goals': 0, 'assists': 0, 'bonus': 0, 'cs': 0, 'saves': 0,
                'xg': 0.0, 'xa': 0.0, 'xgi': 0.0, 'xgc': 0.0,
                'price': 0.0, 'selected_pct': 0.0,
                'transfers_in': 0, 'transfers_out': 0,
                'was_home': None, 'fixture_count': 0
            })

            for h in history:
                gw = h['round']
                if gw < min_gw or gw > max_gw:
                    continue

                r = by_round[gw]
                r['gw'] = gw
                r['pts'] += h['total_points']
                r['mins'] += h['minutes']
                r['goals'] += h['goals_scored']
                r['assists'] += h['assists']
                r['bonus'] += h['bonus']
                r['cs'] += h['clean_sheets']
                r['saves'] += h.get('saves', 0)
                # xG fields: MUST cast from string to float (FPL API quirk)
                r['xg'] += float(h.get('expected_goals') or 0)
                r['xa'] += float(h.get('expected_assists') or 0)
                r['xgi'] += float(h.get('expected_goal_involvements') or 0)
                r['xgc'] += float(h.get('expected_goals_conceded') or 0)
                r['price'] = h['value'] / 10   # FPL stores as pence×10
                r['selected_pct'] = round(
                    h['selected'] / 1_000_000 * 100, 1
                ) if h.get('selected') else 0
                r['transfers_in'] += h.get('transfers_in', 0)
                r['transfers_out'] += h.get('transfers_out', 0)
                r['fixture_count'] += 1
                if r['fixture_count'] == 1:
                    r['was_home'] = h.get('was_home')
                else:
                    r['was_home'] = 'dgw'  # double gameweek marker

            gw_list = sorted(by_round.values(), key=lambda x: x['gw'])

            # Compute form_last5 from discrete history
            sorted_rounds = sorted(by_round.keys())
            last5_rounds = sorted_rounds[-5:] if len(sorted_rounds) >= 5 else sorted_rounds
            form_last5 = sum(by_round[r]['pts'] for r in last5_rounds)

            players_out[str(player_id)] = {
                'form_last5': form_last5,
                'gws_available': len(gw_list),
                'gws': gw_list
            }

        except Exception as e:
            print(f'\n  ⚠️  Skipped player {player_id}: {e}')
            continue

    print()  # newline after progress line

    return make_envelope('fpl-api', current_gw, {
        'gw_window': gw_window,
        'min_gw': min_gw,
        'max_gw': max_gw,
        'players': players_out
    })


def build_manager_history(my_entry_id, rival_entry_ids, current_gw, target_gw=None):
    """
    manager_history.json — GW-by-GW points and rank for all 7 managers.
    Includes rank_delta (direction of movement) and percentile_rank.
    """
    max_gw = target_gw or current_gw
    all_ids = [my_entry_id] + rival_entry_ids
    managers = {}

    for entry_id in all_ids:
        try:
            hist = fetch_entry_history(entry_id)
            season_gws = hist.get('current', [])
            chips = hist.get('chips', [])

            gws = []
            prev_or = None

            for gw_data in season_gws:
                gw = gw_data['event']
                if gw > max_gw:
                    continue

                overall_rank = gw_data.get('overall_rank')
                rank_delta = None
                if prev_or is not None and overall_rank is not None:
                    rank_delta = overall_rank - prev_or  # negative = improved rank
                prev_or = overall_rank

                chip_this_gw = next(
                    (c['name'] for c in chips if c['event'] == gw), None
                )

                gws.append({
                    'gw': gw,
                    'pts': gw_data['points'],
                    'total': gw_data['total_points'],
                    'overall_rank': overall_rank,
                    'percentile_rank': gw_data.get('percentile_rank'),
                    'rank_delta': rank_delta,
                    'hits': gw_data.get('event_transfers_cost', 0),
                    'bench_pts': gw_data.get('points_on_bench', 0),
                    'value': gw_data.get('value', 0),
                    'bank': gw_data.get('bank', 0),
                    'chip': chip_this_gw
                })

            managers[str(entry_id)] = {
                'entry_id': entry_id,
                'chips_used': [
                    {'name': c['name'], 'gw': c['event']} for c in chips
                    if c['event'] <= max_gw
                ],
                'gws': gws
            }

        except Exception as e:
            print(f'  ⚠️  Skipped manager {entry_id}: {e}')

    return make_envelope('fpl-api', current_gw, {'managers': managers})


def build_league_table(league_id, current_gw):
    """league_table.json — current standings for all league managers."""
    data = fetch_league_standings(league_id)
    results = data.get('standings', {}).get('results', [])
    league_name = data.get('league', {}).get('name', '')

    table = []
    for i, r in enumerate(results):
        table.append({
            'rank': r['rank'],
            'last_rank': r['last_rank'],
            'rank_delta': r['last_rank'] - r['rank'],   # positive = moved up
            'entry_id': r['entry'],
            'entry_name': r['entry_name'],
            'manager_name': r['player_name'],
            'gw_pts': r['event_total'],
            'total': r['total'],
            'gap_to_first': results[0]['total'] - r['total'],
            'gap_to_next': results[i - 1]['total'] - r['total'] if i > 0 else 0
        })

    return make_envelope('fpl-api', current_gw, {
        'league_name': league_name,
        'standings': table
    })


def build_fixtures(bootstrap, fixtures_raw, current_gw, config):
    """
    fixtures.json — all fixtures + pre-computed fdr_by_team lookup.
    fdr_by_team means dashboard/skills never need to join fixtures at runtime.
    """
    lookahead = config['windows']['fixture_lookahead_gws']
    fdr_window = config['windows']['fdr_avg_window']
    team_short = {t['id']: t['short_name'] for t in bootstrap['teams']}

    fixtures = []
    for f in fixtures_raw:
        gw = f.get('event')
        if not gw:
            continue
        fixtures.append({
            'gw': gw,
            'fixture_id': f['id'],
            'home_team_id': f['team_h'],
            'home_team': team_short.get(f['team_h'], ''),
            'away_team_id': f['team_a'],
            'away_team': team_short.get(f['team_a'], ''),
            'fdr_home': f.get('team_h_difficulty'),
            'fdr_away': f.get('team_a_difficulty'),
            'ko': f.get('kickoff_time'),
            'finished': f.get('finished', False)
        })

    # Pre-compute FDR lookup for each team for the next N GWs
    upcoming = list(range(current_gw, current_gw + lookahead + 1))
    fdr_by_team = {}

    for team in bootstrap['teams']:
        tid = team['id']
        team_fixtures = []

        for f in fixtures_raw:
            gw = f.get('event')
            if not gw or gw not in upcoming:
                continue
            if f['team_h'] == tid:
                team_fixtures.append({
                    'gw': gw,
                    'opponent': team_short.get(f['team_a'], ''),
                    'fdr': f.get('team_h_difficulty'),
                    'home': True
                })
            elif f['team_a'] == tid:
                team_fixtures.append({
                    'gw': gw,
                    'opponent': team_short.get(f['team_h'], ''),
                    'fdr': f.get('team_a_difficulty'),
                    'home': False
                })

        team_fixtures.sort(key=lambda x: x['gw'])
        window_fdrs = [f['fdr'] for f in team_fixtures[:fdr_window] if f['fdr']]
        avg_fdr = round(sum(window_fdrs) / len(window_fdrs), 1) if window_fdrs else None

        fdr_by_team[str(tid)] = {
            'next_fixtures': team_fixtures,
            'avg_fdr_next3': avg_fdr
        }

    return make_envelope('fpl-api', current_gw, {
        'fixtures': fixtures,
        'fdr_by_team': fdr_by_team
    })


def build_my_squad(my_entry_id, current_gw, bootstrap):
    """my_squad.json — our current 15 with bank, FTs, captain info."""
    name_lookup = {e['id']: e['web_name'] for e in bootstrap['elements']}
    picks_data = fetch_entry_picks(my_entry_id, current_gw)
    history = fetch_entry_history(my_entry_id)
    current_hist = next(
        (h for h in history['current'] if h['event'] == current_gw), {}
    )

    picks = []
    for p in picks_data.get('picks', []):
        picks.append({
            'position': p['position'],
            'player_id': p['element'],
            'name': name_lookup.get(p['element'], ''),
            'is_captain': p['is_captain'],
            'is_vice_captain': p['is_vice_captain'],
            'multiplier': p['multiplier'],
            'on_bench': p['position'] > 11,
            'bench_order': p['position'] - 11 if p['position'] > 11 else None
        })

    return make_envelope('fpl-api', current_gw, {
        'bank': current_hist.get('bank', 0),
        'squad_value': current_hist.get('value', 0),
        'event_transfers': current_hist.get('event_transfers', 0),
        'event_transfers_cost': current_hist.get('event_transfers_cost', 0),
        'active_chip': picks_data.get('active_chip'),
        'picks': picks
    })


def build_rival_squads(rival_entry_ids, current_gw, bootstrap):
    """rival_squads.json — all 6 rival squads with captain info."""
    name_lookup = {e['id']: e['web_name'] for e in bootstrap['elements']}
    rivals = {}

    for entry_id in rival_entry_ids:
        try:
            picks_data = fetch_entry_picks(entry_id, current_gw)
            picks = []
            for p in picks_data.get('picks', []):
                picks.append({
                    'position': p['position'],
                    'player_id': p['element'],
                    'name': name_lookup.get(p['element'], ''),
                    'is_captain': p['is_captain'],
                    'is_vice_captain': p['is_vice_captain'],
                    'multiplier': p['multiplier'],
                    'on_bench': p['position'] > 11
                })
            rivals[str(entry_id)] = {
                'active_chip': picks_data.get('active_chip'),
                'picks': picks
            }
        except Exception as e:
            print(f'  ⚠️  Skipped rival {entry_id}: {e}')

    return make_envelope('fpl-api', current_gw, {'rivals': rivals})


def build_gw_meta(bootstrap, ci_gw_summaries, current_gw):
    """
    gw_meta.json — GW context: deadlines in SGT, averages, most captained.
    Deadlines converted to Singapore Time for local relevance.
    """
    events = {e['id']: e for e in bootstrap['events']}
    ci_by_gw = {int(r['id']): r for r in ci_gw_summaries}

    def event_block(gw_id):
        e = events.get(gw_id, {})
        return {
            'id': gw_id,
            'name': e.get('name', f'Gameweek {gw_id}'),
            'deadline': e.get('deadline_time'),
            'deadline_sgt': to_sgt(e.get('deadline_time')),
            'finished': e.get('finished', False)
        }

    ci_prev = ci_by_gw.get(current_gw - 1, {})

    return make_envelope('fpl-api+core-insights', current_gw, {
        'current_gw': event_block(current_gw),
        'next_gw': event_block(current_gw + 1) if current_gw < 38 else None,
        'previous_gw': {
            **event_block(current_gw - 1),
            'average_score': safe_int(ci_prev.get('average_entry_score')),
            'highest_score': safe_int(ci_prev.get('highest_score')),
            'most_captained_id': safe_int(ci_prev.get('most_captained')),
            'most_transferred_in_id': safe_int(ci_prev.get('most_transferred_in')),
        } if current_gw > 1 else None
    })


def build_live_gw(current_gw, bootstrap, my_squad_ids, rival_entry_ids, config):
    """
    live_gw.json — live points for universe players + pre-computed team scores.
    Only meaningful during a live gameweek.
    """
    live_data = fetch_live_gw(current_gw)
    name_lookup = {e['id']: e['web_name'] for e in bootstrap['elements']}

    # Index live data by player ID
    live_by_player = {
        e['id']: e['stats'] for e in live_data.get('elements', [])
    }

    # Build player live stats (universe only)
    universe_ids = set(my_squad_ids)
    for entry_id in rival_entry_ids:
        try:
            picks = fetch_entry_picks(entry_id, current_gw)
            for p in picks['picks']:
                universe_ids.add(p['element'])
        except Exception:
            pass

    players = {}
    for pid in universe_ids:
        stats = live_by_player.get(pid, {})
        players[str(pid)] = {
            'name': name_lookup.get(pid, ''),
            'pts': stats.get('total_points', 0),
            'mins': stats.get('minutes', 0),
            'goals': stats.get('goals_scored', 0),
            'assists': stats.get('assists', 0),
            'cs': stats.get('clean_sheets', 0),
            'bonus': stats.get('bonus', 0),
            'bps': stats.get('bps', 0),
            'yellow_cards': stats.get('yellow_cards', 0),
            'red_cards': stats.get('red_cards', 0),
            'saves': stats.get('saves', 0),
            'in_dreamteam': stats.get('in_dreamteam', False)
        }

    return make_envelope('fpl-api', current_gw, {
        'players': players,
        'fetched_at_note': 'Bonus points may be provisional during live matches'
    })


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(mode='live', target_gw=None):
    print(f'\n🏃 FPL Pipeline — mode={mode}' +
          (f', GW={target_gw}' if target_gw else '') + '\n')

    config = load_config()
    my_entry_id = config['fpl']['my_entry_id']
    rival_entry_ids = config['fpl']['rival_entry_ids']
    league_id = config['fpl']['league_id']

    # Determine output path based on mode
    if mode == 'backtest' and target_gw:
        out = f"seasons/{config['season']['current']}/snapshots/GW{target_gw:02d}/"
    elif mode == 'offseason':
        out = config['output_paths']['season_final']
    else:
        out = config['output_paths']['live']

    print(f'📂 Output → {out}\n')

    # ── 1. Fetch all source data ───────────────────────────────────────────
    print('📡 Fetching source data...')
    bootstrap     = fetch_bootstrap();           print('  ✅ FPL bootstrap-static')
    fixtures_raw  = fetch_fixtures_raw();        print('  ✅ FPL fixtures')
    ci_stats      = fetch_ci_file(config, 'playerstats.csv')
    print(f'  ✅ CI playerstats ({len(ci_stats):,} rows)')
    ci_teams_rows = fetch_ci_file(config, 'teams.csv');     print('  ✅ CI teams')
    ci_gw_summ    = fetch_ci_file(config, 'gameweek_summaries.csv')
    print('  ✅ CI gameweek_summaries')

    # Determine active GW
    current_gw = target_gw or next(
        (e['id'] for e in bootstrap['events'] if e['is_current']), 38
    )
    print(f'\n📅 Active GW: {current_gw}')

    # ── 2. Build lookups ───────────────────────────────────────────────────
    print('\n🔧 Building lookups...')
    ci_latest  = build_ci_latest(ci_stats)
    fpl_teams  = build_fpl_team_lookup(bootstrap)
    ci_teams   = build_ci_team_lookup(ci_teams_rows)
    print(f'  ✅ CI latest: {len(ci_latest)} players | FPL teams: {len(fpl_teams)}')

    # ── 3. Get squad and rival picks ───────────────────────────────────────
    print('\n👥 Fetching picks...')
    try:
        my_picks_data = fetch_entry_picks(my_entry_id, current_gw)
        my_squad_ids  = set(p['element'] for p in my_picks_data['picks'])
        print(f'  ✅ My squad: {len(my_squad_ids)} players')
    except Exception as e:
        print(f'  ⚠️  My squad unavailable: {e}')
        my_squad_ids = set()

    rival_ids = set()
    for entry_id in rival_entry_ids:
        try:
            picks = fetch_entry_picks(entry_id, current_gw)
            for p in picks['picks']:
                rival_ids.add(p['element'])
        except Exception as e:
            print(f'  ⚠️  Rival {entry_id} picks unavailable: {e}')
    print(f'  ✅ Rival unique players: {len(rival_ids)}')

    # ── 4. Compute auto-flags ──────────────────────────────────────────────
    print('\n🚩 Auto-flagging...')
    auto_flags = compute_auto_flags(bootstrap['elements'], ci_latest, config)
    flag_counts = {}
    for flags in auto_flags.values():
        for f in flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    for flag, count in sorted(flag_counts.items()):
        print(f'  {flag}: {count}')
    print(f'  Total flagged: {len(auto_flags)} players')

    # Build full universe ID set
    # rotation_risk warns only — do not add players to universe just for this
    buy_flagged = set(
        pid for pid, flags in auto_flags.items()
        if set(flags) - {"rotation_risk"}
    )
    universe_ids = my_squad_ids | rival_ids | buy_flagged
    print(f'  Universe size: {len(universe_ids)} players')

    # ── 5. Build and write outputs ─────────────────────────────────────────
    print('\n💾 Writing outputs...')

    write_json(
        build_player_scan(bootstrap, ci_latest, auto_flags, current_gw, config),
        f'{out}player_scan.json'
    )

    universe_data = build_player_universe(
        bootstrap, ci_latest, fpl_teams, ci_teams,
        universe_ids, auto_flags, current_gw
    )
    write_json(universe_data, f'{out}player_universe.json')

    print(f'  Fetching element-summary for {len(universe_ids)} players...')
    history_data = build_player_history(
        universe_ids, current_gw, config, target_gw
    )
    write_json(history_data, f'{out}player_history.json')

    # Backfill form_last5 into player_universe now that history is built
    hist_players = history_data['data']['players']
    for p in universe_data['data']['players']:
        pid = str(p['id'])
        if pid in hist_players:
            p['form_last5'] = hist_players[pid]['form_last5']
    write_json(universe_data, f'{out}player_universe.json')  # rewrite with form_last5

    write_json(
        build_manager_history(my_entry_id, rival_entry_ids, current_gw, target_gw),
        f'{out}manager_history.json'
    )
    write_json(
        build_league_table(league_id, current_gw),
        f'{out}league_table.json'
    )
    write_json(
        build_fixtures(bootstrap, fixtures_raw, current_gw, config),
        f'{out}fixtures.json'
    )
    write_json(
        build_gw_meta(bootstrap, ci_gw_summ, current_gw),
        f'{out}gw_meta.json'
    )

    if my_squad_ids:
        write_json(
            build_my_squad(my_entry_id, current_gw, bootstrap),
            f'{out}my_squad.json'
        )

    write_json(
        build_rival_squads(rival_entry_ids, current_gw, bootstrap),
        f'{out}rival_squads.json'
    )

    # live_gw.json — only in live or backtest modes (not offseason)
    if mode in ('live', 'backtest'):
        write_json(
            build_live_gw(current_gw, bootstrap, my_squad_ids, rival_entry_ids, config),
            f'{out}live_gw.json'
        )

    # ── 6. Update meta/season_state.json ──────────────────────────────────
    state = {
        'current_season': config['season']['current'],
        'current_gw': current_gw,
        'previous_gw': current_gw - 1 if current_gw > 1 else None,
        'next_gw': current_gw + 1 if current_gw < 38 else None,
        'mode': mode,
        'mode_note': f'Pipeline ran in {mode} mode at GW{current_gw}.',
        'last_pipeline_run': datetime.now(timezone.utc).isoformat(),
        'ci_data_available': True,
        'fpl_api_available': True,
        'next_season_expected': '2026-08-01'
    }
    write_json(state, 'meta/season_state.json')

    print(f'\n✅ Pipeline complete — GW{current_gw} | mode={mode}')
    print(f'   Files written to: {out}\n')


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='FPL Data Pipeline')
    parser.add_argument(
        '--mode',
        choices=['live', 'offseason', 'backtest', 'preseason'],
        default='live',
        help='Pipeline mode (default: live)'
    )
    parser.add_argument(
        '--gw',
        type=int,
        help='Gameweek number — required for backtest mode'
    )
    args = parser.parse_args()

    if args.mode == 'backtest' and not args.gw:
        print('❌  Error: --gw is required for backtest mode')
        print('   Example: python pipeline.py --mode=backtest --gw=20')
        sys.exit(1)

    run_pipeline(mode=args.mode, target_gw=args.gw)
