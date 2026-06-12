#!/usr/bin/env python3
"""
Generate AI insights for the wirtzplay FPL dashboard.

Reads the already-built JSON in ./live/, builds a small context, asks Gemini
for a short structured read, and writes ./live/insights.json.

- Runs server-side in GitHub Actions (key never reaches the browser).
- Skips the API call entirely if the underlying data hasn't changed (saves cost).
- Designed to fail soft: if anything goes wrong it exits 0 without touching the
  existing insights.json, so the dashboard keeps showing the last good read.

Env: GEMINI_API_KEY  (set as a GitHub Actions secret)
Run from the repo root:  python scripts/generate_insights.py
"""

import json, os, sys, hashlib, datetime, pathlib

# ---- config -------------------------------------------------------------
DATA_DIR = pathlib.Path(os.environ.get("FPL_DATA_DIR", "live"))   # where the JSON lives
OUT_FILE = DATA_DIR / "insights.json"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")        # confirm a current model in AI Studio
MY_ENTRY = 822500
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

PROMPT_VERSION = "2"   # bump this to force a regenerate even if the data is unchanged

SYSTEM = (
    "You are an FPL analyst for the team 'wirtzplay' in the Ballon d'FPL mini-league. "
    "Using ONLY the data provided, return a JSON object with these keys: "
    "headline  - string, <=12 words; "
    "team      - array of 2-4 short strings about MY squad (captain pick, a transfer to consider, a risk to watch); "
    "league    - array of 1-2 short strings (rivals and where I can gain points); "
    "stats     - array of 2-3 short strings: a market read (best value, in-form names, standout differentials); "
    "charts    - array of 1-2 short strings: how to read this week's value/form picture. "
    "Be specific and actionable, reference real players and fixtures from the data, "
    "invent no statistics. Output JSON only, no prose outside it."
)

# ---- helpers ------------------------------------------------------------
def load(name):
    try:
        return json.loads((DATA_DIR / f"{name}.json").read_text())
    except Exception:
        return None

def d(obj):  # unwrap {"data": ...}
    return (obj or {}).get("data", {}) if isinstance(obj, dict) else {}

def build_context():
    uni = d(load("player_universe"))
    league = d(load("league_table"))
    squad = d(load("my_squad"))
    rivals = d(load("rival_squads"))
    fixtures = d(load("fixtures"))
    gw_meta = d(load("gw_meta"))

    players = {p["id"]: p for p in uni.get("players", [])}
    fdr = fixtures.get("fdr_by_team", {})

    def pl_brief(pid):
        p = players.get(pid, {})
        f3 = (fdr.get(str(p.get("team_id"))) or {}).get("avg_fdr_next3")
        return {
            "name": p.get("name"), "pos": POS.get(p.get("position")),
            "team": p.get("team_short"), "price": p.get("price"),
            "form5": p.get("form_last5"), "ep_next": p.get("ep_next"),
            "fdr3": f3, "status": p.get("status"), "news": (p.get("news") or "")[:80],
        }

    picks = squad.get("picks", [])
    my_squad = [pl_brief(x["player_id"]) for x in picks]

    standings = league.get("standings", [])[:8]
    league_brief = [
        {"team": s.get("entry_name"), "mgr": s.get("manager_name"),
         "rank": s.get("rank"), "total": s.get("total"), "gw": s.get("gw_pts"),
         "me": s.get("entry_id") == MY_ENTRY}
        for s in standings
    ]

    pool = [p for p in uni.get("players", []) if (p.get("minutes") or 0) > 450]
    top_form = sorted(pool, key=lambda p: p.get("form_last5") or 0, reverse=True)[:10]
    top_form = [{"name": p["name"], "pos": POS.get(p["position"]), "team": p.get("team_short"),
                 "price": p.get("price"), "form5": p.get("form_last5")} for p in top_form]

    gems = [p for p in pool if any(f in (p.get("flags") or [])
            for f in ("value_gem", "xgi_value", "xgi_elite", "value_play"))][:8]
    gems = [{"name": p["name"], "pos": POS.get(p["position"]), "team": p.get("team_short"),
             "price": p.get("price"), "form5": p.get("form_last5")} for p in gems]

    teams = []
    short = {p.get("team_id"): p.get("team_short") for p in uni.get("players", [])}
    for tid, t in fdr.items():
        a = t.get("avg_fdr_next3")
        if a is not None:
            teams.append({"team": short.get(int(tid), tid), "fdr3": a})
    teams.sort(key=lambda x: x["fdr3"])
    best_fix, worst_fix = teams[:5], teams[-5:]

    return {
        "gw": gw_meta.get("current_gw", {}).get("id") if isinstance(gw_meta.get("current_gw"), dict) else gw_meta.get("current_gw"),
        "next_gw": gw_meta.get("next_gw", {}).get("id") if isinstance(gw_meta.get("next_gw"), dict) else gw_meta.get("next_gw"),
        "my_squad": my_squad,
        "bank": (squad.get("bank") or 0) / 10,
        "value": (squad.get("squad_value") or 0) / 10,
        "league": league_brief,
        "top_form": top_form,
        "value_gems": gems,
        "best_fixtures": best_fix,
        "worst_fixtures": worst_fix,
    }

def main():
    ctx = build_context()
    sig = hashlib.md5((PROMPT_VERSION + json.dumps(ctx, sort_keys=True, default=str)).encode()).hexdigest()

    # skip if data unchanged since last run
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text())
            if prev.get("meta", {}).get("sig") == sig:
                print("Data unchanged — skipping Gemini call.")
                return
        except Exception:
            pass

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("No GEMINI_API_KEY — skipping (dashboard will keep last insights).")
        return

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=MODEL,
            contents=SYSTEM + "\n\nDATA:\n" + json.dumps(ctx, default=str),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(resp.text)
    except Exception as e:
        print(f"Insight generation failed ({e}) — leaving existing file untouched.")
        return

    out = {
        "meta": {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "model": MODEL, "sig": sig},
        "data": data,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_FILE}")

if __name__ == "__main__":
    main()
