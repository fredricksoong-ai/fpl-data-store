#!/usr/bin/env python3
"""Build players.json — the autoflag board, ported from the original fpl-data-store.

Scores every active player against the same 11 buy-signal thresholds (plus the
rotation_risk avoid-flag) and keeps those carrying >=1 flag, ranked by flag count
(more flags = higher conviction). Stdlib only — reads the live FPL bootstrap.

Output: {generated, event, players:[{name,code,pos,price,min,form,ep,xgi,sel,flags,flag_count}]}
Thresholds mirror the wiki (wiki/fpl-autoflag-system.md); tune here for a new season.
"""
from __future__ import annotations
import json, os, urllib.request, datetime as dt
from pathlib import Path

API = "https://fantasy.premierleague.com/api"
OUT = Path(os.environ.get("PLAYERS_OUT", "players.json"))
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# thresholds (price in tenths: 60 = £6.0)
T = dict(value_gem_price=60, value_gem_form=5.0, value_gem_min=450,
         price_rising_change=3, price_rising_form=4.0, price_rising_min=450,
         cs_clean=10, cs_min=1800, xgi_elite=0.65, xgi_elite_min=450,
         xgi_value=0.55, xgi_value_price=70, xgi_value_min=450,
         cbit=11.5, cbit_min=900, corner_xgi=0.15, pen_min=600,
         gk_saves90=3.2, gk_min=450, rot_avg=50, rot_total=1800)


def get(path, tries=5):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    for _i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as _e:
            # retry transient failures (launch-week 503s, timeouts, 429s) but fail fast on real client errors
            if _i == tries - 1 or getattr(_e, "code", None) in (400, 401, 403, 404):
                raise
            import time; time.sleep(2 * (_i + 1))


def f(x):
    try: return float(x)
    except Exception: return 0.0


def main() -> int:
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    fin = [e["id"] for e in boot["events"] if e.get("finished")]
    event = fin[-1] if fin else 0
    els = [e for e in boot["elements"] if e.get("minutes", 0) > 0]

    # rank-based pools
    by_form = sorted(els, key=lambda e: -f(e.get("form")))
    form_top20 = {e["id"] for e in by_form[:20]}
    ep_top = set()
    for pt in POS:
        pool = sorted([e for e in els if e["element_type"] == pt], key=lambda e: -f(e.get("ep_next")))
        ep_top |= {e["id"] for e in pool[:8]}

    players = []
    for e in els:
        mins = e.get("minutes", 0); price = e.get("now_cost", 0); pos = POS[e["element_type"]]
        form = f(e.get("form")); ep = f(e.get("ep_next")); xgi = f(e.get("expected_goal_involvements_per_90"))
        dc90 = f(e.get("defensive_contribution_per_90")); saves = e.get("saves", 0) or 0
        cs = e.get("clean_sheets", 0) or 0; ccs = e.get("cost_change_start", 0) or 0
        flags = []
        if e["id"] in form_top20: flags.append("form_top20")
        if e["id"] in ep_top: flags.append("ep_top_pos")
        if price <= T["value_gem_price"] and form >= T["value_gem_form"] and mins >= T["value_gem_min"]: flags.append("value_gem")
        if ccs >= T["price_rising_change"] and form >= T["price_rising_form"] and mins >= T["price_rising_min"]: flags.append("price_rising")
        if pos in ("GK", "DEF") and cs >= T["cs_clean"] and mins >= T["cs_min"]: flags.append("cs_candidate")
        if xgi >= T["xgi_elite"] and mins >= T["xgi_elite_min"]: flags.append("xgi_elite")
        if xgi >= T["xgi_value"] and price <= T["xgi_value_price"] and mins >= T["xgi_value_min"]: flags.append("xgi_value")
        if pos in ("DEF", "MID") and dc90 >= T["cbit"] and mins >= T["cbit_min"]: flags.append("cbit_strong")
        if e.get("corners_and_indirect_freekicks_order") == 1 and xgi >= T["corner_xgi"]: flags.append("corner_taker")
        if e.get("penalties_order") == 1 and mins >= T["pen_min"]: flags.append("penalty_taker")
        if pos == "GK" and mins >= T["gk_min"] and (saves / mins * 90) >= T["gk_saves90"]: flags.append("gk_shot_stopper")
        if (mins / 38.0) <= T["rot_avg"] and mins >= T["rot_total"]:
            flags.append("rotation_risk")
        # keep ALL players (minutes>0) so the scatter can show the field; the list view filters to flagged
        players.append({"id": e["id"], "name": e.get("web_name"), "code": short[e["team"]], "pos": pos,
                        "price": round(price / 10.0, 1), "min": mins, "form": round(form, 1),
                        "ep": round(ep, 1), "xgi": round(xgi, 2), "dc90": round(dc90, 1),
                        "xg90": round(f(e.get("expected_goals_per_90")), 2), "xa90": round(f(e.get("expected_assists_per_90")), 2),
                        "g": e.get("goals_scored", 0) or 0, "a": e.get("assists", 0) or 0,
                        "sel": f(e.get("selected_by_percent")),
                        "tp": e.get("total_points", 0) or 0, "ppg": f(e.get("points_per_game")),
                        "cs": cs, "gc": e.get("goals_conceded", 0) or 0, "sv": saves,
                        "xgc": round(f(e.get("expected_goals_conceded_per_90")), 2),
                        "val": f(e.get("value_season")), "bps": e.get("bps", 0) or 0,
                        "st": e.get("status", "a"), "cop": e.get("chance_of_playing_next_round"),
                        "news": (e.get("news") or "").strip(),
                        "flags": flags, "flag_count": len([x for x in flags if x != "rotation_risk"])})
    players.sort(key=lambda p: (-p["flag_count"], -p["form"]))

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "event": event, "players": players}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    nflag = sum(1 for p in players if p["flag_count"] >= 1)
    print(f"wrote {OUT}: {len(players)} players ({nflag} flagged) GW{event}; max flags {players[0]['flag_count'] if players else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
