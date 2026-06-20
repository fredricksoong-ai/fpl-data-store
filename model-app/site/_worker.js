// Cloudflare Pages advanced-mode Worker for the FPLanner site.
// POST /refresh -> triggers the GitHub Actions workflow_dispatch (rebuilds all feeds).
// Everything else falls through to the static site (env.ASSETS).
//
// The token lives ONLY here as a Cloudflare secret named GH_DISPATCH_TOKEN, set in
// Pages > wirtzplay > Settings > Variables and Secrets (type: Secret). Use a
// fine-grained PAT with permission Actions = Read and write on fredricksoong-ai/fpl-data-store.

const REPO = "fredricksoong-ai/fpl-data-store";
const WORKFLOW = "fpl-model.yml";
const XG_PATH = "model-app/data/xg_overrides.json";   // manual FotMob xG, committed -> read by build_xg.py

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // POST /sync-xg -> merge manually-entered match xG into the committed overrides file.
    // The commit triggers the workflow (on: push), rebuilding xg.json with your numbers.
    if (url.pathname === "/sync-xg") {
      if (request.method !== "POST") return json({ ok: false, error: "POST only" }, 405);
      if (!env.GH_DISPATCH_TOKEN) return json({ ok: false, error: "GH_DISPATCH_TOKEN not configured" }, 500);
      let body; try { body = await request.json(); } catch (e) { return json({ ok: false, error: "bad JSON" }, 400); }
      const incoming = body && body.xg;
      if (!incoming || typeof incoming !== "object") return json({ ok: false, error: "missing xg" }, 400);
      const clean = {};
      for (const k of Object.keys(incoming)) {
        const v = incoming[k];
        if (Array.isArray(v) && v.length === 2) {
          const a = Number(v[0]), b = Number(v[1]);
          if (isFinite(a) && isFinite(b) && a >= 0 && a <= 10 && b >= 0 && b <= 10)
            clean[k] = [Math.round(a * 100) / 100, Math.round(b * 100) / 100];
        }
      }
      if (!Object.keys(clean).length) return json({ ok: false, error: "no valid xG values" }, 400);
      const api = `https://api.github.com/repos/${REPO}/contents/${encodeURI(XG_PATH)}`;
      const H = { "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fplanner-dashboard" };
      let sha, merged = { ...clean };
      const g = await fetch(`${api}?ref=main`, { headers: H });
      if (g.status === 200) { const gj = await g.json(); sha = gj.sha;
        try { merged = { ...JSON.parse(decodeB64(gj.content)), ...clean }; } catch (e) {} }
      else if (g.status !== 404) return json({ ok: false, status: g.status, error: await g.text() }, 502);
      const put = await fetch(api, { method: "PUT", headers: H, body: JSON.stringify({
        message: `xG sync: ${Object.keys(clean).length} match(es)`, content: encodeB64(JSON.stringify(merged)), sha, branch: "main" }) });
      if (put.status === 200 || put.status === 201) return json({ ok: true, matches: Object.keys(merged).length });
      return json({ ok: false, status: put.status, error: await put.text() }, 502);
    }

    if (url.pathname === "/refresh") {
      if (request.method !== "POST") return json({ ok: false, error: "POST only" }, 405);
      if (!env.GH_DISPATCH_TOKEN) return json({ ok: false, error: "GH_DISPATCH_TOKEN not configured" }, 500);
      const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "fplanner-dashboard",
        },
        body: JSON.stringify({ ref: "main" }),
      });
      if (r.status === 204) return json({ ok: true });
      return json({ ok: false, status: r.status, error: await r.text() }, 502);
    }
    return env.ASSETS.fetch(request);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
function encodeB64(str) { const b = new TextEncoder().encode(str); let s = ""; for (const x of b) s += String.fromCharCode(x); return btoa(s); }
function decodeB64(b64) { const bin = atob(b64.replace(/\s/g, "")); return new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0))); }
