#!/usr/bin/env bash
# One-time: pull all 20 club crests into model-app/site/badges/ so the app self-hosts them.
# Run from the repo root (fpl-data-store).  Re-run any time to refresh.
#   bash model-app/download_badges.sh
set -euo pipefail

DEST="$(dirname "$0")/site/badges"
mkdir -p "$DEST"

# 3-letter code -> Premier League club id (badges-alt/{id}.svg)
declare -A ID=(
  [ARS]=3 [AVL]=7 [BHA]=36 [BOU]=91 [BRE]=94 [CHE]=8 [COV]=9 [CRY]=31
  [EVE]=11 [FUL]=54 [HUL]=88 [IPS]=40 [LEE]=2 [LIV]=14 [MCI]=43 [MUN]=1
  [NEW]=4 [NFO]=17 [SUN]=56 [TOT]=6
)

for code in "${!ID[@]}"; do
  url="https://resources.premierleague.com/premierleague25/badges-alt/${ID[$code]}.svg"
  if curl -fsSL "$url" -o "$DEST/$code.svg"; then
    echo "ok  $code  <- $url"
  else
    echo "FAIL $code ($url)" >&2
  fi
done
echo "Saved $(ls "$DEST" | wc -l) badges to $DEST"
