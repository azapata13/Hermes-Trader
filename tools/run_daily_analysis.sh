#!/bin/bash
# Lance l'analyse journalière Hermès (READ-ONLY) et range les rapports dans ~/hermes-data/reports/.
# Après la clôture : RAPPORT-<date>.md = le document à envoyer (voir docs/RAPPORT_QUOTIDIEN.md).
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
# Après la clôture : agrégat des stops + labo scalping (jour, 10 jours, 20 jours) + résumé
VERDICT=""
if [ "$STAMP" -ge 1600 ]; then
    "$PY" tools/day_analysis.py --days 10 --out "$OUT/agregat-10-jours.md" > /dev/null 2>> "$OUT/$DAY.err"
    "$PY" tools/scalp_lab.py --date "$DAY" --out "$OUT/scalp-$DAY.md" --json "$OUT/scalp-$DAY.json" \
        > /dev/null 2>> "$OUT/$DAY.err"
    "$PY" tools/scalp_lab.py --days 10 --out "$OUT/scalp-10-jours.md" --json "$OUT/scalp-10-jours.json" \
        > /dev/null 2>> "$OUT/$DAY.err"
    "$PY" tools/scalp_lab.py --days 20 --lookback 35 --out "$OUT/scalp-20-jours.md" --json "$OUT/scalp-20-jours.json" \
        > /dev/null 2>> "$OUT/$DAY.err"
    # LE rapport à envoyer (langage simple + maths) : docs/RAPPORT_QUOTIDIEN.md
    "$PY" tools/daily_report.py --date "$DAY" --reports-dir "$OUT" > /dev/null 2>> "$OUT/$DAY.err"
    VERDICT="$(grep -hE '^(✅|❌)' "$OUT/scalp-10-jours.md" 2>/dev/null | head -1 | sed 's/\*//g' | cut -c1-120)"
fi

if [ $RC -eq 0 ]; then
    if [ -f "$OUT/RAPPORT-$DAY.md" ] && [ "$STAMP" -ge 1600 ]; then
        MSG="Rapport $DAY prêt : $OUT/RAPPORT-$DAY.md ${VERDICT:+— 10 j : $VERDICT}"
    else
        MSG="Analyse $DAY ($STAMP) prête : $OUT/$DAY.md"
    fi
else
    MSG="Analyse $DAY ($STAMP) ÉCHEC rc=$RC : voir $OUT/$DAY.err"
fi
echo "$(date '+%F %T') $MSG" >> "$HOME/hermes-data/logs/daily_analysis.log"
command -v osascript >/dev/null && osascript -e "display notification \"$MSG\" with title \"Hermès\"" 2>/dev/null
exit $RC
