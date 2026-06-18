# wirtzplay model app — drop-in bundle

The World Cup engine, retrained on the Premier League, driving an FPL decision view in the
same chrome. Self-contained: vendored engine + generator + view. Deploys to its **own**
Cloudflare Pages project (`wirtzplay-model`) so your live `wirtzplay.pages.dev` stays
untouched while you test. We merge the two later.

```
model-app/
├── engine/                 vendored model (Dixon–Coles + Elo + FPL points, pure py)
├── build_predictions.py    fits the engine, writes site/fpl_predictions.json + data.js
├── site/
│   ├── index.html          the view (5 tabs, reads data.js)
│   ├── data.js             generated
│   └── fpl_predictions.json generated (the contract)
├── requirements.txt
└── workflow-fpl-model.yml  → copy to .github/workflows/fpl-model.yml
```

## Deploy steps (same pattern as the World Cup app)

1. **Drop it in.** Copy the whole `wirtzplay-model-bundle/` into the **fpl-data-store** repo,
   renamed to `model-app/`. Move `workflow-fpl-model.yml` to `.github/workflows/fpl-model.yml`.

2. **Create the Cloudflare Pages project.** In the Cloudflare dashboard → Workers & Pages →
   Create → Pages → **Direct upload** (not Git) → name it **`wirtzplay-model`** → skip the
   upload (the Action does it). This just reserves the project name + gives you the URL
   `wirtzplay-model.pages.dev`.

3. **Add the two secrets** to the fpl-data-store repo (Settings → Secrets and variables →
   Actions) — the same values already on wc26:
   - `CLOUDFLARE_API_TOKEN`
   - `CLOUDFLARE_ACCOUNT_ID`

4. **Push.** The workflow runs on push (and daily), builds the projections, and deploys
   `model-app/site` to `wirtzplay-model.pages.dev`. Open it on your phone, add to Home Screen.

That's it — identical to the WC flow (GitHub Action builds with Python, wrangler deploys the
static folder), just a separate project name and folder.

## Test it locally first (optional)

```
cd model-app
pip install -r requirements.txt
python build_predictions.py 21      # a sample GW so the view has data
open site/index.html
```

In-season, run `python build_predictions.py` with no argument — it auto-targets the upcoming
gameweek and pulls your real squad.

## Notes / what's still stubbed

- **Off-season now:** until the 26/27 fixtures publish (~mid-July), the build falls back to the
  last finished GW so the view always has something to show.
- **Transfers** ignore budget / free-transfer logic; **Chips** show illustrative EV. The real
  logic (fixture-run simulator, bank + FT constraints) is Phase 4.
- **Odds API** slots into the GW Proj tab later to show market odds beside the model.
- **The merge:** once projections validate in-season, fold these tabs into your main dashboard
  and point `wirtzplay.pages.dev` at the unified app; retire `wirtzplay-model`.
