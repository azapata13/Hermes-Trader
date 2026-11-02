#!/bin/bash
# Installe (ou réinstalle) le lancement automatique de l'analyse journalière sur le Mac mini.
#   bash deploy/install_daily_analysis.sh            # installe + lance une première analyse
#   bash deploy/install_daily_analysis.sh --remove   # désinstalle
# Horaires : 11:05, 14:05, 16:20 heure de New York, du lundi au vendredi (convertis en heure locale
# du Mac au moment de l'installation : relancer ce script après un changement d'heure si le Mac
# n'est pas réglé sur New York).
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.hermes.daily-analysis"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then rm -f "$PLIST"; echo "Désinstallé."; exit 0; fi
chmod +x "$REPO/tools/run_daily_analysis.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/hermes-data/logs"

entries=""
for hm in 11:05 14:05 16:20; do
    # heure New York -> heure locale du Mac (aujourd'hui)
    epoch=$(TZ=America/New_York date -j -f "%Y-%m-%d %H:%M" "$(TZ=America/New_York date +%F) $hm" +%s)
    H=$(date -r "$epoch" +%-H); M=$(date -r "$epoch" +%-M)
    for wd in 1 2 3 4 5; do
        entries="$entries<dict><key>Weekday</key><integer>$wd</integer><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>"
    done
done

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$REPO/tools/run_daily_analysis.sh</string></array>
<key>WorkingDirectory</key><string>$REPO</string>
<key>StartCalendarInterval</key><array>$entries</array>
<key>StandardOutPath</key><string>$HOME/hermes-data/logs/daily_analysis.out</string>
<key>StandardErrorPath</key><string>$HOME/hermes-data/logs/daily_analysis.out</string>
</dict></plist>
PL
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installé : $PLIST"
echo "Première analyse maintenant..."
"$REPO/tools/run_daily_analysis.sh" && echo "OK -> $HOME/hermes-data/reports/" || echo "Voir $HOME/hermes-data/reports/*.err"
