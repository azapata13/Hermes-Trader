#!/usr/bin/env python3
"""Rapport quotidien Hermès en langage simple : RAPPORT-AAAA-MM-JJ.md.

C'est LE document à envoyer chaque jour (voir docs/RAPPORT_QUOTIDIEN.md). Il est généré à partir des
fichiers JSON produits par tools/day_analysis.py et tools/scalp_lab.py : aucun chiffre n'est recopié
à la main. Lecture seule.

    python tools/daily_report.py --date 2026-11-02            # lit ~/hermes-data/reports/
    python tools/daily_report.py --date 2026-11-02 --reports-dir <dossier>

Fichiers lus (les absents sont signalés, pas inventés) :
    <date>.json               analyse du jour (segments, niveaux, test des stops)
    scalp-<date>.json         labo scalping sur le jour
    scalp-10-jours.json       labo scalping sur 10 jours
    scalp-20-jours.json       labo scalping sur 20 jours
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

POINT_VALUE = 2.0
WIN_GOAL = 0.60
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre")


# ------------------------------------------------------------------------------------------ maths
def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % confidence interval of a win rate (Wilson score)."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def pct(x: float) -> str:
    return f"{100 * x:.0f} %"


def pts(x: float) -> str:
    return f"{x:+.2f} pts"


def usd(x_pts: float) -> str:
    return f"{x_pts * POINT_VALUE:+.0f} $"


def confidence(r: dict) -> str:
    n = r["trades"]
    if n < 20:
        return "FAIBLE (moins de 20 trades : peut être du hasard)"
    if r.get("stable_halves") is True and n >= 40:
        return "BONNE (40+ trades, tient sur les deux moitiés de la période)"
    if r.get("stable_halves") is False:
        return "FAIBLE (ne tient pas sur les deux moitiés de la période)"
    return "MOYENNE"


def meets(r: dict, min_trades: int) -> bool:
    return (r["win_rate"] >= WIN_GOAL and r["expectancy"] > 0 and r["trades"] >= min_trades
            and r.get("stable_halves") is not False)


# ------------------------------------------------------------------------------------------ input
def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def best_per_setup(rows: list[dict], min_trades: int) -> dict[str, dict]:
    """Per technique, the exit to judge it on: best one meeting the goal, else best rentable one with
    enough trades, else its best expectancy (rows are sorted by expectancy desc)."""
    out: dict[str, dict] = {}
    for rank in (lambda r: meets(r, min_trades),
                 lambda r: r["expectancy"] > 0 and r["trades"] >= min_trades,
                 lambda r: r["trades"] >= min_trades,
                 lambda r: True):
        for r in rows:
            if r["setup"] not in out and rank(r):
                out[r["setup"]] = r
    return out


def all_setups() -> list[str]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.scalp_lab import SETUPS
        return list(SETUPS)
    except Exception:  # noqa: BLE001 - report must still render
        return []


# --------------------------------------------------------------------------------------- sections
def fr_date(d: date) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def math_block(r: dict) -> list[str]:
    n, w, lo = r["trades"], r["wins"], r["losses"]
    t, s, c = r["target"], r["stop"], r["cost"]
    be = (s + c) / (t + s)
    ci_lo, ci_hi = wilson(w, n)
    L = [
        f"- **Taux de réussite** = gagnants / trades = {w} / {n} = **{pct(r['win_rate'])}** "
        f"(objectif : 60 %).",
        f"- **Seuil de rentabilité** = (stop + coûts) / (objectif + stop) = ({s:g} + {c:g}) / ({t:g} + {s:g}) = "
        f"**{pct(be)}** → marge de **{100 * (r['win_rate'] - be):+.0f} points de %** au-dessus du seuil.",
        f"- **Espérance par trade** = taux × gain moyen − (1 − taux) × perte moyenne = "
        f"{r['win_rate']:.2f} × {r['avg_win']:.2f} − {1 - r['win_rate']:.2f} × {abs(r['avg_loss']):.2f} = "
        f"**{pts(r['expectancy'])}** ({usd(r['expectancy'])} par contrat MNQ).",
        f"- **Total** = {n} trades × {r['expectancy']:+.2f} = **{pts(r['total'])}** = **{usd(r['total'])}** "
        f"par contrat MNQ (1 pt = 2 $).",
    ]
    if lo and r["gross_loss"] < 0:
        L.append(f"- **Profit factor** = gains totaux / pertes totales = {r['gross_win']:.1f} / "
                 f"{abs(r['gross_loss']):.1f} = **{r['gross_win'] / abs(r['gross_loss']):.2f}** "
                 f"(> 1 = gagnant ; > 1,5 = solide).")
    else:
        L.append("- **Profit factor** : aucune perte sur l'échantillon (à confirmer, échantillon trop court).")
    L.append(f"- **Fiabilité du taux** : avec {n} trades, le vrai taux est entre **{pct(ci_lo)} et {pct(ci_hi)}** "
             f"(intervalle de confiance à 95 %). "
             + ("Même le bas de la fourchette dépasse 60 % : solide." if ci_lo >= WIN_GOAL else
                "Le bas de la fourchette est sous 60 % : il faut plus de trades pour confirmer."))
    q = 1 - r["win_rate"]
    if 0 < q < 1:
        k = 5
        p5 = 100 * q ** k
        ms = r["max_losing_streak"]
        L.append(f"- **Risque de série** : probabilité de {k} pertes d'affilée à partir d'un trade donné = "
                 f"(1 − taux)^{k} = {q:.2f}^{k} = {'< 0,1' if p5 < 0.1 else f'{p5:.1f}'} %. Pire série observée : "
                 f"{ms} perte{'s' if ms > 1 else ''} d'affilée = jusqu'à {ms * (s + c):.0f} pts "
                 f"({ms * (s + c) * POINT_VALUE:.0f} $) si chaque perte va au stop.")
    sh = r.get("stable_halves")
    L.append("- **Stabilité** : " + {True: "objectif tenu sur la 1re ET la 2e moitié des jours ✅",
                                      False: "objectif PAS tenu sur une des deux moitiés des jours ❌",
                                      None: "non mesurable (moins de 4 jours)"}[sh])
    return L


def podium(rows: list[dict], min_trades: int, title: str) -> list[str]:
    ranked = sorted([r for r in rows if r["trades"] >= min_trades and r["expectancy"] > 0],
                    key=lambda r: (not meets(r, min_trades), -r["expectancy"] * math.sqrt(r["trades"])))
    seen, distinct = set(), []
    for r in ranked:  # one line per TECHNIQUE (its best exit), not 3 variants of the same one
        if r["setup"] not in seen:
            seen.add(r["setup"])
            distinct.append(r)
    ranked = distinct
    L = [f"### {title}", ""]
    if not ranked:
        L += ["_Aucune combinaison avec une espérance positive et assez de trades._", ""]
        return L
    for i, r in enumerate(ranked[:3], 1):
        tag = "✅ objectif atteint" if meets(r, min_trades) else "⚠️ objectif PAS atteint"
        L += [f"**{i}. {r['setup']} — {r['bracket']}** · {tag}", "",
              f"_Ce que fait la technique : {r['description']}_", "",
              f"Résultat : {r['trades']} trades ({r['long']} long / {r['short']} short), "
              f"{r['wins']} gagnants, {r['losses']} perdants, {r['green_days']}/{r['days']} jours verts.", ""]
        L += math_block(r)
        L += [f"- **Confiance** : {confidence(r)}", ""]
        seg = r.get("by_segment") or {}
        if seg:
            L.append("- **Par moment de la journée** : " + " · ".join(
                f"{k} {v['trades']} tr / {pct(v['win_rate'])} / {v['total']:+.1f} pts" for k, v in seg.items()))
            L.append("")
    return L


def decisions(rows: list[dict], min_trades: int) -> list[str]:
    L = ["| Technique | Meilleure sortie | Trades | % gagnants | Seuil | Espérance | Décision | Pourquoi |",
         "|---|---|---|---|---|---|---|---|"]
    best = best_per_setup(rows, min_trades)
    absent = [n for n in all_setups() if n not in best]
    for name, r in sorted(best.items(), key=lambda kv: (not meets(kv[1], min_trades), -kv[1]["expectancy"])):
        be = (r["stop"] + r["cost"]) / (r["target"] + r["stop"])
        if meets(r, min_trades):
            dec, why = "✅ GARDER", "≥ 60 %, espérance positive, assez de trades, stable"
        elif r["expectancy"] > 0 and r["trades"] >= min_trades:
            dec, why = "👀 SURVEILLER", ("rentable mais sous 60 % de gagnants" if r["win_rate"] < WIN_GOAL
                                         else "ne tient pas sur les deux moitiés")
        elif r["trades"] < min_trades:
            dec, why = "⏳ PAS ASSEZ DE DONNÉES", f"{r['trades']} trades (minimum {min_trades})"
        else:
            dec, why = "❌ ÉCARTER", "espérance négative même avec la meilleure sortie"
        L.append(f"| {name} | {r['bracket']} | {r['trades']} | {pct(r['win_rate'])} | {pct(be)} | "
                 f"{r['expectancy']:+.2f} | {dec} | {why} |")
    for name in absent:
        L.append(f"| {name} | — | 0 | — | — | — | ⏳ PAS ASSEZ DE DONNÉES | aucun signal sur la période |")
    return L


def market_section(day: dict) -> list[str]:
    L = []
    segs = [s for s in day.get("segments", []) if s.get("stats")]
    if not segs:
        return ["_Pas de données de séance._"]
    story = " → ".join(f"{s['name'].lower()} **{s['stats']['trend'].lower()}**" for s in segs)
    L += [f"**Le film de la journée :** {story}.", ""]
    d = day.get("day")
    if d:
        L += [f"Séance 9:30-16:00 : ouverture {d['open']:.2f}, plus haut {d['high']:.2f}, plus bas {d['low']:.2f}, "
              f"clôture {d['close']:.2f} → {d['net']:+.2f} pts, amplitude {d['range']:.2f} pts.", ""]
    L += ["| Moment | Heures | Mouvement | Amplitude | Qui pousse (delta) | Tendance | Par rapport au moment d'avant |",
          "|---|---|---|---|---|---|---|"]
    for s in segs:
        st = s["stats"]
        who = "acheteurs" if st["delta"] > 0 else "vendeurs"
        L.append(f"| {s['name']} | {st['start']}-{st['end']} | {st['net']:+.2f} pts | {st['range']:.2f} pts | "
                 f"{who} ({st['delta']:+d}) | **{st['trend']}** | {st['vs_previous']} |")
    L += ["", "_Tendance = mouvement net, efficacité (mouvement net ÷ chemin parcouru, mesurée sur 5 min ; "
              "≥ 0,30 = directionnel) et position de la clôture dans le range du moment._", ""]
    lv = day.get("levels") or {}
    if lv:
        L += ["**Niveaux clés du jour :** " + " · ".join(f"{k} {v:.2f}" for k, v in lv.items()), ""]
        rows = []
        for k in lv:
            states = [s["stats"]["levels"].get(k, "-") for s in segs]
            if any(x.startswith(("CASSÉ", "rejeté", "tenu")) for x in states):
                rows.append(f"| {k} | " + " | ".join(states) + " |")
        if rows:
            L += ["| Niveau | " + " | ".join(s["name"] for s in segs) + " |", "|---|" + "---|" * len(segs)] + rows + [""]
    return L


def stops_section(stop_tests: list[dict]) -> list[str]:
    if not stop_tests:
        return ["_Aucun signal de test de stop aujourd'hui._"]
    L = []
    agg: dict[str, float] = {}
    for t in stop_tests:
        runs = [r for r in t["runs"] if "pts" in r]
        if not runs:
            continue
        best = max(runs, key=lambda r: r["pts"])
        worst = min(runs, key=lambda r: r["pts"])
        L.append(f"- **{t['name']}** ({'LONG' if t['side'] > 0 else 'SHORT'} à {t['entry']:.2f}, {t['entry_t']}) : "
                 f"meilleur stop **{best['rule']}** {best['pts']:+.2f} pts ({usd(best['pts'])}), pire "
                 f"{worst['rule']} {worst['pts']:+.2f} pts. Le prix est allé jusqu'à {best['mfe']:.2f} pts en "
                 f"faveur et {best['mae']:.2f} pts contre.")
        for r in runs:
            agg[r["rule"]] = agg.get(r["rule"], 0.0) + r["pts"]
    if agg:
        top = sorted(agg.items(), key=lambda kv: -kv[1])
        L += ["", f"**Sur l'ensemble des tests du jour, le stop qui rapporte le plus : {top[0][0]} "
                  f"({top[0][1]:+.2f} pts)** ; le moins : {top[-1][0]} ({top[-1][1]:+.2f} pts).",
              "_Fixe = le stop ne bouge pas. Suiveur N = le stop suit le prix à N pts du meilleur prix atteint. "
              "BE = stop remonté au prix d'entrée après un gain. ATR = distance proportionnelle à la volatilité._"]
    return L


GLOSSAIRE = """## 7. Les maths en une minute

