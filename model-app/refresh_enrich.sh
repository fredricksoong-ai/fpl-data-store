#!/usr/bin/env bash
# refresh_enrich.sh — refresh player_enrich.json from the FPL-Core-Insights detail layer.
#
# Fail-soft and commit-guarded: re-ingests ONLY when the upstream branch HEAD changed since the
# last successful ingest (stamped as source_sha in the output). Any failure — unreachable source,
# clone error, ingest error — leaves the committed player_enrich.json untouched and exits 0, so the
# 3-hourly model build never breaks on an upstream hiccup. build_players is itself fail-soft on a
# missing/stale feed, so a skipped refresh simply reuses the last good enrichment.
#
# Config via env (with defaults): CI_REPO, CI_BRANCH, CI_SEASON.
# NOTE: CI_BRANCH points at the review branch that carries the enriched schema until it merges to main.
set -uo pipefail

REPO="${CI_REPO:-olbauday/FPL-Core-Insights}"
BRANCH="${CI_BRANCH:-codex/data-integration-review}"
SEASON="${CI_SEASON:-2025-2026}"
OUT="${ENRICH_OUT:-model-app/site/player_enrich.json}"

remote_sha="$(git ls-remote "https://github.com/${REPO}.git" "refs/heads/${BRANCH}" 2>/dev/null | awk '{print $1}')"
if [ -z "${remote_sha}" ]; then
  echo "enrich: cannot reach ${REPO}@${BRANCH}; keeping committed feed"; exit 0
fi

prev_sha="$(python3 -c "import json;print(json.load(open('${OUT}')).get('source_sha',''))" 2>/dev/null || true)"
if [ -n "${prev_sha}" ] && [ "${prev_sha}" = "${remote_sha}" ]; then
  echo "enrich: upstream unchanged (${remote_sha:0:8}); reusing committed feed"; exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
if ! git clone --depth 1 --branch "${BRANCH}" --filter=blob:none --no-checkout \
     "https://github.com/${REPO}.git" "${tmp}/fci" >/dev/null 2>&1; then
  echo "enrich: clone failed; keeping committed feed"; exit 0
fi
if ! ( cd "${tmp}/fci" && git sparse-checkout set "data/${SEASON}" >/dev/null 2>&1 && git checkout >/dev/null 2>&1 ); then
  echo "enrich: sparse checkout failed; keeping committed feed"; exit 0
fi

if ! python3 model-app/ingest_core_insights.py "${tmp}/fci" "${SEASON}" "${OUT}"; then
  echo "enrich: ingest failed; keeping committed feed"; exit 0
fi

# stamp the source commit so the next run can skip when the upstream is unchanged
python3 - "${OUT}" "${remote_sha}" <<'PY'
import json, sys
path, sha = sys.argv[1], sys.argv[2]
d = json.load(open(path)); d["source_sha"] = sha
json.dump(d, open(path, "w"), ensure_ascii=False)
print(f"enrich: ingested @ {sha[:8]}")
PY
