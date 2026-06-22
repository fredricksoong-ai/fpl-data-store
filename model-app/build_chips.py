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


def assign_unique(cands):
    """Assign each chip a distinct GW. cands: [(key, [(gw, score), ...desc])].
    A chip can only be played one-per-GW, so when two chips want the same week the
    one that loses more by moving (higher regret = gap to its next choice) keeps it;
    the other drops to its next-best free week. Returns {key: gw}."""
    order = sorted(cands, key=lambda c: -((c[1][0][1] - c[1][1][1]) if len(c[1]) > 1 else 1e18))
    taken, result = set(), {}
    for key, ranked in order:
        pick = next((gw for gw, _ in ranked if gw not in taken), ranked[0][0] if ranked else None)
        if pick is not None:
            taken.add(pick)
        result[key] = pick
    return result


def bestxv_ids():
    try:
        sq = json.loads((OUT.parent / "squad.json").read_text())
        return sq.get("ids") or None
    except Exception:
        return None


def my_ids(boot):
    cur = [e["id"] for e in boot["events"] if e.get("is_current") or e.get("finished")]
    for ev in ([cur[-1]] if cur else []) + [e["id"] for e in boot["events"] if e.get("is_next")]:
        try:
            picks = get(f"/entry/{MY_ENTRY}/event/{ev}/picks/")["picks"]
            if picks:
                return [p["element"] for p in picks]
        except Exception:
            pass
    return None


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

    hist = pd.concat([load_fd_csv(f) for f in sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))],
                     ignore_index=True).sort_values("date").reset_index(drop=True)
    cutoff = min(kos_first.get(target, [])) if kos_first.get(target) else hist.date.max() + pd.Timedelta(days=1)
    model = dc.fit(hist[hist.date < cutoff], xi=0.0019)
    players = fp.build_players(boot)

    # one projection pass; store per-GW per-player rows so we can score any squad
    projs, dgw, bgw = {}, [], []
    for g in window:
        pairs = gw_pairs[g]; cnt = Counter()
        for h, a in pairs:
            cnt[h] += 1; cnt[a] += 1
        if any(v >= 2 for v in cnt.values()): dgw.append(g)
        if len(cnt) < nteams: bgw.append(g)
        gw_fx = pd.DataFrame([{"home_team": _norm(CODE2FD.get(h, h)), "away_team": _norm(CODE2FD.get(a, a))}
                              for h, a in pairs if _norm(CODE2FD.get(h, h)) in model.attack and _norm(CODE2FD.get(a, a)) in model.attack])
        projs[g] = [] if gw_fx.empty else [(int(r.id), r.name, NAME2CODE.get(r.team, r.team), float(r.xpts))
                                           for r in fp.gameweek_points(model, players, gw_fx).itertuples()]

    def captain_for(g, code):
        for h, a in gw_pairs[g]:
            if h == code: return f"{a} (H)"
            if a == code: return f"{h} (A)"
        return ""

    def plan_for(ids, basis):
        if not ids:
            return None
        S = set(ids); bb, tc, xi, top = {}, {}, {}, {}
        for g in window:
            rows = sorted([r for r in projs[g] if r[0] in S], key=lambda r: -r[3])
            xs = [r[3] for r in rows]
            bb[g] = round(sum(xs), 1); xi[g] = round(sum(xs[:11]), 1); tc[g] = round(xs[0], 1) if xs else 0.0
            top[g] = rows[0] if rows else None
        # candidate GW rankings per chip (doubles/blanks given a dominating bonus)
        def ranked(scoref, pool=None):
            return sorted([(g, scoref(g)) for g in (pool or window)], key=lambda x: -x[1])
        tc_c = ranked(lambda g: tc[g] + (1e6 if g in dgw else 0))
        bb_c = ranked(lambda g: bb[g] + (1e6 if g in dgw else 0))
        fh_c = ranked(lambda g: (1e9 if g in bgw else 0) - xi[g])
        wc_pool = [g for g in window if g + 3 <= half_end] or window
        wc_c = ranked(lambda g: sum(bb.get(g + k, 0) for k in range(4)), wc_pool)
        a = assign_unique([("TC", tc_c), ("BB", bb_c), ("FH", fh_c), ("WC", wc_c)])
        tc_gw, bb_gw, fh_gw, wc_gw = a["TC"], a["BB"], a["FH"], a["WC"]
        cap = (lambda g: (f"{top[g][1]} vs {captain_for(g, top[g][2])}" if top.get(g) else ""))
        chips = [
            {"chip": "WC", "label": "Wildcard", "advice": f"GW{wc_gw}", "gw": wc_gw,
             "note": f"{basis.capitalize()}'s best sustained run starts ~GW{wc_gw} — wildcard in to load up. First-half chip expires after GW19."},
            {"chip": "FH", "label": "Free Hit", "advice": f"GW{fh_gw}", "gw": fh_gw,
             "note": (f"GW{fh_gw} is a blank — Free Hit a full XI through it." if fh_gw in bgw else f"No blank scheduled; GW{fh_gw} is {basis}'s weakest projected XI week to bypass.")},
            {"chip": "BB", "label": "Bench Boost", "advice": f"GW{bb_gw}", "gw": bb_gw,
             "note": (f"GW{bb_gw} is a double — most points across {basis}." if bb_gw in dgw else f"GW{bb_gw}: {basis}'s strongest all-15 fixtures (incl. bench).")},
            {"chip": "TC", "label": "Triple Captain", "advice": f"GW{tc_gw}", "gw": tc_gw, "who": cap(tc_gw),
             "note": (f"GW{tc_gw} is a double — triple {cap(tc_gw)}." if tc_gw in dgw else f"Triple-captain {cap(tc_gw)} in GW{tc_gw} — {basis}'s best captain fixture (proj {tc[tc_gw]} pts).")},
        ]
        chips.sort(key=lambda c: c["gw"])
        return {"basis": basis, "chips": chips}

    bestxv = plan_for(bestxv_ids(), "your Best XV")
    squad = plan_for(my_ids(boot), "your squad")
    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "half": 1 if target <= 19 else 2, "window": [window[0], window[-1]],
           "bestxv": bestxv, "squad": squad}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: GW{window[0]}-{window[-1]} | bestxv={'y' if bestxv else 'n'} squad={'y' if squad else 'n'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
