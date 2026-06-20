#!/usr/bin/env python3
"""Build odds.json — de-vigged bookmaker market probabilities for PL matches.

Mirrors the WC26 odds cadence: the model rebuilds every 3h but the *paid* call is
throttled. We fetch only when forced (manual refresh), when the carried-forward
odds.json is >=12h old, or when a scheduled 6pm/10pm SGT window has passed since the
last fetch — otherwise the existing snapshot is reused and no credit is spent. The
workflow pulls the live odds.json forward before this runs so the age is visible.

Source: The Odds API (soccer_epl, h2h). De-vig each book, average for a consensus.
Output: {fetched_at, credits, events:{"HOME|AWAY":{ph,pd,pa,books}}} keyed by 3-letter codes.
Fail-soft: any error keeps the existing snapshot and exits 0 — the model works without odds.
"""
from __future__ import annotations
import json, os, statistics as st, urllib.parse, urllib.request, datetime as dt
from datetime import timezone, timedelta
from pathlib import Path

OUT = Path(os.environ.get("ODDS_OUT", "odds.json"))
KEY = os.environ.get("ODDS_API_KEY", "")
FORCE = bool(os.environ.get("FORCE_ODDS"))
API = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds/"
SGT = timezone(timedelta(hours=8))
ODDS_FLOOR_H = 12
ODDS_TARGETS_SGT = [18, 22]

# The Odds API full names -> 3-letter code
NAME2CODE = {
    "arsenal": "ARS", "aston villa": "AVL", "bournemouth": "BOU", "afc bournemouth": "BOU",
    "brentford": "BRE", "brighton and hove albion": "BHA", "brighton": "BHA", "chelsea": "CHE",
    "coventry city": "COV", "coventry": "COV", "crystal palace": "CRY", "everton": "EVE",
    "fulham": "FUL", "hull city": "HUL", "hull": "HUL", "ipswich town": "IPS", "ipswich": "IPS",
    "leeds united": "LEE", "leeds": "LEE", "liverpool": "LIV", "manchester city": "MCI",
    "manchester united": "MUN", "newcastle united": "NEW", "newcastle": "NEW",
    "nottingham forest": "NFO", "sunderland": "SUN", "tottenham hotspur": "TOT", "tottenham": "TOT",
}


def code_for(name):
    n = (name or "").strip().lower()
    return NAME2CODE.get(n)


def last_fetch():
    try:
        return dt.datetime.fromisoformat(json.loads(OUT.read_text())["fetched_at"])
    except Exception:
        return None


def should_fetch():
    if FORCE:
        return True, "manual refresh — forced"
    last = last_fetch()
    if last is None:
        return True, "no prior snapshot"
    now = dt.datetime.now(timezone.utc)
    age_h = (now - last).total_seconds() / 3600.0
    if age_h >= ODDS_FLOOR_H:
        return True, f"{age_h:.1f}h old (>{ODDS_FLOOR_H}h floor)"
    now_sgt = now.astimezone(SGT)
    passed = []
    for h in ODDS_TARGETS_SGT:
        t = now_sgt.replace(hour=h, minute=0, second=0, microsecond=0)
        passed += [t, t - timedelta(days=1)]
    last_target = max(t for t in passed if t <= now_sgt)
    if last.astimezone(SGT) < last_target:
        return True, f"scheduled {last_target.strftime('%H:%M')} SGT window"
    return False, f"{age_h:.1f}h old, already fetched since last window"


def devig(h, d, a):
    inv = [1.0 / p for p in (h, d, a) if p and p > 1]
    if len(inv) != 3:
        return None
    s = sum(inv)
    return [v / s for v in inv]


def main() -> int:
    if not KEY:
        print("  (no ODDS_API_KEY — skipping market)")
        return 0
    go, why = should_fetch()
    if not go:
        print(f"  odds.json reused ({why}) — no credit spent")
        return 0
    try:
        url = API + "?" + urllib.parse.urlencode({"regions": "eu", "markets": "h2h", "oddsFormat": "decimal", "apiKey": KEY})
        req = urllib.request.Request(url, headers={"User-Agent": "fplanner"})
        resp = urllib.request.urlopen(req, timeout=30)
        events = json.loads(resp.read())
        credits = resp.headers.get("x-requests-remaining")
        out = {}
        for ev in events:
            hc, ac = code_for(ev.get("home_team")), code_for(ev.get("away_team"))
            if not hc or not ac:
                continue
            hs, ds, as_ = [], [], []
            for bk in ev.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    price = {o.get("name"): o.get("price") for o in mkt.get("outcomes", [])}
                    dv = devig(price.get(ev["home_team"]), price.get("Draw"), price.get(ev["away_team"]))
                    if dv:
                        hs.append(dv[0]); ds.append(dv[1]); as_.append(dv[2])
            if hs:
                out[f"{hc}|{ac}"] = {"ph": round(st.mean(hs), 4), "pd": round(st.mean(ds), 4),
                                     "pa": round(st.mean(as_), 4), "books": len(hs)}
        OUT.write_text(json.dumps({"fetched_at": dt.datetime.now(timezone.utc).isoformat(),
                                   "credits": credits, "events": out}, ensure_ascii=False))
        print(f"  ok  odds.json [{why}]  {len(out)} matches priced | {credits} credits left")
    except Exception as e:
        print(f"  WARN odds skipped (kept existing): {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
