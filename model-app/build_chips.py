#!/usr/bin/env python3
"""Build chips.json — a fixture-driven chip plan based on YOUR squad.

Chip value is relative to the players you own, so the plan keys off your squad:
your live FPL team in-season, and your Best XV (squad.json) as the stand-in pre-season.
It projects every gameweek in the current half with the Dixon-Coles model, restricts to
your 15, and picks the model-best week for each chip:

  Triple Captain  -> the week your best captain option projects highest (named)
  Bench Boost     -> the week your whole 15 (incl. bench) projects highest
  Free Hit        -> a blank GW if any, else the week your XI projects weakest (bypass it)
  Wildcard        -> the start of your squad's best sustained run

Doubles/blanks, once announced, override TC/BB (-> the double) and FH (-> the blank).

Output: {generated, half, window, basis, chips:[{chip,label,advice,who,note}]}. Needs pandas/numpy/scipy.
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
MY_ENTRY = int(os.environ.get("FPL_ENTRY", "822500"))
CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}
NAME2CODE = {_norm(v): k for k, v in CODE2FD.items()}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def resolve_squad(boot):
    """(ids, basis): live FPL picks if available, else Best XV, else league top-15."""
    cur = [e["id"] for e in boot["events"] if e.get("is_current") or e.get("finished")]
    for ev in ([cur[-1]] if cur else []) + [e["id"] for e in boot["events"] if e.get("is_next")]:
        try:
            picks = get(f"/entry/{MY_ENTRY}/event/{ev}/picks/")["picks"]
            if picks:
                return [p["element"] for p in picks], "your squad"
        except Exception:
            pass
    try:
        sq = json.loads((OUT.parent / "squad.json").read_text())
        if sq.get("ids"):
            return sq["ids"], "your Best XV"
    except Exception:
        pass
    return None, "the league's top 15"


def main() -> int:
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}; nteams = len(boot["teams"])
    sched = [f for f in get("/fixtures/") if f.get("event")]
    un = sorted({f["event"] for f in sched if not f.get("finished")})

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
    if not window:
        OUT.write_text(json.dumps({"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                   "half": 0, "chips": []}, ensure_ascii=False))
        print("no upcoming gameweeks for a chip plan"); return 0

    sq_ids, basis = resolve_squad(boot)
    sq_set = set(sq_ids) if sq_ids else None

    hist = pd.concat([load_fd_csv(f) for f in sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))],
                     ignore_index=True).sort_values("date").reset_index(drop=True)
    cutoff = min(kos_first.get(target, [])) if kos_first.get(target) else hist.date.max() + pd.Timedelta(days=1)
    model = dc.fit(hist[hist.date < cutoff], xi=0.0019)
    players = fp.build_players(boot)

    bb, tc, xi, dgw, bgw, top = {}, {}, {}, [], [], {}
    for g in window:
        pairs = gw_pairs[g]; cnt = Counter()
        for h, a in pairs:
            cnt[h] += 1; cnt[a] += 1
        if any(v >= 2 for v in cnt.values()): dgw.append(g)
        if len(cnt) < nteams: bgw.append(g)
        gw_fx = pd.DataFrame([{"home_team": _norm(CODE2FD.get(h, h)), "away_team": _norm(CODE2FD.get(a, a))}
                              for h, a in pairs if _norm(CODE2FD.get(h, h)) in model.attack and _norm(CODE2FD.get(a, a)) in model.attack])
        if gw_fx.empty: bb[g] = tc[g] = xi[g] = 0.0; continue
        proj = fp.gameweek_points(model, players, gw_fx)
        sub = proj[proj["id"].isin(sq_set)] if sq_set else proj
        sub = sub.sort_values("xpts", ascending=False)
        xs = list(sub["xpts"])
        bb[g] = round(float(sum(xs)), 1)              # whole squad (incl. bench)
        xi[g] = round(float(sum(xs[:11])), 1)         # best 11 of the squad (your XI)
        tc[g] = round(float(xs[0]), 1) if xs else 0.0  # your best captain
        r0 = sub.iloc[0] if len(sub) else None
        top[g] = (r0["name"], NAME2CODE.get(r0["team"], r0["team"])) if r0 is not None else None

    def captain_for(g):
        if not top.get(g): return ""
        nm, code = top[g]; ov = ""
        for h, a in gw_pairs[g]:
            if h == code: ov = f"{a} (H)"; break
            if a == code: ov = f"{h} (A)"; break
        return f"{nm} vs {ov}" if ov else nm

    tc_gw = dgw[0] if dgw else max(window, key=lambda g: tc[g])
    bb_gw = dgw[0] if dgw else max(window, key=lambda g: bb[g])
    fh_gw = bgw[0] if bgw else min(window, key=lambda g: xi[g])
    runs = [(g, sum(bb.get(g + k, 0) for k in range(4))) for g in window if g + 3 <= half_end]
    wc_gw = max(runs, key=lambda x: x[1])[0] if runs else window[0]
    bw = basis  # short alias for notes

    chips = [
        {"chip": "WC", "label": "Wildcard", "advice": f"GW{wc_gw}",
         "note": f"{bw.capitalize()}'s best sustained run starts ~GW{wc_gw} — wildcard in to load up on it. First-half chip expires after GW19."},
        {"chip": "FH", "label": "Free Hit", "advice": f"GW{fh_gw}",
         "note": (f"GW{fh_gw} is a blank — Free Hit a full XI through it." if bgw else f"No blank scheduled; GW{fh_gw} is {bw}'s weakest projected XI week to bypass.")},
        {"chip": "BB", "label": "Bench Boost", "advice": f"GW{bb_gw}",
         "note": (f"GW{bb_gw} is a double — most points across {bw}." if dgw else f"GW{bb_gw}: {bw}'s strongest all-15 fixtures (incl. bench).")},
        {"chip": "TC", "label": "Triple Captain", "advice": f"GW{tc_gw}", "who": captain_for(tc_gw),
         "note": (f"GW{tc_gw} is a double — triple {captain_for(tc_gw)}." if dgw else f"Triple-captain {captain_for(tc_gw)} in GW{tc_gw} — {bw}'s best captain fixture (proj {tc[tc_gw]} pts).")},
    ]
    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "half": 1 if target <= 19 else 2, "window": [window[0], window[-1]], "basis": basis, "chips": chips}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: basis={basis} GW{window[0]}-{window[-1]} | WC{wc_gw} FH{fh_gw} BB{bb_gw} TC{tc_gw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
