# AGENTS.md — instructions pour les agents (Codex, Claude) sur Hermès

Lis d'abord : `docs/ARCHITECTURE_PHASE_C.md` (le moteur), puis `docs/HANDOFF_CODEX.md` (état actuel),
`docs/RAPPORT_QUOTIDIEN.md` (ce qu'on envoie chaque jour) et `docs/STRATEGIES.md` (les techniques testées).

## Règles non négociables

- Phase C = **lecture seule**. Jamais d'ordre, `orders_enabled = false`, `read_only = true`, TWS en Read-Only.
- Les outils d'analyse (`tools/day_analysis.py`, `tools/scalp_lab.py`, `tools/daily_report.py`) ne se
  connectent pas à IBKR : ils rejouent `~/hermes-data/recordings`.
- **Ne jamais recopier ou recalculer les chiffres à la main** dans un message : envoyer ce que produit
  `tools/daily_report.py`. Si un fichier manque, le dire, ne rien inventer.
- Ne pas déclarer une technique « validée » sur moins de 10 jours ni si elle ne tient pas sur les deux
  moitiés de la période (voir `docs/STRATEGIES.md`, section Validation).

## Routine quotidienne (jours de bourse)

1. 16:20 heure de New York, automatique (launchd, `deploy/install_daily_analysis.sh`) :
   `tools/run_daily_analysis.sh` → `~/hermes-data/reports/RAPPORT-AAAA-MM-JJ.md` (+ fichiers détaillés).
   Si absent : `tools/run_daily_analysis.sh AAAA-MM-JJ` après 16:00 NY, puis
   `python tools/daily_report.py --date AAAA-MM-JJ`.
2. **Envoyer à Felipe le contenu de `RAPPORT-AAAA-MM-JJ.md`** (Slack/n8n quand la branche
   `d1-slack-human-approval` est prête ; sinon le fichier tel quel). Section 1 « En bref » en tête du
   message, le reste en pièce jointe ou à la suite. Format détaillé : `docs/RAPPORT_QUOTIDIEN.md`.
3. Reporter le verdict du jour dans le **journal des résultats** de `docs/STRATEGIES.md`.

## Tests

`python -m pytest -q` (les tests qui importent `ibapi` ne tournent que sur le Mac mini).
Outils d'analyse : `tests/unit/test_day_analysis.py`, `test_scalp_lab.py`, `test_daily_report.py`.

## Ajouter une technique

Une fonction `s_<nom>(ctx, i, state) -> +1/-1/0` dans `tools/scalp_lab.py`, utilisant seulement les
barres `<= i`, ajoutée à `SETUPS` avec une docstring d'une phrase (elle apparaît dans le rapport), une
ligne dans le catalogue de `docs/STRATEGIES.md`, et le test `test_signals_never_look_ahead` doit passer.
