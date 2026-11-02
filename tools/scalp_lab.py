#!/usr/bin/env python3
"""Labo de scalping MNQ : quelles techniques donnent >= 60 % de trades gagnants ET une espérance positive ?

READ-ONLY : rejoue les enregistrements .hrec (mêmes barres 1 m que tools/day_analysis.py), simule
des petits trades long/short avec objectif 10/15/20 pts, stop 10/15/20 pts, avec ou sans stop remonté
à l'entrée (break-even), et classe les techniques. Aucune connexion IBKR, aucun ordre.

    python tools/scalp_lab.py --days 10                 # 10 derniers jours de trading (le vrai test)
    python tools/scalp_lab.py --date 2026-11-02          # un jour
    python tools/scalp_lab.py --synthetic                # démo sans enregistrement

Règles communes (voir docs/STRATEGIES.md) :
- signal calculé sur la barre 1 m CLOSE i (aucune donnée future), entrée à l'OUVERTURE de la barre i+1 ;
- une position à la fois par technique, pause de 2 barres après chaque sortie, entrées 09:31-15:45 NY,
  sortie forcée à 15:55 ;
- si l'objectif ET le stop sont touchés dans la même barre 1 m, le trade compte PERDANT (pessimiste) ;
- coûts : --cost points aller-retour (défaut 0,75 pt = commissions IBKR ~0,62 pt + glissement) ;
- gagnant = résultat net de coûts > 0.

Objectif utilisateur : >= 60 % de gagnants. Un taux seul ne suffit pas : avec objectif T et stop S, le
taux minimum pour ne pas perdre est (S + coût) / (T + S). Le verdict exige donc 60 % ET espérance > 0 ET
assez de trades (--min-trades) pour ne pas conclure sur du bruit.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from tools.day_analysis import (B, DEFAULT_ROOT, NoData, POINT_VALUE, SEGMENTS, collect, prev_rth,  # noqa: E402
                                slice_day, synthetic_day)

RTH_OPEN, LAST_ENTRY, FORCE_EXIT = dtime(9, 30), dtime(15, 45), dtime(15, 55)
TARGETS = (10.0, 15.0, 20.0)
STOPS = (10.0, 15.0, 20.0)
COOLDOWN = 2
WIN_GOAL = 0.60


# ------------------------------------------------------------------------------------- indicators
@dataclass(slots=True)
class Ctx:
    """Indicator values known at the CLOSE of each RTH bar (index-aligned with ``bars``)."""
    bars: list[B]
    vwap: list[float]
    ema9: list[float]
    ema21: list[float]
    atr: list[float]
    vol20: list[float]
    or_h: float | None
    or_l: float | None
    or_ready: int          # first index at which the 15-min opening range is known
    levels: dict[str, float]


def build_ctx(day_bars: list[B], prev: dict | None) -> Ctx | None:
    rth = [b for b in day_bars if RTH_OPEN <= b.t.time() < dtime(16, 0)]
    if len(rth) < 30:
        return None
    on = [b for b in day_bars if b.t.time() < RTH_OPEN or b.t.date() < rth[0].t.date()]
    vwap, ema9, ema21, atr, vol20 = [], [], [], [], []
    cv = cn = 0.0
    e9 = e21 = rth[0].c
    a = rth[0].h - rth[0].l
    for i, b in enumerate(rth):
        cv += b.vwap_num
        cn += b.v
        vwap.append(cv / cn if cn else b.c)
        e9 += (b.c - e9) * 2 / 10
        e21 += (b.c - e21) * 2 / 22
        ema9.append(e9)
        ema21.append(e21)
        tr = b.h - b.l if i == 0 else max(b.h - b.l, abs(b.h - rth[i - 1].c), abs(b.l - rth[i - 1].c))
        a += (tr - a) / 14
        atr.append(a)
        w = rth[max(0, i - 19):i + 1]
        vol20.append(sum(x.v for x in w) / len(w))
    orb = [i for i, b in enumerate(rth) if b.t.time() < dtime(9, 45)]
    or_h = max(rth[i].h for i in orb) if orb else None
    or_l = min(rth[i].l for i in orb) if orb else None
    levels: dict[str, float] = {}
    if prev:
        levels.update({"veille H": prev["high"], "veille L": prev["low"], "veille C": prev["close"]})
    if on:
        levels.update({"overnight H": max(b.h for b in on), "overnight L": min(b.l for b in on)})
    return Ctx(rth, vwap, ema9, ema21, atr, vol20, or_h, or_l, (orb[-1] + 1) if orb else 10**9, levels)


# ---------------------------------------------------------------------------------------- setups
# Each setup: f(ctx, i, state) -> +1 (long), -1 (short) or 0, using ONLY data up to bar i inclusive.
def s_vwap_rebond(c: Ctx, i: int, st: dict) -> int:
    """Tendance + retour sur le VWAP : la barre touche le VWAP et clôture du bon côté."""
    if i < 20:
        return 0
    b, v = c.bars[i], c.vwap[i]
    slope = c.vwap[i] - c.vwap[i - 10]
    if c.ema9[i] > c.ema21[i] and slope > 0 and b.l <= v + 1 and b.c > v and b.c > b.o:
        return 1
    if c.ema9[i] < c.ema21[i] and slope < 0 and b.h >= v - 1 and b.c < v and b.c < b.o:
        return -1
    return 0


def s_ema_pullback(c: Ctx, i: int, st: dict) -> int:
    """Tendance (EMA9 > EMA21, prix du bon côté du VWAP) + repli sur l'EMA21 avec delta confirmant."""
    if i < 21:
        return 0
    b = c.bars[i]
    if c.ema9[i] > c.ema21[i] and b.c > c.vwap[i] and b.l <= c.ema21[i] + 0.5 and b.c > c.ema21[i] \
            and b.c > b.o and b.delta > 0:
        return 1
    if c.ema9[i] < c.ema21[i] and b.c < c.vwap[i] and b.h >= c.ema21[i] - 0.5 and b.c < c.ema21[i] \
            and b.c < b.o and b.delta < 0:
        return -1
    return 0


