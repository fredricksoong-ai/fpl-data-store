#!/usr/bin/env python3
"""Build recommend.json — model-ranked transfer targets ("who best to pick").

Crosses the Dixon-Coles model's per-player expected points for the upcoming gameweek
(via engine/fpl_points) with the autoflag conviction from players.json. For each
position it ranks players by model xPts and annotates each with their signal flags,
price, the upcoming opponent and ownership — a buy board driven by the model.

Output: {generated, event, positions:{GK:[...],DEF:[...],MID:[...],FWD:[...]}}
Needs pandas, numpy, scipy. Reads players.json (built earlier in the same run) for flags.
"""
from __future__ import annotations
import csv, glob, json, os, sys, urllib.request, datetime as dt
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
from engine import dixon_coles as dc, fpl_points as fp
from engine.data_pl import _norm, load_fd_csv

API = "https://fantasy.premierleague.com/api"
OUT = Path(os.environ.get("REC_OUT", "recommend.json"))
TOPN = int(os.environ.get("REC_TOPN", "15"))

CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}
NAME2CODE = {_norm(v): k for k, v in CODE2FD.items()}
POSFIX = {"GKP": "GK", "GK": "GK", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main() -> int:
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    sel = {e["id"]: e for e in boot["elements"]}
    fixtures = get("/fixtures/")
    sched = [f for f in fixtures if f.get("event")]
    unfinished = sorted({f["event"] for f in sched if not f.get("finished")})

    if unfinished:
        target = unfinished[0]
        gwfix = [f for f in sched if f["event"] == target]
        pairs = [(short[f["team_h"]], short[f["team_a"]]) for f in gwfix]
        kos = [pd.to_datetime(f["kickoff_time"]).tz_localize(None) for f in gwfix if f.get("kickoff_time")]
    else:
        target = 1
        rows = [r for r in csv.DictReader(open(HERE / "data" / "Fixtures-2026-27.csv")) if int(r["gw"]) == 1]
        pairs = [(r["home"], r["away"]) for r in rows]
        kos = []

    opp = {}
    for h, a in pairs:
        opp[h] = (a, "H"); opp[a] = (h, "A")

    hist = pd.concat([load_fd_csv(f) for f in sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))],
                     ignore_index=True).sort_values("date").reset_index(drop=True)
    cutoff = min(kos) if kos else hist.date.max() + pd.Timedelta(days=1)
    model = dc.fit(hist[hist.date < cutoff], xi=0.0019)

    gw_fx = pd.DataFrame([{"home_team": _norm(CODE2FD.get(h, h)), "away_team": _norm(CODE2FD.get(a, a))}
                          for h, a in pairs if _norm(CODE2FD.get(h, h)) in model.attack and _norm(CODE2FD.get(a, a)) in model.attack])
    proj = fp.gameweek_points(model, fp.build_players(boot), gw_fx)

    try:
        flags = {p["id"]: p for p in json.loads((OUT.parent / "players.json").read_text())["players"]}
    except Exception:
        flags = {}

    positions = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for r in proj.itertuples():
        pos = POSFIX.get(r.pos, r.pos)
        if pos not in positions:
            continue
        code = NAME2CODE.get(r.team, r.team)
        o = opp.get(code)
        fl = flags.get(int(r.id), {})
        el = sel.get(int(r.id), {})
        positions[pos].append({
            "id": int(r.id), "name": r.name, "code": code, "pos": pos,
            "price": round(float(r.price), 1), "xpts": round(float(r.xpts), 2),
            "cs": round(float(r.cs_prob), 2), "opp": o[0] if o else "", "venue": o[1] if o else "",
            "flags": fl.get("flags", []), "flag_count": fl.get("flag_count", 0),
            "sel": float(el.get("selected_by_percent", 0) or 0),
        })
    for pos in positions:
        positions[pos] = sorted(positions[pos], key=lambda p: (-p["xpts"], -p["flag_count"]))[:TOPN]

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "event": target, "positions": positions}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: GW{target}; " + ", ".join(f"{k} {len(v)}" for k, v in positions.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