| Mesure | Formule | Ce que ça veut dire |
|---|---|---|
| Taux de réussite | gagnants ÷ trades | Ton objectif : ≥ 60 %. |
| Seuil de rentabilité | (stop + coûts) ÷ (objectif + stop) | Taux minimum pour ne PAS perdre d'argent avec cette sortie. Objectif 10 / stop 20 → (20 + 0,75) ÷ 30 = 69 %. |
| Espérance | taux × gain moyen − (1 − taux) × perte moyenne | Ce que rapporte un trade en moyenne. Doit être > 0. C'est LE chiffre qui compte. |
| Profit factor | gains totaux ÷ pertes totales | > 1 gagnant, > 1,5 solide, < 1 perdant. |
| Intervalle de confiance 95 % | formule de Wilson | Où se situe vraisemblablement le vrai taux. Avec peu de trades, la fourchette est large : 12 sur 20 (60 %) peut en réalité être entre 39 % et 78 %. |
| Série de pertes | (1 − taux)^k | Probabilité de k pertes d'affilée. À 60 % : 5 pertes d'affilée = 1 %. |
| Tient sur les 2 moitiés | objectif atteint sur la 1re ET la 2e moitié des jours | Protection contre le hasard : 144 combinaisons sont testées, certaines « gagnent » par chance. |

