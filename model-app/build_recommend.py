#!/usr/bin/env python3
"""Build recommend.json — model-ranked transfer targets over a multi-GW horizon.

Crosses the Dixon-Coles model's expected points, summed over the next H gameweeks
(default 5, $REC_HORIZON), with the autoflag conviction from players.json. Ranks each
position by horizon xPts and annotates flags, price, the immediate opponent and fixture
count. Also writes the horizon xPts back into players.json (field `xph`) so the player
chart and the squad comparison modal use the same steadier number.

Output: {generated, event, horizon, positions:{GK,DEF,MID,FWD:[...]}}
Needs pandas, numpy, scipy. Run after build_players.py in the same pass.
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
H = int(os.environ.get("REC_HORIZON", "5"))

CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}
NAME2CODE = {_norm(v): k for k, v in CODE2FD.items()}
POSFIX = {"GKP": "GK", "GK": "GK", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}


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


def in_model(model, h, a):
    return _norm(CODE2FD.get(h, h)) in model.attack and _norm(CODE2FD.get(a, a)) in model.attack


# --- Core-Insights enrichment tilt -------------------------------------------------
# A BOUNDED nudge on the model's xPts using the underlying technical layer. The Dixon-Coles
# model stays authoritative; this only leans it toward what the underlying numbers say:
#   1. xG-regression — fade players whose recent returns overperformed their npxG+xA (en_ovp).
#   2. DefCon floor  — reliable defensive-action hitters bank the +2 more often (en_dc90).
#   3. GK quality    — shot-stoppers who prevent goals above expectation (en_gp).
# Prior-season data counts at half weight; total tilt is hard-capped so the model can't be swamped.
DC_LINE = {"DEF": 10.0, "MID": 12.0, "FWD": 12.0}   # FPL 2026/27 DefCon action thresholds
TILT_CAP = 0.12


def enrich_mult(fl):
    """Return (multiplier, tilt) for a players.json record carrying en_* enrichment fields."""
    if not fl or not fl.get("en_src"):
        return 1.0, None
    w = 0.5 if fl.get("en_src") == "prior" else 1.0
    m = 0.0
    ovp = fl.get("en_ovp")
    if ovp is not None:
        m += -max(-1.0, min(1.0, ovp / 4.0)) * 0.08          # regression to underlying xG
    dc90 = fl.get("en_dc90"); pos = fl.get("pos")
    if dc90 is not None and pos in DC_LINE and dc90 >= DC_LINE[pos]:
        m += 0.04                                            # reliable DefCon floor
    gp = fl.get("en_gp")
    if gp is not None and pos == "GK":
        m += max(-0.06, min(0.06, gp / 6.0 * 0.06))          # GK shot-stopping quality
    m = max(-TILT_CAP, min(TILT_CAP, m * w))
    return round(1.0 + m, 4), round(m, 4)


def main() -> int:
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    sel = {e["id"]: e for e in boot["elements"]}
    fixtures = get("/fixtures/")
    sched = [f for f in fixtures if f.get("event")]
    unfinished = sorted({f["event"] for f in sched if not f.get("finished")})
    # the actionable target is the next gw you can still pick for (deadline in the future),
    # NOT the in-progress one — once GW1 kicks off the plan should roll to GW2.
    now = dt.datetime.now(dt.timezone.utc)
    def _future(e):
        d = e.get("deadline_time")
        return bool(d) and dt.datetime.fromisoformat(d.replace("Z", "+00:00")) > now
    upcoming = sorted(e["id"] for e in boot["events"] if _future(e))

    if unfinished:
        target = upcoming[0] if upcoming else unfinished[0]
        hz = [g for g in unfinished if target <= g < target + H]
        nextfix = [f for f in sched if f["event"] == target]
        hzpairs = [(short[f["team_h"]], short[f["team_a"]]) for f in sched if f["event"] in hz and not f.get("finished")]
        kos = [pd.to_datetime(f["kickoff_time"]).tz_localize(None) for f in nextfix if f.get("kickoff_time")]
    else:
        target = 1
        rows = list(csv.DictReader(open(HERE / "data" / "Fixtures-2026-27.csv")))
        nextfix = None
        nextpairs = [(r["home"], r["away"]) for r in rows if int(r["gw"]) == target]
        hzpairs = [(r["home"], r["away"]) for r in rows if target <= int(r["gw"]) < target + H]
        kos = []

    # immediate opponent (next GW only) for display
    npairs = [(short[f["team_h"]], short[f["team_a"]]) for f in nextfix] if nextfix else nextpairs
    opp = {}
    for h, a in npairs:
        opp[h] = (a, "H"); opp[a] = (h, "A")

    hist = pd.concat([load_fd_csv(f) for f in sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))],
                     ignore_index=True).sort_values("date").reset_index(drop=True)
    cutoff = min(kos) if kos else hist.date.max() + pd.Timedelta(days=1)
    model = dc.fit(hist[hist.date < cutoff], xi=0.0019)

    players_df = fp.build_players(boot)
    gw_fx = pd.DataFrame([{"home_team": _norm(CODE2FD.get(h, h)), "away_team": _norm(CODE2FD.get(a, a))}
                          for h, a in hzpairs if in_model(model, h, a)])
    proj = fp.gameweek_points(model, players_df, gw_fx)  # xpts summed over the horizon fixtures
    # single next-GW projection too (captaincy is a one-week call)
    gw_fx1 = pd.DataFrame([{"home_team": _norm(CODE2FD.get(h, h)), "away_team": _norm(CODE2FD.get(a, a))}
                           for h, a in npairs if in_model(model, h, a)])
    xp1_by = {int(r.id): round(float(r.xpts), 2) for r in fp.gameweek_points(model, players_df, gw_fx1).itertuples()} if not gw_fx1.empty else {}

    try:
        flags = {p["id"]: p for p in json.loads((OUT.parent / "players.json").read_text())["players"]}
    except Exception:
        flags = {}

    positions = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    xph_by = {}; mult_by = {}
    for r in proj.itertuples():
        pos = POSFIX.get(r.pos, r.pos)
        fl = flags.get(int(r.id), {})
        mult, tilt = enrich_mult(fl); mult_by[int(r.id)] = mult
        xph_by[int(r.id)] = round(float(r.xpts) * mult, 2)   # enrichment-tilted horizon xPts
        if pos not in positions:
            continue
        code = NAME2CODE.get(r.team, r.team)
        o = opp.get(code); el = sel.get(int(r.id), {})
        positions[pos].append({
            "id": int(r.id), "name": r.name, "code": code, "pos": pos,
            "price": round(float(r.price), 1), "xph": round(float(r.xpts) * mult, 2),
            "xph_m": round(float(r.xpts), 2), "en_tilt": tilt, "nfix": int(r.n_fix),
            "opp": o[0] if o else "", "venue": o[1] if o else "",
            "flags": fl.get("flags", []), "flag_count": fl.get("flag_count", 0),
            "sel": float(el.get("selected_by_percent", 0) or 0),
        })
    for pos in positions:
        positions[pos] = sorted(positions[pos], key=lambda p: (-p["xph"], -p["flag_count"]))[:TOPN]

    # write the horizon xPts back into players.json so the modal + chart share it
    try:
        pj = json.loads((OUT.parent / "players.json").read_text())
        for p in pj["players"]:
            mult = mult_by.get(p["id"], 1.0)
            xp1r = xp1_by.get(p["id"])
            p["xph"] = xph_by.get(p["id"])                                  # already tilted
            p["xp1"] = round(xp1r * mult, 2) if xp1r is not None else None  # tilt the 1-GW too
            p["en_tilt"] = round(mult - 1.0, 4) if mult != 1.0 else None
        (OUT.parent / "players.json").write_text(json.dumps(pj, ensure_ascii=False))
    except Exception as e:
        print("  could not merge xph into players.json:", e)

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "event": target, "horizon": len(set(hz)) if unfinished else H, "positions": positions}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: GW{target}+{H}; " + ", ".join(f"{k} {len(v)}" for k, v in positions.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
