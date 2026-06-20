#!/usr/bin/env python3
"""Build xg.json — per-match team xG (for & against) + actual goals, for the xG page.

Source: FPL API only (stdlib). For each finished gameweek, the /event/{gw}/live/
endpoint reports each player's expected_goals; summing by club gives that club's
xG created in its match that week. The opponent's sum is its xG conceded. Actual
goals come from /fixtures/. Pre-match model win probs (ph/pa) are merged from the
model file if present, else left null (the detail view's context note just omits).

Emits: {generated, season, event, games:[{gw,h,a,gh,ga,xh,xa,ph,pa}, ...]}
Single match per club per GW is assumed (double-gameweeks fold into one GW row).

    python build_xg.py            # writes ./xg.json
"""
from __future__ import annotations
import json, os, urllib.request, datetime as dt
from pathlib import Path

API = "https://fantasy.premierleague.com/api"
OUT = Path(os.environ.get("XG_OUT", "xg.json"))
MODEL = os.environ.get("XG_MODEL", "")  # optional Model 20xx.json for pre-match probs


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main() -> int:
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    el_team = {e["id"]: e["team"] for e in boot["elements"]}
    finished = sorted({f["event"] for f in boot["events"] if f.get("finished") and f.get("id")}) \
        if False else [e["id"] for e in boot["events"] if e.get("finished")]

    fixtures = get("/fixtures/")
    by_gw = {}
    for f in fixtures:
        if f.get("event"):
            by_gw.setdefault(f["event"], []).append(f)

    # optional pre-match probabilities, keyed (gw, home, away)
    probs = {}
    if MODEL and Path(MODEL).exists():
        for x in json.loads(Path(MODEL).read_text()).get("fixtures", []):
            probs[(x["gw"], x["home"], x["away"])] = (x.get("p_home"), x.get("p_away"))

    # manual FotMob xG overrides (committed via the Sync xG button), keyed "gw|HOME|AWAY"
    try:
        overrides = json.loads((Path(__file__).resolve().parent / "data" / "xg_overrides.json").read_text())
    except Exception:
        overrides = {}

    games = []
    last = 0
    for gw in finished:
        try:
            live = {x["id"]: x["stats"] for x in get(f"/event/{gw}/live/")["elements"]}
        except Exception:
            continue
        # club xG created this GW = sum of its players' expected_goals
        team_xg = {}
        for pid, st in live.items():
            t = el_team.get(pid)
            if t is None:
                continue
            team_xg[t] = team_xg.get(t, 0.0) + float(st.get("expected_goals", 0) or 0)
        for f in by_gw.get(gw, []):
            if not f.get("finished") or f.get("team_h_score") is None:
                continue
            h, a = f["team_h"], f["team_a"]
            ph, pa = probs.get((gw, short[h], short[a]), (None, None))
            xh, xa = round(team_xg.get(h, 0.0), 2), round(team_xg.get(a, 0.0), 2)
            ov = overrides.get(f"{gw}|{short[h]}|{short[a]}")  # manual FotMob xG wins where present
            manual = False
            if ov and len(ov) == 2:
                xh, xa = float(ov[0]), float(ov[1]); manual = True
            games.append({
                "gw": gw, "h": short[h], "a": short[a],
                "gh": int(f["team_h_score"]), "ga": int(f["team_a_score"]),
                "xh": xh, "xa": xa, "ph": ph, "pa": pa, "m": 1 if manual else 0,
            })
            last = max(last, gw)

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "season": "live", "event": last, "games": games}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: {len(games)} games through GW{last}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
