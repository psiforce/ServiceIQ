#!/usr/bin/env bash
# Renders docs/diagrams/svg/*.svg to docs/diagrams/*.png (2x) with headless Microsoft Edge.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
PROFILE="$(cygpath -w "${TEMP:-/tmp}")\\serviceiq-edge-render"
for f in "$ROOT"/diagrams/svg/*.svg; do
  name="$(basename "$f" .svg)"
  w=$(head -c 300 "$f" | sed -n 's/.*width="\([0-9]*\)".*/\1/p')
  h=$(head -c 300 "$f" | sed -n 's/.*height="\([0-9]*\)".*/\1/p')
  url="file:///$(cygpath -m "$f" | sed 's/ /%20/g')"
  "$EDGE" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --user-data-dir="$PROFILE" --window-size="$w,$h" \
    --screenshot="$(cygpath -w "$ROOT/diagrams/$name.png")" "$url" >/dev/null 2>&1
  echo "$name: ${w}x${h}"
done