def s_orb_cassure(c: Ctx, i: int, st: dict) -> int:
    """Cassure du range des 15 premières minutes (une fois par côté, avant 11:30)."""
    if i < c.or_ready or c.or_h is None or c.bars[i].t.time() >= dtime(11, 30):
        return 0
    b, p = c.bars[i], c.bars[i - 1]
    if b.c > c.or_h >= p.c and not st.get("up"):
        st["up"] = True
        return 1
    if b.c < c.or_l <= p.c and not st.get("dn"):
        st["dn"] = True
        return -1
    return 0


def s_orb_echec(c: Ctx, i: int, st: dict) -> int:
    """Fausse cassure du range 15 min : clôture dehors puis retour dedans dans les 10 barres => on fade."""
    if i < c.or_ready or c.or_h is None or c.bars[i].t.time() >= dtime(12, 0):
        return 0
    b = c.bars[i]
    recent = c.bars[max(c.or_ready, i - 10):i]
    if any(x.c > c.or_h for x in recent) and b.c < c.or_h - 1 and not st.get("fh"):
        st["fh"] = True
        return -1
    if any(x.c < c.or_l for x in recent) and b.c > c.or_l + 1 and not st.get("fl"):
        st["fl"] = True
        return 1
    return 0


def s_niveau_rejet(c: Ctx, i: int, st: dict) -> int:
    """Rejet d'un niveau clé (veille H/L/C, overnight H/L) : touche sans casser franchement, clôture repart."""
    if i < 1:
        return 0
    b, p = c.bars[i], c.bars[i - 1]
    for name, lv in c.levels.items():
        if p.c > lv and lv - 4 <= b.l <= lv + 1 and b.c >= lv + 2 and b.c > b.o and st.get(name) != i:
            st[name] = i
            return 1
        if p.c < lv and lv - 1 <= b.h <= lv + 4 and b.c <= lv - 2 and b.c < b.o and st.get(name) != i:
            st[name] = i
            return -1
    return 0


def s_retour_vwap(c: Ctx, i: int, st: dict) -> int:
    """Excès : prix à plus de 3 ATR du VWAP + barre de retournement avec delta qui change de camp."""
    if i < 20:
        return 0
    b = c.bars[i]
    d = b.c - c.vwap[i]
    k = 3 * c.atr[i]
    if d > k and b.c < b.o and b.delta < 0:
        return -1
    if d < -k and b.c > b.o and b.delta > 0:
        return 1
    return 0


def s_momentum_delta(c: Ctx, i: int, st: dict) -> int:
    """3 barres consécutives dans le même sens, delta confirmant, volume > 1,5 x moyenne 20, côté VWAP."""
    if i < 22:
        return 0
    w = c.bars[i - 2:i + 1]
    b = c.bars[i]
    if b.v < 1.5 * c.vol20[i - 1]:
        return 0
    if all(x.c > x.o and x.delta > 0 for x in w) and b.c > c.vwap[i]:
        return 1
    if all(x.c < x.o and x.delta < 0 for x in w) and b.c < c.vwap[i]:
        return -1
    return 0


