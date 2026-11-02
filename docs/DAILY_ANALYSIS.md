# Analyse journalière (READ-ONLY)

`tools/day_analysis.py` rejoue les enregistrements `~/hermes-data/recordings` (aucune connexion IBKR, aucun ordre) et produit :
niveaux clés (veille, overnight, OR30, IB60, VWAP), tendance OUVERTURE 09:30-11:00 / MIDI 11:00-14:00 / FIN 14:00-16:00 (heure NY)
avec continuation/retournement, comportement aux niveaux, et test de déplacement du stop (fixe, suiveur, BE+suiveur, ATR, structurel)
sur les signaux ORB 30 min, 11:00 et 14:00.

Les sessions sont rangées sous la date UTC de leur **démarrage** : l'outil rejoue toutes les sessions récentes et découpe par jour de trading NY.

## Installation sur le Mac mini (une fois)

```bash
cd <repo> && git pull && bash deploy/install_daily_analysis.sh
```

Lancement automatique 11:05, 14:05, 16:20 (NY), lun-ven. Rapports : `~/hermes-data/reports/AAAA-MM-JJ.md` (+ `.json`, copie horodatée,
`agregat-10-jours.md` après la clôture), notification macOS, journal `~/hermes-data/logs/daily_analysis.log`.

À la main : `tools/run_daily_analysis.sh 2026-11-02` ou `python tools/day_analysis.py --days 10`.
