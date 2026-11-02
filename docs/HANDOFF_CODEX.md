# Passation — analyse journalière et labo scalping (2026-11-02 → mercredi 2026-11-04)

Pour Codex (et toute personne qui reprend). Ajouts **lecture seule** : aucun changement au code live
(`hermes/`), à la config IBKR ni aux chemins d'ordres. Tout est sur `main`.

## Objectif utilisateur

Plusieurs petits trades par jour de **10 à 20 pts**, long et short, **≥ 60 % de gagnants en fin de journée**
avec espérance positive. Comprendre comment s'installent les tendances à l'ouverture, à midi et en fin de
journée, et tester le déplacement du stop.

## Ce qui a été ajouté

| Fichier | Rôle |
|---|---|
| `tools/day_analysis.py` | Niveaux (veille, overnight, OR30, IB60, VWAP), tendance OUVERTURE / MIDI / FIN (heure NY) + continuation/retournement, comportement aux niveaux, test des stops (fixe, suiveur, BE+suiveur, ATR, structurel). |
| `tools/scalp_lab.py` | 8 techniques × 18 sorties (objectif 10/15/20, stop 10/15/20, ± break-even) ; verdict « ≥ 60 % ET espérance > 0 ET tient sur chaque moitié de la période ». |
| `tools/daily_report.py` | **Le rapport quotidien à envoyer** (`RAPPORT-AAAA-MM-JJ.md`) : langage simple, meilleurs tests avec les calculs, décisions garder/surveiller/écarter. Format : `docs/RAPPORT_QUOTIDIEN.md`. |
| `AGENTS.md` | Règles et routine quotidienne pour les agents. |
| `docs/STRATEGIES.md` | Catalogue des techniques, règles, maths du % minimum, protocole de validation, **journal des résultats à tenir à jour**. |
| `docs/DAILY_ANALYSIS.md` | Utilisation et installation. |
| `tools/run_daily_analysis.sh` + `deploy/install_daily_analysis.sh` | launchd : 11:05, 14:05, 16:20 NY lun-ven → `~/hermes-data/reports/` (+ `RAPPORT-AAAA-MM-JJ.md`, notification macOS). |
| `tests/unit/test_day_analysis.py`, `tests/unit/test_scalp_lab.py`, `tests/unit/test_daily_report.py` | Stops, brackets, pas de look-ahead, replay rapide = replay complet, cache. |

## Points techniques à connaître

- **Dossiers de date** : le recorder range une session sous la date UTC de son *démarrage*. Une session
  multi-jours reste dans son premier dossier → les outils rejouent toutes les sessions récentes (mtime) et
  découpent par jour de trading NY. Ne pas sélectionner par nom de dossier.
- **Replay rapide** : `RawMarketDepth` est sauté (ni les barres ni la classification C4 ne lisent le carnet) ;
  test d'équivalence fourni. `--with-depth` = replay complet.
- **Cache** : `~/hermes-data/cache/day_analysis/` (clé = taille + mtime des parts) ; supprimable sans risque.
- **Pessimisme** : objectif et stop dans la même barre 1 m = perdant. Passer aux barres 30 s réduirait
  ce biais (piste d'amélioration).
- La branche locale du Mac `d1-slack-human-approval` diverge de `main` ; les fichiers ci-dessus y ont été
  copiés via `git checkout origin/main -- …` (non commités sur cette branche).

## À faire mercredi

1. Lire `~/hermes-data/reports/RAPPORT-2026-11-02.md` et `RAPPORT-2026-11-03.md` (régénérer si absent :
   `tools/run_daily_analysis.sh AAAA-MM-JJ` après 16:00 NY).
2. Reporter les verdicts dans le **journal** de `docs/STRATEGIES.md`.
3. Brancher `RAPPORT-*.md` sur l'envoi Slack de `d1-slack-human-approval` (section 1 dans le message,
   rapport complet joint) — voir `docs/RAPPORT_QUOTIDIEN.md` et `AGENTS.md`.
4. Pistes : barres 30 s pour lever les ambiguïtés ; filtre de régime (jour de tendance vs range détecté
   sur l'ouverture) pour n'activer que les techniques adaptées ; walk-forward (calibrer sur N jours,
   tester sur les suivants).
