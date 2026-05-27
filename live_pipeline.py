"""
FPL Live Pipeline — live_gw.json updater
Runs every 30 min during match windows via GitHub Actions.

Only updates live/live_gw.json — does not touch any other files.
Reads picks from local live/ files (fixed during GW, no re-fetch needed).
Exits immediately if mode is offseason or preseason.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


FPL_BASE = 'https://fantasy.premierleague.com/api'
HEADERS  = {'User-Agent': 'Mozilla/5.0'}
MY_ENTRY = 822500


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def read_local(path):
    with open(path) as f:
        return json.load(f)

def write_json(data, path):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f'  ✅ {path} ({os.path.getsize(path) / 1024:.1f} KB)')


# ── Score computation ─────────────────────────────────────────────────────────

def compute_live_score(picks, live_by_player):
    """
    Compute a manager's live GW score from their picks and live player data.
    Handles captain multiplier (×2 or ×3 for TC chip).
    Skips benched players.
    """
    total        = 0
    captain_pts  = 0
    captain_name = ''

    for p in picks:
        if p.get('on_bench'):
            continue

        pid        = p['player_id']
        stats      = live_by_player.get(pid, {})
        raw_pts    = stats.get('total_points', 0)
        multiplier = p.get('multiplier', 1)
        weighted   = raw_pts * multiplier

        total += weighted

        if p.get('is_captain'):
            captain_pts  = weighted
            captain_name = p.get('name', '')

    return total, captain_pts, captain_name


# ── Main ──────────────────────────────────────────────────────────────────────

def run_live_pipeline():
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f'\n⚡ FPL Live Pipeline — {now}\n')

    # 1. Check season state — exit early if not a live GW
    try:
        state      = read_local('meta/season_state.json')
        mode       = state.get('mode', 'live')
        current_gw = state.get('current_gw', 38)
    except Exception as e:
        print(f'❌ Could not read season_state.json: {e}')
        sys.exit(1)

    if mode in ('offseason', 'preseason'):
        print(f'Mode is [{mode}] — no active GW. Nothing to update.')
        sys.exit(0)

    print(f'📅 Active GW: {current_gw}')

    # 2. Fetch live points from FPL API
    print('📡 Fetching live data...')
    try:
        live_data      = fetch_json(f'{FPL_BASE}/event/{current_gw}/live/')
        live_by_player = {e['id']: e['stats'] for e in live_data.get('elements', [])}
        print(f'  ✅ {len(live_by_player)} player entries fetched')
    except Exception as e:
        print(f'  ❌ Failed to fetch live data: {e}')
        sys.exit(1)

    # 3. Load picks from local files — fixed during GW, no re-fetch needed
    try:
        my_squad = read_local('live/my_squad.json')
        rivals   = read_local('live/rival_squads.json')
    except Exception as e:
        print(f'  ❌ Could not read picks files: {e}')
        print('    Run the main pipeline first to populate live/ folder')
        sys.exit(1)

    # 4. Compute live scores for all managers
    our_picks               = my_squad['data']['picks']
    our_score, our_cap_pts, our_cap_name = compute_live_score(our_picks, live_by_player)

    rival_scores = {}
    for entry_id, rival_data in rivals['data']['rivals'].items():
        score, cap_pts, cap_name = compute_live_score(
            rival_data['picks'], live_by_player
        )
        rival_scores[entry_id] = {
            'score':        score,
            'captain_pts':  cap_pts,
            'captain_name': cap_name
        }

    # 5. Build player stats dict (universe players only for file size)
    players = {}
    for pid, stats in live_by_player.items():
        players[str(pid)] = {
            'pts':          stats.get('total_points', 0),
            'mins':         stats.get('minutes', 0),
            'goals':        stats.get('goals_scored', 0),
            'assists':      stats.get('assists', 0),
            'cs':           stats.get('clean_sheets', 0),
            'bonus':        stats.get('bonus', 0),
            'bps':          stats.get('bps', 0),
            'yellow_cards': stats.get('yellow_cards', 0),
            'red_cards':    stats.get('red_cards', 0),
            'saves':        stats.get('saves', 0),
            'in_dreamteam': stats.get('in_dreamteam', False)
        }

    # 6. Write live_gw.json
    output = {
        'meta': {
            'schema_version': '1.0',
            'fetched_at':     datetime.now(timezone.utc).isoformat(),
            'current_gw':     current_gw,
            'source':         'fpl-api'
        },
        'data': {
            'gw': current_gw,
            'note': 'Bonus points provisional during live matches',
            'scores': {
                str(MY_ENTRY): {
                    'entry_name':   'wirtzplay',
                    'score':        our_score,
                    'captain_pts':  our_cap_pts,
                    'captain_name': our_cap_name
                },
                **{
                    eid: {'score': d['score'], 'captain_pts': d['captain_pts'],
                          'captain_name': d['captain_name']}
                    for eid, d in rival_scores.items()
                }
            },
            'players': players
        }
    }

    print('\n💾 Writing output...')
    write_json(output, 'live/live_gw.json')

    # 7. Print live scoreboard
    print(f'\n📊 Live scoreboard — GW{current_gw}:')
    print(f'  wirtzplay    {our_score:>3} pts  (C: {our_cap_name} {our_cap_pts})')
    for eid, d in rival_scores.items():
        print(f'  {eid:12} {d["score"]:>3} pts  (C: {d["captain_name"]} {d["captain_pts"]})')

    print(f'\n✅ Live pipeline complete\n')


if __name__ == '__main__':
    run_live_pipeline()
