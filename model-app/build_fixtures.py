#!/usr/bin/env python3
"""Build fixtures.json — the next gameweek's fixtures with the model's read, for the Fixtures page.

For each fixture in the target gameweek the Dixon-Coles + Elo ensemble gives win/draw/
loss probs and xG; recent results give a form strip. If the match is finished its actual
score is included. (Superseded the old Picks builder: the scoreline-prediction game and the
per-model breakdown were dropped, so this no longer computes top scorelines or reads odds.)

Target GW: $FIXTURES_GW if set (handy for testing a past GW), else the next unfinished GW
from the live FPL API, else GW1 from the static schedule when no season is loaded yet.

Emits: {generated, season, event, fixtures:[{gw,home,away,kickoff,ph,pd,pa,xgh,xga,
        formH:[...],formA:[...],finished,gh,ga}, ...]}
Needs pandas, numpy, scipy.
"""
from __future__ import annotations
import csv, glob, json, os, sys, datetime as dt, urllib.request
from pathlib import Path
import pandas as pd, numpy as np

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
from engine import dixon_coles as dc, elo as E
from engine.data_pl import _norm, load_fd_csv

API = "https://fantasy.premierleague.com/api"
OUT = Path(os.environ.get("FIXTURES_OUT", "fixtures.json"))
FORCE_GW = int(os.environ["FIXTURES_GW"]) if os.environ.get("FIXTURES_GW") else None

CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}


# Official 2026/27 GW1 kickoff times (UTC; BST = UTC+1), used pre-season when the FPL API
# has no live fixtures yet so the Fixtures page can group by date and show SGT times.
# Once the API serves the live season, kickoff_time from /fixtures/ takes over automatically.
GW1_KICKOFF = {
    "ARS|COV": "2026-08-21T19:00:00Z",
    "HUL|MUN": "2026-08-22T11:30:00Z",
    "NFO|LEE": "2026-08-22T14:00:00Z",
    "EVE|CRY": "2026-08-22T14:00:00Z",
    "IPS|SUN": "2026-08-22T14:00:00Z",
    "BRE|TOT": "2026-08-22T16:30:00Z",
    "MCI|BOU": "2026-08-23T13:00:00Z",
    "BHA|AVL": "2026-08-23T13:00:00Z",
    "NEW|LIV": "2026-08-23T15:30:00Z",
    "FUL|CHE": "2026-08-24T19:00:00Z",
}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def form_strip(hist, team, before, n=5):
    g = hist[(hist.date < before) & ((hist.home_team == team) | (hist.away_team == team))].tail(n)
    out = []
    for r in g.itertuples():
        gf, ga = (r.home_score, r.away_score) if r.home_team == team else (r.away_score, r.home_score)
        out.append("W" if gf > ga else "L" if gf < ga else "D")
    return out


def main() -> int:
    files = sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))
    hist = pd.concat([load_fd_csv(f) for f in files], ignore_index=True).sort_values("date").reset_index(drop=True)

    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    fixtures = get("/fixtures/")
    sched = [f for f in fixtures if f.get("event")]
    unfinished = sorted({f["event"] for f in sched if not f.get("finished")})

    use_fpl = bool(sched)
    if FORCE_GW is not None:
        target = FORCE_GW
    elif unfinished:
        target = unfinished[0]
    else:
        target = 1
        use_fpl = False  # no live season → static GW1

    if use_fpl:
        gw_fix = [f for f in sched if f["event"] == target]
        matches = [{"home": short[f["team_h"]], "away": short[f["team_a"]],
                    "kickoff": f.get("kickoff_time"),
                    "finished": bool(f.get("finished")) and f.get("team_h_score") is not None,
                    "gh": f.get("team_h_score"), "ga": f.get("team_a_score")} for f in gw_fix]
    else:
        srows = [r for r in csv.DictReader(open(HERE / "data" / "Fixtures-2026-27.csv")) if int(r["gw"]) == target]
        matches = [{"home": r["home"], "away": r["away"],
                    "kickoff": GW1_KICKOFF.get(f"{r['home']}|{r['away']}") if target == 1 else None,
                    "finished": False, "gh": None, "ga": None} for r in srows]

    # train through the day before the gameweek's first kickoff (no leakage for past-GW tests)
    kos = [m["kickoff"] for m in matches if m["kickoff"]]
    cutoff = pd.to_datetime(min(kos)).tz_convert(None) if kos else hist.date.max() + pd.Timedelta(days=1)
    train = hist[hist.date < cutoff]
    model = dc.fit(train, xi=0.0019)
    elom = E.fit(train)

    rows = []
    for m in matches:
        hk, ak = _norm(CODE2FD.get(m["home"], m["home"])), _norm(CODE2FD.get(m["away"], m["away"]))
        if hk not in model.attack or ak not in model.attack:
            continue
        lh, la = model.expected_goals(hk, ak, neutral=False)
        pen = E.ensemble_probs(model.outcome_probs(hk, ak, neutral=False),
                               elom.outcome_probs(hk, ak, neutral=False), w=0.45)
        rows.append({
            "gw": target, "home": m["home"], "away": m["away"], "kickoff": m["kickoff"],
            "ph": round(pen["home"], 3), "pd": round(pen["draw"], 3), "pa": round(pen["away"], 3),
            "xgh": round(lh, 2), "xga": round(la, 2),
            "formH": form_strip(train, hk, cutoff), "formA": form_strip(train, ak, cutoff),
            "finished": m["finished"], "gh": m["gh"], "ga": m["ga"],
        })

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "season": "2026-27", "event": target, "fixtures": rows}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: GW{target}, {len(rows)} fixtures ({'FPL' if use_fpl else 'static'} source)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