**Règle d'or :** une technique n'est retenue que si elle atteint 60 % ET une espérance positive sur au
moins 10 jours (idéalement 20), avec au moins 20 trades, et qu'elle tient sur chaque moitié de la
période. Un seul jour ne prouve rien.

Coûts inclus : 0,75 pt par aller-retour (commissions IBKR + glissement). 1 point MNQ = 2 $.
"""


def build(day_d: date, rep: Path) -> str:
    ds = day_d.isoformat()
    day_json = load(rep / f"{ds}.json")
    s1 = load(rep / f"scalp-{ds}.json")
    s10 = load(rep / "scalp-10-jours.json")
    s20 = load(rep / "scalp-20-jours.json")
    day = day_json[0]["day"] if day_json else None
    stop_tests = day_json[0]["stop_tests"] if day_json else []
    missing = [n for n, x in ((f"{ds}.json", day_json), (f"scalp-{ds}.json", s1), ("scalp-10-jours.json", s10),
                              ("scalp-20-jours.json", s20)) if x is None]
    ref = s20 if s20 and len(s20.get("days", [])) >= 10 else (s10 or s1)

    L = [f"# Rapport Hermès MNQ — {fr_date(day_d)}", ""]
    if missing:
        L += [f"> ⚠️ Fichiers manquants (sections incomplètes) : {', '.join(missing)}", ""]

    # 1. En bref
    L += ["## 1. En bref (30 secondes)", ""]
    if day:
        segs = [s for s in day["segments"] if s.get("stats")]
        L.append("- **Marché** : " + " → ".join(f"{s['name'].lower()} {s['stats']['trend'].lower()}" for s in segs)
                 + (f" ; séance {day['day']['net']:+.2f} pts." if day.get("day") else "."))
        if not day.get("complete_rth"):
            L.append(f"- ⚠️ Séance incomplète ({day.get('rth_bars', 0)} minutes enregistrées sur 390).")
    for label, sj in (("Aujourd'hui", s1), ("Sur 10 jours", s10), ("Sur 20 jours", s20)):
        if not sj:
            continue
        n_days = len(sj.get("days", []))
        mt = sj.get("min_trades", 20)
        ok = [r for r in sj["summary"] if meets(r, mt)]
        if ok:
            b = ok[0]
            L.append(f"- **{label}** ({n_days} j) : ✅ {len(ok)} combinaison(s) atteignent 60 %. Meilleure : "
                     f"**{b['setup']} ({b['bracket']})** — {pct(b['win_rate'])} de gagnants, "
                     f"{b['trades']} trades, {usd(b['total'])} par contrat.")
        else:
            pos = [r for r in sj["summary"] if r["expectancy"] > 0 and r["trades"] >= mt]
            extra = (f" Plus proche : {pos[0]['setup']} ({pos[0]['bracket']}) à {pct(pos[0]['win_rate'])}."
                     if pos else "")
            L.append(f"- **{label}** ({n_days} j) : ❌ aucune combinaison n'atteint 60 % avec espérance positive.{extra}")
    if stop_tests:
        agg: dict[str, float] = {}
        for t in stop_tests:
            for r in t["runs"]:
                if "pts" in r:
                    agg[r["rule"]] = agg.get(r["rule"], 0.0) + r["pts"]
        if agg:
            k, v = max(agg.items(), key=lambda kv: kv[1])
            L.append(f"- **Meilleur stop aujourd'hui** : {k} ({v:+.2f} pts sur les tests du jour).")
    if ref:
        n_days = len(ref.get("days", []))
        L.append("- **À retenir** : " + ("les décisions ci-dessous s'appuient sur "
                                        f"{n_days} jours." if n_days >= 10 else
                                        f"seulement {n_days} jour(s) de données — trop tôt pour valider une "
                                        "technique, on accumule."))
    L.append("")

    # 2. Marché
    L += ["## 2. Le marché aujourd'hui (ouverture, midi, fin de journée)", ""]
    L += market_section(day) if day else ["_Analyse du jour absente._", ""]

    # 3. Podium
    L += ["## 3. Les meilleurs tests — avec les calculs", ""]
    if ref:
        L += podium(ref["summary"], ref.get("min_trades", 20),
                    f"Sur la période la plus longue disponible ({len(ref.get('days', []))} jours)")
    if s1 and s1 is not ref:
        L += podium(s1["summary"], s1.get("min_trades", 5), "Aujourd'hui seulement (indicatif)")

    # 4. Tous les tests
    L += ["## 4. Tous les tests : garder, surveiller, écarter", ""]
    if ref:
        L += [f"Base : {len(ref.get('days', []))} jours, meilleure sortie de chaque technique parmi 18 "
              "(objectif 10/15/20 × stop 10/15/20 × avec/sans break-even). Le détail complet des 144 "
              "combinaisons est dans les fichiers scalp-*.md.", ""]
        L += decisions(ref["summary"], ref.get("min_trades", 20)) + [""]
        tested = len(ref["summary"])
        passing = sum(1 for r in ref["summary"] if meets(r, ref.get("min_trades", 20)))
        L += [f"Combinaisons ayant produit au moins un trade : {tested} (sur {max(len(all_setups()), 1) * 18}) · "
              f"atteignant l'objectif : {passing}.", ""]
    else:
        L += ["_Labo scalping absent._", ""]

    # 5. Stops
    L += ["## 5. Test du déplacement du stop (signaux du jour)", ""] + stops_section(stop_tests) + [""]

    # 6. Plan
    L += ["## 6. Ce qu'on fait demain", ""]
    if ref:
        keep = [r for r in best_per_setup(ref["summary"], ref.get("min_trades", 20)).values()
                if meets(r, ref.get("min_trades", 20))]
        if keep:
            for r in keep:
                seg = r.get("by_segment") or {}
                good = [k for k, v in seg.items() if v["win_rate"] >= WIN_GOAL and v["total"] > 0]
                L.append(f"- Suivre **{r['setup']}** avec **{r['bracket']}**"
                         + (f", surtout en {', '.join(x.lower() for x in good)}" if good else "") + ".")
        else:
            L.append("- Aucune technique validée : on continue d'enregistrer et de tester, sans trader ces "
                     "signaux en réel.")
        L.append("- Toute technique « à surveiller » doit être confirmée sur les jours suivants avant usage réel.")
    L.append("")
    L.append(GLOSSAIRE)
    L += ["", "_Généré automatiquement par tools/daily_report.py à partir des enregistrements Hermès (lecture "
              "seule). Fichiers détaillés : " + f"{ds}.md, scalp-{ds}.md, scalp-10-jours.md, scalp-20-jours.md._"]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True)
    ap.add_argument("--reports-dir", default="~/hermes-data/reports")
    ap.add_argument("--out", help="défaut : <reports-dir>/RAPPORT-<date>.md")
    args = ap.parse_args(argv)
    rep = Path(args.reports_dir).expanduser()
    d = date.fromisoformat(args.date)
    text = build(d, rep)
    out = Path(args.out).expanduser() if args.out else rep / f"RAPPORT-{d.isoformat()}.md"
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