def s_epuisement(c: Ctx, i: int, st: dict) -> int:
    """Nouveau plus haut/bas 20 barres avec longue mèche (>= 60 % du range) et delta contraire => fade."""
    if i < 20:
        return 0
    b = c.bars[i]
    rng = b.h - b.l
    if rng < 2:
        return 0
    prior = c.bars[i - 20:i]
    if b.h > max(x.h for x in prior) and (b.h - max(b.o, b.c)) >= 0.6 * rng and b.delta < 0:
        return -1
    if b.l < min(x.l for x in prior) and (min(b.o, b.c) - b.l) >= 0.6 * rng and b.delta > 0:
        return 1
    return 0


SETUPS = {
    "VWAP rebond (tendance)": s_vwap_rebond,
    "EMA21 pullback (tendance)": s_ema_pullback,
    "ORB15 cassure": s_orb_cassure,
    "ORB15 fausse cassure (fade)": s_orb_echec,
    "Rejet niveau clé": s_niveau_rejet,
    "Retour au VWAP (excès)": s_retour_vwap,
    "Momentum + delta": s_momentum_delta,
    "Épuisement (mèche + delta)": s_epuisement,
}


# ------------------------------------------------------------------------------------ simulation
@dataclass(frozen=True, slots=True)
class Bracket:
    target: float
    stop: float
    be: bool  # stop moved to entry + 1 tick once price has gone +60 % of the target

    @property
    def label(self) -> str:
        return f"obj {self.target:g} / stop {self.stop:g}" + (" / BE" if self.be else "")


BRACKETS = tuple(Bracket(t, s, be) for t in TARGETS for s in STOPS for be in (False, True))


@dataclass(slots=True)
class Trade:
    setup: str
    bracket: str
    day: str
    segment: str
    side: int
    entry_t: str
    entry: float
    exit_t: str
    exit: float
    pts: float           # net of costs
    outcome: str         # OBJECTIF | STOP | BE | TEMPS
    ambiguous: bool


def segment_of(t: dtime) -> str:
    for name, a, z in SEGMENTS:
        if a <= t < z:
            return name
    return "?"


def run_bracket(bars: list[B], j: int, side: int, entry: float, br: Bracket, cost: float):
    """Walk from bar j (entry bar, entered at its open). Returns (exit_idx, exit_px, outcome, ambiguous)."""
    tgt = entry + side * br.target
    stop = entry - side * br.stop
    be_trig = entry + side * 0.6 * br.target
    moved = False
    for k in range(j, len(bars)):
        b = bars[k]
        if b.t.time() >= FORCE_EXIT:
            return k, b.o, "TEMPS", False
        hit_stop = b.l <= stop if side > 0 else b.h >= stop
        hit_tgt = b.h >= tgt if side > 0 else b.l <= tgt
        if hit_stop:
            # gap through the stop: filled at the open
            px = min(stop, b.o) if side > 0 else max(stop, b.o)
            return k, px, "BE" if moved else "STOP", hit_tgt
        if hit_tgt:
            return k, tgt, "OBJECTIF", False
        if br.be and not moved and ((b.h >= be_trig) if side > 0 else (b.l <= be_trig)):
            stop = entry + side * 0.25
            moved = True
    k = len(bars) - 1
    return k, bars[k].c, "TEMPS", False


def simulate_day(ctx: Ctx, day: str, cost: float) -> list[Trade]:
    out: list[Trade] = []
    bars = ctx.bars
    n = len(bars)
    for name, fn in SETUPS.items():
        # signals are independent of the bracket: compute them once (state per setup per day)
        st: dict = {}
        sigs = [fn(ctx, i, st) if bars[i].t.time() < LAST_ENTRY else 0 for i in range(n - 1)]
        for br in BRACKETS:
            i = 0
            while i < n - 1:
                side = sigs[i]
                if not side:
                    i += 1
                    continue
                j = i + 1
                entry = bars[j].o
                k, px, outcome, amb = run_bracket(bars, j, side, entry, br, cost)
                pts = side * (px - entry) - cost
                out.append(Trade(name, br.label, day, segment_of(bars[j].t.time()), side,
                                 bars[j].t.strftime("%H:%M"), entry, bars[k].t.strftime("%H:%M"), px,
                                 round(pts, 2), outcome, amb))
                i = k + 1 + COOLDOWN
    return out


# ---------------------------------------------------------------------------------------- report
def _meets(pnl: list[float]) -> bool:
    return bool(pnl) and sum(1 for p in pnl if p > 0) / len(pnl) >= WIN_GOAL and sum(pnl) > 0


