# Le rapport quotidien — ce que Felipe reçoit chaque soir

**Fichier : `~/hermes-data/reports/RAPPORT-AAAA-MM-JJ.md`**, généré par `tools/daily_report.py` à 16:20 NY.
C'est le seul document à envoyer. Les chiffres viennent des fichiers JSON des outils, jamais d'un
calcul à la main. Le rapport est en français, en langage simple, avec les calculs posés.

## Structure (toujours dans cet ordre)

| Section | Contenu | Pour répondre à |
|---|---|---|
| 1. En bref (30 secondes) | Le film du marché (ouverture → midi → fin), le verdict 60 % aujourd'hui / 10 jours / 20 jours, le meilleur stop du jour, ce qu'il faut retenir. | « Qu'est-ce qui s'est passé, est-ce qu'on a une technique à 60 % ? » |
| 2. Le marché aujourd'hui | Tendance de chaque moment (haussière / baissière / range), continuation ou retournement, qui pousse (delta acheteurs/vendeurs), niveaux clés et ce qui s'y est passé (cassé / tenu / rejeté). | « Comment s'installent les tendances et de quel côté elles vont ? » |
| 3. Les meilleurs tests — avec les calculs | Les 3 meilleures **techniques différentes** sur la période la plus longue, puis sur le jour seul. Pour chacune : ce qu'elle fait, résultats, et les maths (taux, seuil de rentabilité, espérance, total en $, profit factor, intervalle de confiance, risque de série, stabilité), et dans quel moment de la journée elle marche. | « Quels sont les meilleurs tests et pourquoi ? » |
| 4. Tous les tests | Les 8 techniques, chacune avec sa meilleure sortie valide et une décision : ✅ GARDER · 👀 SURVEILLER · ❌ ÉCARTER · ⏳ PAS ASSEZ DE DONNÉES. | « Qu'est-ce qu'on garde, qu'est-ce qu'on jette ? » |
| 5. Test du déplacement du stop | Sur les signaux ORB 30 min, 11:00 et 14:00 : meilleur et pire stop (fixe, suiveur, break-even, ATR, structurel), jusqu'où le prix est allé pour et contre. | « Où mettre / comment bouger le stop ? » |
| 6. Ce qu'on fait demain | Les techniques retenues avec leur sortie et leur moment de la journée, ou « aucune validée, on continue d'accumuler ». | « Quoi faire demain ? » |
| 7. Les maths en une minute | Glossaire des formules. | Comprendre chaque chiffre. |

## Tous les tests analysés

**A. Analyse du marché** (`tools/day_analysis.py`)
- Découpage OUVERTURE 09:30-11:00 · MIDI 11:00-14:00 · FIN 14:00-16:00 (heure de New York).
- Pour chaque moment : mouvement net, amplitude, delta, pente, efficacité → tendance ; continuation / retournement.
- Niveaux : veille H/L/C, overnight H/L, range des 30 premières minutes (OR30), première heure (IB60), VWAP.
- Comportement à chaque niveau par moment : cassé ↑/↓, tenu (support), rejeté (résistance).

**B. Test du déplacement du stop** (`tools/day_analysis.py`) — 15 stops sur 3 signaux par jour
- Signaux : cassure ORB 30 min · position vs VWAP + delta à 11:00 · idem à 14:00. Sortie à 15:59 ou au stop.
- Stops : fixe 10/15/20/30/40 · suiveur 10/15/20/30 · break-even +10 puis suiveur 15 · break-even +15
  puis suiveur 20 · suiveur ATR ×1 / ×1,5 / ×2 · structurel (extrême opposé).

**C. Labo scalping 10-20 pts, objectif 60 %** (`tools/scalp_lab.py`) — 8 techniques × 18 sorties = 144 combinaisons
- Techniques : VWAP rebond · EMA21 pullback · ORB15 cassure · ORB15 fausse cassure · rejet de niveau clé ·
  retour au VWAP · momentum + delta · épuisement (détail : `docs/STRATEGIES.md`).
- Sorties : objectif 10/15/20 × stop 10/15/20 × avec/sans break-even.
- Sur 1 jour, 10 jours et 20 jours ; validation = 60 % + espérance > 0 + 20 trades + stable sur les 2 moitiés.

## Fichiers détaillés (si Felipe veut creuser)

`AAAA-MM-JJ.md` (analyse du jour), `scalp-AAAA-MM-JJ.md` (labo du jour, liste des trades),
`scalp-10-jours.md`, `scalp-20-jours.md` (les 144 combinaisons), `agregat-10-jours.md` (stops sur 10 jours),
`*.json` (données brutes), `AAAA-MM-JJ.err` (erreurs éventuelles).

## Message type (Slack / courriel)

```
📊 Hermès MNQ — <date>
<copier la section 1 « En bref » du RAPPORT>
Rapport complet : RAPPORT-<date>.md (en pièce jointe)
```
