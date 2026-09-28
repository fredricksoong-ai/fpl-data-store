#!/usr/bin/env python3
"""build_artifact.py — assemble the FPLanner persisted-artifact HTML from the app + current feeds.

The Cowork artifact sandbox blocks network, so the app can't fetch its JSON feeds or the PL badge CDN.
This bakes the feeds inline behind a tiny fetch-shim (the app code is otherwise unchanged) and swaps the
team-badge <img> for a text chip. Run it after the feed builders refresh site/*.json, then hand the
output to update_artifact.

Usage: python build_artifact.py [site_dir] [out_html]
  defaults: site_dir=./site  out_html=fplanner.html
"""
from __future__ import annotations
import json, os, sys

# feeds the app fetches (odds is optional/throttled — fine if absent, app degrades gracefully)
FEEDS = ["players", "recommend", "v2", "arena", "chips", "fixtures", "xg", "titleodds", "squad", "player_history"]

OLD_LOGO = '''const logo=(code,cls)=>{const id=BADGE[code];if(!id)return'';  // local self-hosted badge first, CDN fallback if not yet downloaded
  return `<img class="bdg${cls?' '+cls:''}" src="badges/${code}.svg" onerror="this.onerror=null;this.src='https://resources.premierleague.com/premierleague25/badges-alt/${id}.svg'" alt="" loading="lazy">`;};'''
NEW_LOGO = ("const logo=(code,cls)=>{if(!code)return'';return `<span class=\"bdg${cls?' '+cls:''}\" "
            "style=\"display:inline-flex;align-items:center;justify-content:center;min-width:1.7em;height:1.05em;"
            "padding:0 3px;border-radius:3px;background:rgba(255,255,255,0.14);font-size:8px;font-weight:800;"
            "vertical-align:middle\">${code}</span>`;};")


def build(site_dir: str, out_html: str) -> str:
    html = open(os.path.join(site_dir, "index.html")).read()
    feeds = {}
    for k in FEEDS:
        p = os.path.join(site_dir, f"{k}.json")
        if os.path.exists(p):
            feeds[f"{k}.json"] = json.load(open(p))
    shim = ("<script>window.__FEEDS=" + json.dumps(feeds, ensure_ascii=False) + ";(function(){var of=window.fetch;"
            "window.fetch=function(u,o){var k=String(u).split('/').pop().split('?')[0];"
            "if(Object.prototype.hasOwnProperty.call(window.__FEEDS,k))"
            "return Promise.resolve({ok:true,status:200,json:function(){return Promise.resolve(window.__FEEDS[k]);}});"
            "if(k.slice(-5)==='.json')return Promise.reject(new Error('offline:'+k));"
            "return of?of(u,o):Promise.reject(new Error('offline:'+k));};})();</script>")
    if "<body>" not in html:
        raise SystemExit("no <body> in index.html")
    html = html.replace("<body>", "<body>\n" + shim, 1)
    if OLD_LOGO not in html:
        raise SystemExit("logo() block not found — index.html may have changed; update OLD_LOGO")
    html = html.replace(OLD_LOGO, NEW_LOGO, 1)
    open(out_html, "w").write(html)
    print(f"wrote {out_html}: {len(html)} bytes | feeds: {', '.join(sorted(feeds))}")
    return out_html


if __name__ == "__main__":
    site = sys.argv[1] if len(sys.argv) > 1 else "site"
    out = sys.argv[2] if len(sys.argv) > 2 else "fplanner.html"
    build(site, out)