def summarize(trades: list[Trade], cost: float) -> list[dict]:
    all_days = sorted({t.day for t in trades})
    half = set(all_days[:len(all_days) // 2])
    groups: dict[tuple[str, str], list[Trade]] = {}
    for t in trades:
        groups.setdefault((t.setup, t.bracket), []).append(t)
    rows = []
    for (setup, br), ts in groups.items():
        pnl = [t.pts for t in ts]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p <= 0]
        streak = worst = 0
        for p in pnl:
            streak = streak + 1 if p <= 0 else 0
            worst = max(worst, streak)
        by_day: dict[str, float] = {}
        for t in ts:
            by_day[t.day] = by_day.get(t.day, 0.0) + t.pts
        seg: dict[str, list[float]] = {}
        for t in ts:
            seg.setdefault(t.segment, []).append(t.pts)
        bracket = next(b for b in BRACKETS if b.label == br)
        # robustness against data snooping (144 combinations tested): the goal must hold on EACH half
        stable = None
        if len(all_days) >= 4:
            stable = (_meets([t.pts for t in ts if t.day in half])
                      and _meets([t.pts for t in ts if t.day not in half]))
        rows.append({
            "setup": setup, "bracket": br, "trades": len(ts), "win_rate": len(wins) / len(ts),
            "expectancy": sum(pnl) / len(ts), "total": sum(pnl),
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
            "max_losing_streak": worst, "days": len(by_day),
            "green_days": sum(1 for v in by_day.values() if v > 0),
            "ambiguous": sum(1 for t in ts if t.ambiguous),
            "breakeven_win_rate": min(1.0, (bracket.stop + cost) / (bracket.target + bracket.stop)),
            "stable_halves": stable,
            "long": sum(1 for t in ts if t.side > 0), "short": sum(1 for t in ts if t.side < 0),
            "by_segment": {k: {"trades": len(v), "win_rate": sum(1 for p in v if p > 0) / len(v),
                               "total": sum(v)} for k, v in seg.items()},
        })
    rows.sort(key=lambda r: (-r["expectancy"], -r["win_rate"]))
    return rows


def verdict(rows: list[dict], min_trades: int) -> tuple[list[dict], list[dict]]:
    ok = [r for r in rows if r["win_rate"] >= WIN_GOAL and r["expectancy"] > 0 and r["trades"] >= min_trades
          and r["stable_halves"] is not False]
    near = [r for r in rows if r not in ok and r["expectancy"] > 0 and r["trades"] >= min_trades]
    near.sort(key=lambda r: -r["win_rate"])
    return ok, near


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def row_md(r: dict) -> str:
    pf = "∞" if r["profit_factor"] == float("inf") else f"{r['profit_factor']:.2f}"
    return (f"| {r['setup']} | {r['bracket']} | {r['trades']} ({r['long']}L/{r['short']}S) | **{pct(r['win_rate'])}** | "
            f"{pct(r['breakeven_win_rate'])} | {r['expectancy']:+.2f} | {r['total']:+.1f} | "
            f"{r['total'] * POINT_VALUE:+.0f} $ | {pf} | {r['max_losing_streak']} | {r['green_days']}/{r['days']} | "
            f"{ {True: 'oui', False: 'NON', None: '—'}[r['stable_halves']] } |")


HEAD = ("| Technique | Objectif / stop | Trades | % gagnants | % mini rentable | Espérance pts | Total pts | "
        "Total $ (1 MNQ) | Profit factor | Pertes d'affilée max | Jours verts | Tient sur les 2 moitiés |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|")


def render(rows: list[dict], days: list[str], trades: list[Trade], cost: float, min_trades: int) -> str:
    ok, near = verdict(rows, min_trades)
    L = [f"# Labo scalping MNQ — {days[0]}" + (f" → {days[-1]} ({len(days)} jours)" if len(days) > 1 else ""), "",
         f"Objectif : **≥ {pct(WIN_GOAL)} de gagnants ET espérance > 0** après {cost:g} pt de coûts aller-retour, "
         f"au moins {min_trades} trades, et (à partir de 4 jours) objectif tenu sur CHAQUE moitié de la période "
         f"(protection contre le hasard : {len(SETUPS) * len(BRACKETS)} combinaisons sont testées). Entrée à l'ouverture de la barre suivant le signal ; objectif et stop "
         f"touchés dans la même minute = compté perdant.", "", "## Verdict", ""]
    if ok:
        L.append(f"✅ **{len(ok)} combinaison(s) atteignent l'objectif** :")
        L += ["", HEAD] + [row_md(r) for r in ok[:10]]
    else:
        L.append("❌ **Aucune technique n'atteint 60 % de gagnants avec une espérance positive** sur cet échantillon.")
        if near:
            L += ["", "Les plus proches (espérance positive, classées par % de gagnants) :", "", HEAD]
            L += [row_md(r) for r in near[:5]]
    if len(days) < 10:
        L += ["", f"> ⚠️ {len(days)} jour(s) seulement : un résultat sur un jour n'est PAS une technique validée. "
                  "Juger sur `--days 10` minimum, idéalement 20+, et vérifier que le résultat tient sur chaque "
                  "moitié de la période."]
    best = (ok or near or rows)[:3]
    if best:
        L += ["", "## Quand ça marche (par segment) — meilleures combinaisons", "",
              "| Technique | Objectif / stop | OUVERTURE | MIDI | FIN |", "|---|---|---|---|---|"]
        for r in best:
            cells = []
            for s, _, _ in SEGMENTS:
                x = r["by_segment"].get(s)
                cells.append("—" if not x else f"{x['trades']} tr, {pct(x['win_rate'])}, {x['total']:+.1f} pts")
            L.append(f"| {r['setup']} | {r['bracket']} | " + " | ".join(cells) + " |")
    L += ["", "## Meilleure configuration de chaque technique", "", HEAD]
    seen = set()
    for r in rows:
        if r["setup"] in seen or r["trades"] == 0:
            continue
        seen.add(r["setup"])
        L.append(row_md(r))
    if len(days) == 1 and best:
        b0 = best[0]
        L += ["", f"## Trades du jour — {b0['setup']} ({b0['bracket']})", "",
              "| Entrée | Sens | Prix | Sortie | Prix | Résultat | Net pts |", "|---|---|---|---|---|---|---|"]
        for t in trades:
            if t.setup == b0["setup"] and t.bracket == b0["bracket"]:
                L.append(f"| {t.entry_t} | {'LONG' if t.side > 0 else 'SHORT'} | {t.entry:.2f} | {t.exit_t} | "
                         f"{t.exit:.2f} | {t.outcome}{' (ambigu)' if t.ambiguous else ''} | {t.pts:+.2f} |")
    amb = sum(r["ambiguous"] for r in rows)
    L += ["", f"_Trades ambigus (objectif et stop dans la même minute, comptés perdants) : {amb}. "
              "Les barres 30 s réduiraient cette incertitude._"]
    return "\n".join(L)


# ------------------------------------------------------------------------------------------ main
def run(bars: list[B], days: list[date], cost: float) -> tuple[list[Trade], list[str]]:
    trades: list[Trade] = []
    done = []
    for d in days:
        ctx = build_ctx(slice_day(bars, d), prev_rth(bars, d))
        if ctx is None:
            continue
        trades += simulate_day(ctx, d.isoformat(), cost)
        done.append(d.isoformat())
    return trades, done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT))
    ap.add_argument("--date", help="jour de trading YYYY-MM-DD (heure de New York)")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--lookback", type=int, default=0)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--cost", type=float, default=0.75, help="coûts aller-retour en points (commissions + glissement)")
    ap.add_argument("--min-trades", type=int, default=0, help="défaut : 5 pour un jour, 20 pour plusieurs jours")
    ap.add_argument("--with-depth", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--json")
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args(argv)

    if args.synthetic:
        bars, ctx = synthetic_day()
        days = sorted({b.t.date() for b in bars})
    else:
        cfg = load_config(args.config)
        want = date.fromisoformat(args.date) if args.date else None
        try:
            bars, days, _ = collect(Path(args.path).expanduser(), want, args.days, args.lookback, cfg, args.with_depth)
        except NoData as e:
            print(e.args[0], file=sys.stderr)
            return e.args[1]
    trades, done = run(bars, days, args.cost)
    if not done:
        print("Pas assez de barres RTH pour simuler (minimum 30 minutes)", file=sys.stderr)
        return 3
    min_trades = args.min_trades or (5 if len(done) == 1 else 20)
    rows = summarize(trades, args.cost)
    text = render(rows, done, trades, args.cost, min_trades)
    print(text)
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
    if args.json:
        ok, near = verdict(rows, min_trades)
        Path(args.json).expanduser().write_text(json.dumps(
            {"days": done, "cost": args.cost, "min_trades": min_trades, "goal_met": ok, "summary": rows,
             "trades": [asdict(t) for t in trades]}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
