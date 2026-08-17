#!/usr/bin/env python3
"""ingest_core_insights.py — build player_enrich.json from the FPL-Core-Insights detail layer.

Aggregates the per-player-match technical stats (playermatchstats + shots) for a season into a
per-player enrichment record. FPL stays authoritative for minutes/points; this supplies the
UNDERLYING technical layer the FPL API lacks: non-penalty xG, set-piece xG, DefCon per 90,
GK goals-prevented, xA, chances created — plus a recent-form over/under-performance signal for
the AI's xG-regression tilt.

Keyed by the STABLE FPL player_code (survives the season rollover, so last season's record can be
used as a pre-season prior), with the season's element player_id + web_name for same-season joins.

Usage: python ingest_core_insights.py <core_insights_repo_dir> <season> [out.json]
  e.g. python ingest_core_insights.py ./FPL-Core-Insights 2025-2026 player_enrich.json
"""
from __future__ import annotations
import csv, glob, json, os, re, sys, datetime as dt
from collections import defaultdict
from pathlib import Path

SET_PIECE = {"corner", "free-kick", "freekick", "set-piece", "direct-free-kick", "fromcorner"}
RECENT_GW = 6    # window for the recent over/under-performance signal
MIN_P90 = 300    # minutes floor below which per-90 rates are noise -> emitted as null


def gw_of(path):
    m = re.search(r"GW(\d+)", path)
    return int(m.group(1)) if m else 0


def fnum(v):
    try:
        return float(v) if v not in (None, "") else 0.0
    except Exception:
        return 0.0


def main() -> int:
    root = Path(sys.argv[1]); season = sys.argv[2]
    out = Path(sys.argv[3] if len(sys.argv) > 3 else "player_enrich.json")
    base = root / f"data/{season}/By Tournament/Premier League"

    # FPL player_id -> stable player_code / name / position
    pmeta = {}
    with open(root / f"data/{season}/players.csv") as f:
        for r in csv.DictReader(f):
            try:
                pmeta[int(r["player_id"])] = {"code": r.get("player_code"), "name": r.get("web_name"),
                                              "pos": r.get("position")}
            except Exception:
                pass

    agg = defaultdict(lambda: defaultdict(float))
    recent = defaultdict(lambda: defaultdict(float))
    max_gw = 0
    PM_FIELDS = ["minutes_played", "xg", "xa", "chances_created", "big_chances_missed", "shots_on_target",
                 "total_shots", "goals", "assists", "defensive_contributions", "saves", "goals_prevented",
                 "xgot_faced", "goals_conceded"]

    pm_files = sorted(glob.glob(str(base / "GW*/playermatchstats.csv")), key=gw_of)
    for fp in pm_files:
        gw = gw_of(fp); max_gw = max(max_gw, gw)
        for r in csv.DictReader(open(fp)):
            try:
                pid = int(r["player_id"])
            except Exception:
                continue
            for k in PM_FIELDS:
                agg[pid][k] += fnum(r.get(k))
            if fnum(r.get("minutes_played")) > 0:
                agg[pid]["apps"] += 1

    # shots -> non-penalty xG + set-piece xG (season and recent)
    for fp in sorted(glob.glob(str(base / "GW*/shots.csv")), key=gw_of):
        gw = gw_of(fp)
        for r in csv.DictReader(open(fp)):
            try:
                pid = int(r["player_id"])
            except Exception:
                continue
            xg = fnum(r.get("xg")); sit = (r.get("situation") or "").lower()
            if "penalty" not in sit:
                agg[pid]["npxg"] += xg
                if gw > max_gw - RECENT_GW:
                    recent[pid]["npxg"] += xg
            if sit in SET_PIECE:
                agg[pid]["sp_xg"] += xg

    # recent returns for the over/under-performance signal
    for fp in pm_files:
        if gw_of(fp) <= max_gw - RECENT_GW:
            continue
        for r in csv.DictReader(open(fp)):
            try:
                pid = int(r["player_id"])
            except Exception:
                continue
            for k in ("goals", "assists", "xa", "minutes_played"):
                recent[pid][k] += fnum(r.get(k))

    players = {}
    for pid, a in agg.items():
        mins = a["minutes_played"]
        if mins <= 0:
            continue
        info = pmeta.get(pid, {})
        rec = recent.get(pid, {})
        r_gi = rec.get("goals", 0) + rec.get("assists", 0)
        r_xgi = rec.get("npxg", 0) + rec.get("xa", 0)
        key = info.get("code") or f"id{pid}"
        rel = mins >= MIN_P90                                   # enough minutes for per-90 to be meaningful
        p90 = lambda k: round(a[k] / mins * 90, 3) if rel else None
        players[str(key)] = {
            "player_id": pid, "name": info.get("name"), "pos": info.get("pos"),
            "minutes": int(mins), "apps": int(a["apps"]),
            "npxg": round(a["npxg"], 2), "npxg90": p90("npxg"),
            "xa": round(a["xa"], 2), "xa90": p90("xa"),
            "sp_xg": round(a["sp_xg"], 2),
            "def_con": round(a["defensive_contributions"], 1),
            "def_con90": round(a["defensive_contributions"] / mins * 90, 2) if rel else None,
            "chances_created": round(a["chances_created"], 1),
            "big_chances_missed": round(a["big_chances_missed"], 1),
            "sot90": round(a["shots_on_target"] / mins * 90, 2) if rel else None,
            "gk_goals_prevented": round(a["goals_prevented"], 2),
            "gk_xgot_faced": round(a["xgot_faced"], 2),
            "goals": int(a["goals"]), "assists": int(a["assists"]),
            # xG-regression signal: recent (goals+assists) minus recent (npxG+xA); +ve = overperforming (fade)
            "recent_gi": round(r_gi, 1), "recent_xgi": round(r_xgi, 2),
            "recent_overperf": round(r_gi - r_xgi, 2),
        }

    res = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "season": season, "through_gw": max_gw, "n": len(players), "players": players}
    out.write_text(json.dumps(res, ensure_ascii=False))
    print(f"wrote {out}: {len(players)} players through GW{max_gw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
