#!/usr/bin/env python3
"""Build v2.json — live data for the FPLanner v2 UI, straight from the FPL API.

Assembles exactly the structures the v2 app renders:
  squads[teamName] = [[type,name,gwPts,own,played,C,VC,bench,opp,ko,goals,assists,transferIn], ...]
  stats[teamName]  = {gw,avg,gwr,up,ml,gap,val,bank,op,ovr,ovrUp}
  league           = [[rank,team,manager,captain,chipNow,chipsUsed,ovr,gwPts,total,gap,xPts,xGI,isMe], ...]

xPts = sum of the XI's FPL ep_next; xGI = mean of the XI's expected_goal_involvements_per_90.
(The Dixon–Coles model xPts can replace ep_next later; this gets the app live now.)

Run anywhere with internet (locally or in the GitHub Action). Writes ./v2.json by default,
or to $V2_OUT.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

API = "https://fantasy.premierleague.com/api"
MY_ENTRY = 822500
LEAGUE = 1822310
CHIP = {"bboost": "BB", "freehit": "FH", "wildcard": "WC", "3xc": "TC"}
OUT = Path(os.environ.get("V2_OUT", "v2.json"))


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def fmt_rank(n):
    if n is None: return "—"
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{round(n/1000)}K"
    return str(n)


def ordinal(n):
    return f"{n}{'th' if 11<=n%100<=13 else {1:'st',2:'nd',3:'rd'}.get(n%10,'th')}"


def main() -> int:
    boot = get("/bootstrap-static/")
    finished = [e for e in boot["events"] if e.get("finished")]
    gw = finished[-1]["id"] if finished else 1
    avg = (finished[-1].get("average_entry_score") if finished else 35) or 35

    el = {e["id"]: e for e in boot["elements"]}
    teams = {t["id"]: t for t in boot["teams"]}
    tshort = {t["id"]: t["short_name"] for t in boot["teams"]}

    # live per-player GW stats
    try:
        live = {x["id"]: x["stats"] for x in get(f"/event/{gw}/live/")["elements"]}
    except Exception:
        live = {}

    # this GW fixtures → per-team opponent / kickoff / finished
    fx = {f["id"]: f for f in get("/fixtures/") if f.get("event") == gw}
    team_fix = {}
    for f in fx.values():
        ko = ""
        if f.get("kickoff_time"):
            d = dt.datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8)))
            ko = d.strftime("%a %H%M")
        team_fix[f["team_h"]] = {"opp": tshort.get(f["team_a"], "?"), "ko": ko, "started": f.get("started"), "finished": f.get("finished"), "fdr": f.get("team_h_difficulty", 0)}
        team_fix[f["team_a"]] = {"opp": tshort.get(f["team_h"], "?"), "ko": ko, "started": f.get("started"), "finished": f.get("finished"), "fdr": f.get("team_a_difficulty", 0)}

    standings = get(f"/leagues-classic/{LEAGUE}/standings/")["standings"]["results"]
    leader_total = max((r["total"] for r in standings), default=0)

    squads, stats, league = {}, {}, []

    def picks_at(entry, ev):
        try: return get(f"/entry/{entry}/event/{ev}/picks/")
        except Exception: return None

    for r in standings:
        entry, name = r["entry"], r["entry_name"]
        pk = picks_at(entry, gw)
        if not pk:
            continue
        prev = picks_at(entry, gw - 1) if gw > 1 else None
        prev_ids = {p["element"] for p in prev["picks"]} if prev else set()
        hist = get(f"/entry/{entry}/history/")
        cur = {h["event"]: h for h in hist["current"]}
        this, last = cur.get(gw, {}), cur.get(gw - 1, {})

        rows, xi_ep, xi_xgi, cap_name = [], 0.0, [], ""
        for p in pk["picks"]:
            e = el.get(p["element"], {})
            st = live.get(p["element"], {})
            tf = team_fix.get(e.get("team"), {})
            mins = st.get("minutes", 0) or 0
            status = 0 if not tf.get("started") else (1 if mins > 0 else 2)  # 0 upcoming, 1 played, 2 DNP
            if p["is_captain"]:
                cap_name = e.get("web_name", "")
            rows.append([
                e.get("element_type", 1), e.get("web_name", "?"),
                int(st.get("total_points", e.get("event_points", 0)) or 0),
                float(e.get("selected_by_percent", 0) or 0),
                status,
                1 if p["is_captain"] else 0, 1 if p["is_vice_captain"] else 0,
                1 if p["position"] > 11 else 0,
                tf.get("opp", "?"), tf.get("ko", "") if status == 0 else "",
                int(st.get("goals_scored", 0) or 0), int(st.get("assists", 0) or 0),
                1 if (p["element"] not in prev_ids and prev_ids) else 0,
                round(float(e.get("ep_next", 0) or 0), 1),   # 13 xPts (model ep_next; DC model later)
                tf.get("fdr", 0),                            # 14 fixture difficulty 1–5
            ])
            if p["position"] <= 11:
                xi_ep += float(e.get("ep_next", 0) or 0)
                xi_xgi.append(float(e.get("expected_goal_involvements_per_90", 0) or 0))
        squads[name] = rows

        eh = pk["entry_history"]
        ovr = this.get("overall_rank")
        ovr_prev = last.get("overall_rank")
        gwr = this.get("rank")
        gwr_prev = last.get("rank")
        gap_to_leader = r["total"] - leader_total
        if r["rank"] == 1:
            second = sorted((x["total"] for x in standings), reverse=True)
            gap = f"+{second[0]-second[1]}" if len(second) > 1 else "—"
        else:
            gap = f"{gap_to_leader}"  # negative
        stats[name] = {
            "gw": this.get("points", eh.get("points", 0)), "avg": avg,
            "gwr": fmt_rank(gwr), "up": 1 if (gwr_prev and gwr and gwr < gwr_prev) else 0,
            "ml": ordinal(r["rank"]), "gap": gap,
            "val": f"£{eh.get('value',1000)/10:.1f}m", "bank": f"{eh.get('bank',0)/10:.1f}",
            "op": r["total"], "ovr": fmt_rank(ovr),
            "ovrUp": 1 if (ovr_prev and ovr and ovr < ovr_prev) else 0,
        }

        chips_cnt = {}
        for c in hist.get("chips", []):
            k = CHIP.get(c["name"], c["name"].upper())
            chips_cnt[k] = chips_cnt.get(k, 0) + 1
        league.append([
            r["rank"], name, r["player_name"], cap_name,
            CHIP.get(pk.get("active_chip"), ""),
            [[k, v] for k, v in chips_cnt.items()],
            fmt_rank(ovr), r["event_total"], r["total"],
            (leader_total - r["total"]) if r["rank"] != 1 else 0,
            round(xi_ep, 0), round(sum(xi_xgi)/len(xi_xgi), 1) if xi_xgi else 0.0,
            1 if entry == MY_ENTRY else 0,
        ])

    me = next((r["entry_name"] for r in standings if r["entry"] == MY_ENTRY), "wirtzplay")
    out = {"event": gw, "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "me": me, "squads": squads, "stats": stats, "league": league}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: GW{gw}, {len(squads)} squads, {len(league)} league rows, me={me}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
