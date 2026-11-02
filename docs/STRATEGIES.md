# Techniques de scalping MNQ — catalogue et protocole de validation

Statut : **en évaluation** (labo `tools/scalp_lab.py`, lecture seule). Aucune de ces techniques n'est
validée tant que le labo ne l'a pas confirmée sur les enregistrements réels (voir « Validation »).
Phase C : **aucun ordre n'est passé** par Hermès ; ce document décrit des hypothèses à tester.

## Objectif

Plusieurs petits trades par jour, **long et short**, objectif **10 à 20 points**, avec **≥ 60 % de
trades gagnants en fin de journée** — et une **espérance positive** après coûts.

### Pourquoi 60 % ne suffit pas seul

Avec un objectif `T` et un stop `S`, le taux de réussite minimum pour ne pas perdre d'argent est
`(S + coûts) / (T + S)` :

| Objectif / stop | % gagnants minimum (0,75 pt de coûts) |
|---|---|
| 10 / 10 | 54 % |
| 10 / 15 | 63 % |
| 10 / 20 | 69 % |
| 15 / 15 | 53 % |
| 20 / 10 | 36 % |
| 20 / 20 | 52 % |

Un stop plus large que l'objectif augmente le % de gagnants **mais** exige un % encore plus élevé.
Le labo affiche ce seuil à côté du taux réel pour chaque combinaison.

## Règles communes (identiques pour toutes les techniques)

- Données : barres 1 minute Hermès (trades classés acheteur/vendeur par le BBO tick-by-tick → delta).
- Signal calculé à la **clôture** de la barre `i` avec les seules données connues à ce moment ;
  entrée à l'**ouverture** de la barre `i+1`.
- Une position à la fois par technique ; pause de 2 minutes après chaque sortie.
- Entrées de 09:31 à 15:45 (New York), sortie forcée à 15:55.
- Variantes de sortie testées : objectif 10 / 15 / 20 × stop 10 / 15 / 20, avec ou sans **break-even**
  (stop ramené à entrée + 1 tick dès que le prix a parcouru 60 % de l'objectif).
- Si objectif et stop sont touchés dans la même minute → compté **perdant** (hypothèse pessimiste).
- Coûts : 0,75 pt aller-retour par défaut (commissions IBKR ≈ 0,62 pt + glissement).

## Les 8 techniques

| # | Technique | Sens | Règle d'entrée (barre de signal) | Idée |
|---|---|---|---|---|
| 1 | **VWAP rebond** | tendance | EMA9 > EMA21, VWAP en hausse sur 10 min, la barre touche le VWAP (±1 pt) et clôture au-dessus, barre verte. Inverse en short. | Les institutionnels défendent le VWAP en tendance. |
| 2 | **EMA21 pullback** | tendance | EMA9 > EMA21, prix > VWAP, la barre touche l'EMA21 et clôture au-dessus, barre verte, **delta > 0**. Inverse en short. | Repli peu profond dans une tendance, agresseurs qui reviennent. |
| 3 | **ORB15 cassure** | breakout | Après 09:45, première clôture au-dessus du plus haut des 15 premières minutes (une fois par côté, avant 11:30). | Le range d'ouverture fixe le sens de la matinée. |
| 4 | **ORB15 fausse cassure** | fade | Clôture hors du range 15 min dans les 10 dernières barres puis retour dedans d'au moins 1 pt (avant 12:00) → contre la cassure. | Les cassures ratées piègent les breakouts tardifs. |
| 5 | **Rejet de niveau clé** | fade | Niveaux veille H/L/C, overnight H/L : la barre arrive du bon côté, touche le niveau (dépasse au plus de 4 pts) et clôture à ≥ 2 pts de l'autre côté, barre dans le sens du rejet. | Les niveaux de la veille et de la nuit concentrent les ordres. |
| 6 | **Retour au VWAP** | mean reversion | Prix à plus de 3 ATR(1 m) du VWAP + barre de retournement avec delta qui change de camp. | Les excès loin du VWAP se résorbent souvent de 10-20 pts. |
| 7 | **Momentum + delta** | continuation | 3 barres consécutives dans le même sens avec delta du même signe, volume > 1,5 × moyenne 20 min, du bon côté du VWAP. | Agression soutenue et volume = poursuite à court terme. |
| 8 | **Épuisement** | fade | Nouveau plus haut (bas) 20 min avec mèche ≥ 60 % du range de la barre et delta contraire. | Rejet du nouveau extrême + agresseurs qui changent de camp. |

Le labo indique aussi pour chaque technique **dans quel segment elle marche** (ouverture 09:30-11:00,
midi 11:00-14:00, fin 14:00-16:00), pour pouvoir n'activer une technique qu'aux heures où elle tient.

## Validation (avant d'ajouter une technique au plan de trading)

Le labo teste 8 techniques × 18 sorties = **144 combinaisons**. Sur un seul jour, certaines
« gagnent » par pur hasard. Une technique n'est retenue que si, sur **au moins 10 jours de trading,
idéalement 20+** :

1. % de gagnants ≥ 60 % **et** espérance > 0 après coûts ;
2. au moins 20 trades ;
3. le résultat **tient sur chaque moitié** de la période (colonne « Tient sur les 2 moitiés » = oui) ;
4. une série de pertes d'affilée supportable, et une majorité de jours verts.

Ensuite : la confirmer sur les jours **suivants** (non vus) avant toute utilisation réelle.

## Utilisation

```bash
python tools/scalp_lab.py --days 10          # verdict sur 10 jours
python tools/scalp_lab.py --days 20 --lookback 35
python tools/scalp_lab.py --date 2026-11-02  # détail des trades d'un jour
```

Automatique (Mac mini, après `bash deploy/install_daily_analysis.sh`) : à 16:20 NY, rapports dans
`~/hermes-data/reports/` : `scalp-AAAA-MM-JJ.md`, `scalp-10-jours.md`, `scalp-20-jours.md` et
`RESUME-AAAA-MM-JJ.md` (tendances + verdicts sur une page).

## Journal des résultats

À compléter avec les verdicts réels (date, période, techniques retenues/écartées, pourquoi).

| Date | Période | Retenu | Écarté | Notes |
|---|---|---|---|---|
| | | | | |
