#!/bin/bash
# Lance l'analyse journalière Hermès (READ-ONLY) et range les rapports dans ~/hermes-data/reports/.
# Appelé par launchd (deploy/install_daily_analysis.sh) à 11:05, 14:05 et 16:20 heure de New York,
# ou à la main :  tools/run_daily_analysis.sh [AAAA-MM-JJ]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HOME/hermes-data/reports"
mkdir -p "$OUT" "$HOME/hermes-data/logs"
if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY="$(command -v python3)"; fi
DAY="${1:-$(TZ=America/New_York date +%F)}"
STAMP="$(TZ=America/New_York date +%H%M)"
cd "$REPO" || exit 1

"$PY" tools/day_analysis.py --date "$DAY" \
    --out "$OUT/$DAY.md" --json "$OUT/$DAY.json" > /dev/null 2> "$OUT/$DAY.err"
RC=$?
cp -f "$OUT/$DAY.md" "$OUT/$DAY-$STAMP.md" 2>/dev/null
# Agrégat glissant des stops sur 10 jours, seulement après la clôture
if [ "$STAMP" -ge 1600 ]; then
    "$PY" tools/day_analysis.py --days 10 --out "$OUT/agregat-10-jours.md" > /dev/null 2>> "$OUT/$DAY.err"
fi

if [ $RC -eq 0 ]; then
    MSG="Analyse $DAY ($STAMP) prête : $OUT/$DAY.md"
else
    MSG="Analyse $DAY ($STAMP) ÉCHEC rc=$RC : voir $OUT/$DAY.err"
fi
echo "$(date '+%F %T') $MSG" >> "$HOME/hermes-data/logs/daily_analysis.log"
command -v osascript >/dev/null && osascript -e "display notification \"$MSG\" with title \"Hermès\"" 2>/dev/null
exit $RC
