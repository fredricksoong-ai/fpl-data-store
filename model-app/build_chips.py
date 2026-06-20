#!/usr/bin/env python3
"""Build chips.json — a fixture-driven chip plan for the current half-season.

FPL grants a full set of chips per half (the first-half set expires at GW19), and
doubles/blanks mostly fall in the second half — so first-half chips must be planned
off fixtures. This projects every gameweek in the current half with the Dixon-Coles
model and picks the model-best week for each chip:

  Triple Captain  -> the week a single premium has the highest projected haul
  Bench Boost     -> the week the strongest XV projects highest in aggregate
  Free Hit        -> a blank GW if any, else the squad's worst projected week (navigate it)
  Wildcard        -> the start of the best sustained run (load up for it)

Double/blank gameweeks, once announced, override TC/BB (-> the double) and FH (-> the blank).
Works pre-season off data/Fixtures-2026-27.csv; switches to live FPL fixtures in-season.

Output: {generated, half, chips:[{chip,label,advice,note}]}. Needs pandas, numpy, scipy.
"""
from __future__ import annotations
import csv, glob, json, os, sys, urllib.request, datetime as dt
from collections import Counter
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
from engine import dixon_coles as dc, fpl_points as fp
from engine.data_pl import _norm, load_fd_csv

API = "https://fantasy.premierleague.com/api"
OUT = Path(os.environ.get("CHIPS_OUT", "chips.json"))
CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}
NAME2CODE = {_norm(v): k for k, v in CODE2FD.items()}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main() -> int:
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}; nteams = len(boot["teams"])
    sched = [f for f in get("/fixtures/") if f.get("event")]
    un = sorted({f["event"] for f in sched if not f.get("finished")})

    # per-GW list of (home_code, away_code); live FPL if a season is loaded, else static
    gw_pairs, kos_first = {}, {}
    if un:
        target = un[0]
        for f in sched:
            if not f.get("finished"):
                gw_pairs.setdefault(f["event"], []).append((short[f["team_h"]], short[f["team_a"]]))
                if f.get("kickoff_time"):
                    kos_first.setdefault(f["event"], []).append(pd.to_datetime(f["kickoff_time"]).tz_localize(None))
    else:
        target = 1
        for r in csv.DictReader(open(HERE / "data" / "Fixtures-2026-27.csv")):
            gw_pairs.setdefault(int(r["gw"]), []).append((r["home"], r["away"]))

    half_end = 19 if target <= 19 else 38
    window = [g for g in sorted(gw_pairs) if target <= g <= half_end]

    hist = pd.concat([load_fd_csv(f) for f in sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))],
                     ignore_index=True).sort_values("date").reset_index(drop=True)
    cutoff = min(kos_first.get(target, [])) if kos_first.get(target) else hist.date.max() + pd.Timedelta(days=1)
    model = dc.fit(hist[hist.date < cutoff], xi=0.0019)
    players = fp.build_players(boot)

    tc, bb, dgw, bgw, top = {}, {}, [], [], {}
    for g in window:
        pairs = gw_pairs[g]
        cnt = Counter()
        for h, a in pairs:
            cnt[h] += 1; cnt[a] += 1
        if any(v >= 2 for v in cnt.values()):
            dgw.append(g)
        if len(cnt) < nteams:
            bgw.append(g)
        gw_fx = pd.DataFrame([{"home_team": _norm(CODE2FD.get(h, h)), "away_team": _norm(CODE2FD.get(a, a))}
                              for h, a in pairs if _norm(CODE2FD.get(h, h)) in model.attack and _norm(CODE2FD.get(a, a)) in model.attack])
        if gw_fx.empty:
            tc[g] = bb[g] = 0.0; continue
        proj = fp.gameweek_points(model, players, gw_fx).sort_values("xpts", ascending=False)
        xs = list(proj["xpts"])
        tc[g] = round(float(xs[0]), 1) if xs else 0.0           # best single captain that week
        bb[g] = round(float(sum(xs[:15])), 1) if xs else 0.0    # strong-XV aggregate haul proxy
        r0 = proj.iloc[0] if len(proj) else None
        top[g] = (r0["name"], NAME2CODE.get(r0["team"], r0["team"])) if r0 is not None else None


    def captain_for(g):
        if not top.get(g):
            return ""
        nm, code = top[g]
        ov = ""
        for h, a in gw_pairs[g]:
            if h == code: ov = f"{a} (H)"; break
            if a == code: ov = f"{h} (A)"; break
        return f"{nm} vs {ov}" if ov else nm

    if not window:
        OUT.write_text(json.dumps({"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                   "half": 0, "chips": []}, ensure_ascii=False))
        print("no upcoming gameweeks for a chip plan"); return 0

    tc_gw = dgw[0] if dgw else max(window, key=lambda g: tc[g])
    bb_gw = dgw[0] if dgw else max(window, key=lambda g: bb[g])
    fh_gw = bgw[0] if bgw else min(window, key=lambda g: bb[g])
    # wildcard: start of the best sustained 4-GW run (load up before it)
    runs = [(g, sum(bb.get(g + k, 0) for k in range(4))) for g in window if g + 3 <= half_end]
    wc_gw = max(runs, key=lambda x: x[1])[0] if runs else window[0]

    chips = [
        {"chip": "WC", "label": "Wildcard", "advice": f"GW{wc_gw}",
         "note": f"Best sustained run starts ~GW{wc_gw} — wildcard into it to load up on form fixtures. First-half chip expires after GW19."},
        {"chip": "FH", "label": "Free Hit", "advice": f"GW{fh_gw}",
         "note": (f"GW{fh_gw} is a blank — Free Hit a full XI through it." if bgw else f"No blank scheduled; GW{fh_gw} is the squad's toughest projected week to bypass.")},
        {"chip": "BB", "label": "Bench Boost", "advice": f"GW{bb_gw}",
         "note": (f"GW{bb_gw} is a double gameweek — most points on the table." if dgw else f"GW{bb_gw} projects the strongest all-15 fixtures in the half.")},
        {"chip": "TC", "label": "Triple Captain", "advice": f"GW{tc_gw}", "who": captain_for(tc_gw),
         "note": (f"GW{tc_gw} is a double — triple {captain_for(tc_gw)}, two fixtures." if dgw else f"Triple-captain {captain_for(tc_gw)} in GW{tc_gw} — the half's best single premium fixture (proj {tc[tc_gw]} pts).")},
    ]
    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "half": 1 if target <= 19 else 2, "window": [window[0], window[-1]], "chips": chips}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: half {out['half']} GW{window[0]}-{window[-1]} | WC{wc_gw} FH{fh_gw} BB{bb_gw} TC{tc_gw} | DGW {dgw} BGW {bgw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
