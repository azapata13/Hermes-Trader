#!/usr/bin/env python3
"""Analyse journalière MNQ : niveaux, tendance ouverture / midi / fin de journée, test de stops.

READ-ONLY : rejoue des enregistrements .hrec (Normalizer + MarketEngine, barres C5) et écrit un
rapport. Aucune connexion IBKR, aucun ordre.

    python tools/day_analysis.py                          # dernier jour sous ~/hermes-data/recordings
    python tools/day_analysis.py --date 2026-11-02         # un jour de trading précis (heure de New York)
    python tools/day_analysis.py --days 10                 # les 10 derniers jours + agrégat des stops
    python tools/day_analysis.py <session_dir|date_dir> --out rapport.md --json rapport.json
    python tools/day_analysis.py --synthetic               # démo sans enregistrement

Segments (heure de New York, configurables) : OUVERTURE 09:30-11:00, MIDI 11:00-14:00, FIN 14:00-16:00.

Pour chaque segment : O/H/L/C, range, net, VWAP, delta (achat-vente agressif), pente, ratio
d'efficience (|net| / chemin parcouru), position de clôture dans le range => TENDANCE HAUSSIÈRE /
BAISSIÈRE / RANGE, et continuation ou retournement par rapport au segment précédent.

Niveaux : veille H/L/C, overnight H/L, opening range 30 min, initial balance 60 min, VWAP séance.
Pour chaque niveau : touché / cassé / rejeté, par segment.

Test des stops (pas de look-ahead : le signal n'utilise que des barres déjà closes) :
  ORB     cassure (clôture 1 m) de l'opening range 30 min entre 10:00 et 11:00
  MIDI    à 11:00, sens = position vs VWAP séance (confirmé par le delta cumulé si possible)
  FIN     à 14:00, même règle
Sortie à 15:59 (clôture) ou au stop. Variantes : stop fixe, stop suiveur, break-even + suiveur,
suiveur ATR(14 x 5 m), stop structurel. Hypothèse pessimiste : dans une barre 1 m, le stop est
testé AVANT que l'extrême favorable ne fasse avancer le suiveur. Glissement configurable.
MNQ = 2 $ / point.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes.config import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from hermes.ibkr.normalizer import Normalizer  # noqa: E402
from hermes.market.engine import MarketEngine  # noqa: E402

NY = ZoneInfo("America/New_York")
DEFAULT_ROOT = Path("~/hermes-data/recordings").expanduser()
POINT_VALUE = 2.0  # MNQ $/point
SEGMENTS = (("OUVERTURE", dtime(9, 30), dtime(11, 0)),
            ("MIDI", dtime(11, 0), dtime(14, 0)),
            ("FIN", dtime(14, 0), dtime(16, 0)))


# --------------------------------------------------------------------------------------------- data
@dataclass(slots=True)
class B:
    """1-minute bar in POINTS (floats) with New York local time."""
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: int
    buy: int
    sell: int
    vwap_num: float  # sum(price_pts * size)

    @property
    def delta(self) -> int:
        return self.buy - self.sell


def replay_bars(raws, cfg) -> tuple[list[B], dict]:
    bcfg = dataclasses.replace(cfg.bars, enabled=True, history_30s=10**7, history_1m=10**7, history_5m=10**7)
    norm = Normalizer()
    eng = MarketEngine(cfg.book, cfg.session, cfg.subscriptions, tape_cfg=cfg.tape, bars_cfg=bcfg)
    for raw in raws:
        for ev in norm.normalize(raw):
            eng.on_event(ev)
    out: list[B] = []
    ctx: dict = {}
    for inst in eng.instruments.values():
        if inst.bars is None or inst.grid is None:
            continue
        g = inst.grid
        px = g.to_price
        unit = px(1)
        for x in inst.bars.completed_bars(60):
            if not x.volume:
                continue
            out.append(B(datetime.fromtimestamp(x.start_s, NY), px(x.open), px(x.high), px(x.low), px(x.close),
                         x.volume, x.buy_volume, x.sell_volume, x.vwap_num * unit))
        ss = inst.sessions.snapshot()
        p = ss.previous
        if p is not None:
            ctx["prev"] = {"date": p.trading_date, "high": px(p.high), "low": px(p.low), "close": px(p.close),
                           "complete": p.observed_from_open and not p.gap_observed}
        ctx["symbol"] = inst.local_symbol
    return out, ctx


def merge_bars(lists: list[list[B]]) -> list[B]:
    best: dict[datetime, B] = {}
    for bl in lists:
        for b in bl:
            if b.t not in best or b.v > best[b.t].v:
                best[b.t] = b
    return [best[k] for k in sorted(best)]


# ----------------------------------------------------------------------------------------- analysis
def in_win(b: B, a: dtime, z: dtime) -> bool:
    return a <= b.t.time() < z


def stats(bars: list[B]) -> dict | None:
    if not bars:
        return None
    o, c = bars[0].o, bars[-1].c
    h, lo = max(b.h for b in bars), min(b.l for b in bars)
    v = sum(b.v for b in bars)
    # efficiency on 5-minute closes (1-minute noise would hide clean trends)
    pts = [o] + [bars[i].c for i in range(4, len(bars), 5)] + ([c] if len(bars) % 5 else [])
    path = sum(abs(pts[i] - pts[i - 1]) for i in range(1, len(pts)))
    n = len(bars)
    xs = list(range(n))
    mx, my = (n - 1) / 2, sum(b.c for b in bars) / n
    den = sum((x - mx) ** 2 for x in xs) or 1
    slope = sum((x - mx) * (b.c - my) for x, b in zip(xs, bars)) / den  # pts / minute
    rng = h - lo
    er = abs(c - o) / path if path else 0.0
    pos = (c - lo) / rng if rng else 0.5
    net = c - o
    if er >= 0.3 and net > 0 and pos >= 0.6:
        trend = "HAUSSIÈRE"
    elif er >= 0.3 and net < 0 and pos <= 0.4:
        trend = "BAISSIÈRE"
    else:
        trend = "RANGE"
    return {"open": o, "high": h, "low": lo, "close": c, "range": rng, "net": net, "volume": v,
            "vwap": sum(b.vwap_num for b in bars) / v if v else None,
            "delta": sum(b.delta for b in bars), "slope_pts_h": slope * 60, "efficiency": er,
            "close_pos": pos, "trend": trend, "start": bars[0].t.strftime("%H:%M"),
            "end": bars[-1].t.strftime("%H:%M"), "bars": n}


def level_status(bars: list[B], lvl: float, tol: float = 1.0) -> str:
    if not bars:
        return "-"
    hi, lo = max(b.h for b in bars), min(b.l for b in bars)
    o, c = bars[0].o, bars[-1].c
    if lo - tol > lvl:
        return "au-dessus"
    if hi + tol < lvl:
        return "en-dessous"
    if o < lvl <= c:
        return "CASSÉ ↑"
    if o > lvl >= c:
        return "CASSÉ ↓"
    if o <= lvl and c <= lvl:
        return "rejeté (résistance)"
    return "tenu (support)"


def analyse_day(bars: list[B], ctx: dict) -> dict:
    rth = [b for b in bars if in_win(b, dtime(9, 30), dtime(16, 0))]
    on = [b for b in bars if not in_win(b, dtime(9, 30), dtime(16, 0)) and b.t.time() < dtime(9, 30)]
    levels: dict[str, float] = {}
    if "prev" in ctx:
        levels.update({"Veille H": ctx["prev"]["high"], "Veille L": ctx["prev"]["low"],
                       "Veille C": ctx["prev"]["close"]})
    if on:
        levels.update({"Overnight H": max(b.h for b in on), "Overnight L": min(b.l for b in on)})
    orb = [b for b in rth if b.t.time() < dtime(10, 0)]
    ib = [b for b in rth if b.t.time() < dtime(10, 30)]
    if orb:
        levels.update({"OR30 H": max(b.h for b in orb), "OR30 L": min(b.l for b in orb)})
    if ib:
        levels.update({"IB60 H": max(b.h for b in ib), "IB60 L": min(b.l for b in ib)})
    v = sum(b.v for b in rth)
    if v:
        levels["VWAP séance"] = sum(b.vwap_num for b in rth) / v
    segs = []
    prev_trend = None
    cum_delta = 0
    for name, a, z in SEGMENTS:
        sb = [b for b in rth if in_win(b, a, z)]
        st = stats(sb)
        if st is None:
            segs.append({"name": name, "stats": None})
            continue
        cum_delta += st["delta"]
        st["cum_delta"] = cum_delta
        # session VWAP at segment end (bars up to end)
        upto = [b for b in rth if b.t.time() < z]
        vv = sum(b.v for b in upto)
        st["session_vwap_end"] = sum(b.vwap_num for b in upto) / vv if vv else None
        if prev_trend is None or st["trend"] == "RANGE" or prev_trend == "RANGE":
            rel = "—" if prev_trend is None else f"{prev_trend} → {st['trend']}"
        else:
            rel = "CONTINUATION" if st["trend"] == prev_trend else "RETOURNEMENT"
        st["vs_previous"] = rel
        st["levels"] = {k: level_status(sb, lv) for k, lv in levels.items()}
        prev_trend = st["trend"]
        segs.append({"name": name, "stats": st})
    return {"date": rth[0].t.date().isoformat() if rth else (bars[0].t.date().isoformat() if bars else "?"),
            "symbol": ctx.get("symbol", "MNQ"), "levels": levels, "segments": segs,
            "day": stats(rth), "overnight": stats(on), "prev": ctx.get("prev"),
            "rth_bars": len(rth), "complete_rth": len(rth) >= 380}


# --------------------------------------------------------------------------------------- stop tests
def atr5(bars: list[B], n: int = 14) -> float | None:
    fives: list[tuple[float, float, float]] = []
    for i in range(0, len(bars) - len(bars) % 5, 5):
        ch = bars[i:i + 5]
        fives.append((max(b.h for b in ch), min(b.l for b in ch), ch[-1].c))
    if len(fives) < 2:
        return None
    trs = [max(h - l, abs(h - fives[i - 1][2]), abs(l - fives[i - 1][2])) for i, (h, l, _) in enumerate(fives) if i]
    trs = trs[-n:]
    return sum(trs) / len(trs)


@dataclass(slots=True)
class StopRule:
    name: str
    kind: str          # fixed | trail | be_trail | atr_trail | struct
    dist: float = 0.0  # points (or ATR multiple for atr_trail)
    be_after: float = 0.0


STOP_RULES = (
    [StopRule(f"Fixe {d:g}", "fixed", d) for d in (10, 15, 20, 30, 40)]
    + [StopRule(f"Suiveur {d:g}", "trail", d) for d in (10, 15, 20, 30)]
    + [StopRule("BE@+10 puis suiveur 15", "be_trail", 15, 10), StopRule("BE@+15 puis suiveur 20", "be_trail", 20, 15)]
    + [StopRule(f"Suiveur ATR x{k:g}", "atr_trail", k) for k in (1.0, 1.5, 2.0)]
    + [StopRule("Structurel (extrême opposé)", "struct")]
)


def simulate(path: list[B], side: int, entry: float, rule: StopRule, atr: float | None, struct_stop: float,
             slip: float) -> dict:
    """side +1 long / -1 short. path = bars AFTER the entry bar up to the exit time."""
    if rule.kind == "atr_trail":
        if not atr:
            return {"skip": "ATR indisponible"}
        dist = rule.dist * atr
    elif rule.kind == "struct":
        dist = abs(entry - struct_stop)
    else:
        dist = rule.dist
    stop = entry - side * dist
    best = entry
    mae = mfe = 0.0
    for b in path:
        adverse = b.l if side > 0 else b.h
        fav = b.h if side > 0 else b.l
        if (side > 0 and adverse <= stop) or (side < 0 and adverse >= stop):
            # gap through the stop: fill at the worse of stop and open
            fill = min(stop, b.o) if side > 0 else max(stop, b.o)
            fill -= side * slip
            mae = max(mae, side * (entry - adverse))
            return {"pts": side * (fill - entry), "exit": "STOP", "exit_t": b.t.strftime("%H:%M"),
                    "mae": mae, "mfe": mfe, "stop_dist": dist}
        mae = max(mae, side * (entry - adverse))
        mfe = max(mfe, side * (fav - entry))
        best = max(best, fav) if side > 0 else min(best, fav)
        if rule.kind in ("trail", "atr_trail"):
            cand = best - side * dist
            stop = max(stop, cand) if side > 0 else min(stop, cand)
        elif rule.kind == "be_trail" and mfe >= rule.be_after:
            cand = best - side * dist
            be = entry
            cand = max(cand, be) if side > 0 else min(cand, be)
            stop = max(stop, cand) if side > 0 else min(stop, cand)
    if not path:
        return {"skip": "pas de barres après l'entrée"}
    last = path[-1]
    return {"pts": side * (last.c - entry) - slip, "exit": "CLÔTURE", "exit_t": last.t.strftime("%H:%M"),
            "mae": mae, "mfe": mfe, "stop_dist": dist}


def signals(rth: list[B]) -> list[dict]:
    out = []
    orb = [b for b in rth if b.t.time() < dtime(10, 0)]
    if len(orb) >= 20:
        oh, ol = max(b.h for b in orb), min(b.l for b in orb)
        for i, b in enumerate(rth):
            if not dtime(10, 0) <= b.t.time() < dtime(11, 0):
                continue
            if b.c > oh or b.c < ol:
                side = 1 if b.c > oh else -1
                out.append({"name": "ORB 30 min", "idx": i, "side": side, "entry": b.c,
                            "struct": ol if side > 0 else oh, "why": f"clôture {b.c:.2f} hors OR [{ol:.2f}-{oh:.2f}]"})
                break
    for name, t0 in (("MIDI 11:00", dtime(11, 0)), ("FIN 14:00", dtime(14, 0))):
        before = [i for i, b in enumerate(rth) if b.t.time() < t0]
        if len(before) < 30:
            continue
        i = before[-1]
        upto = rth[:i + 1]
        v = sum(b.v for b in upto)
        vwap = sum(b.vwap_num for b in upto) / v
        cd = sum(b.delta for b in upto)
        px = rth[i].c
        side = 1 if px >= vwap else -1
        conf = "delta confirme" if (cd > 0) == (side > 0) else "delta CONTRE"
        seg = [b for b in upto if b.t.time() >= (dtime(9, 30) if t0 == dtime(11, 0) else dtime(11, 0))]
        struct = min(b.l for b in seg) if side > 0 else max(b.h for b in seg)
        out.append({"name": name, "idx": i, "side": side, "entry": px, "struct": struct,
                    "why": f"prix {px:.2f} {'>' if side > 0 else '<'} VWAP {vwap:.2f}, delta cumulé {cd:+d} ({conf})"})
    return out


def stop_tests(bars: list[B], slip: float) -> list[dict]:
    rth = [b for b in bars if in_win(b, dtime(9, 30), dtime(16, 0))]
    res = []
    for s in signals(rth):
        path = rth[s["idx"] + 1:]
        atr = atr5(rth[:s["idx"] + 1])
        runs = []
        for r in STOP_RULES:
            sim = simulate(path, s["side"], s["entry"], r, atr, s["struct"], slip)
            runs.append({"rule": r.name, **sim})
        res.append({**{k: s[k] for k in ("name", "side", "entry", "why")},
                    "entry_t": rth[s["idx"]].t.strftime("%H:%M"), "atr5": atr, "runs": runs})
    return res


# ----------------------------------------------------------------------------------------- report
def f(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def render(day: dict, tests: list[dict]) -> str:
    L = [f"# Analyse Hermès {day['symbol']} — {day['date']}", ""]
    if not day["complete_rth"]:
        L += [f"> ⚠️ Séance RTH incomplète ({day['rth_bars']} barres 1 m / 390) : interpréter avec prudence.", ""]
    d = day["day"]
    if d:
        L += [f"**Séance RTH** O {f(d['open'])} H {f(d['high'])} L {f(d['low'])} C {f(d['close'])} — "
              f"range {f(d['range'])} pts, net {d['net']:+.2f}, delta {d['delta']:+d}, tendance **{d['trend']}**", ""]
    L += ["## Niveaux clés", "", "| Niveau | Prix |", "|---|---|"]
    L += [f"| {k} | {f(v)} |" for k, v in day["levels"].items()]
    L += ["", "## Ouverture / Midi / Fin de journée", "",
          "| Segment | Heures | O | H | L | C | Net | Range | Delta | Pente pts/h | Efficience | Tendance | vs précédent |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in day["segments"]:
        st = s["stats"]
        if st is None:
            L.append(f"| {s['name']} | pas de données |" + " |" * 11)
            continue
        L.append(f"| {s['name']} | {st['start']}-{st['end']} | {f(st['open'])} | {f(st['high'])} | {f(st['low'])} | "
                 f"{f(st['close'])} | {st['net']:+.2f} | {f(st['range'])} | {st['delta']:+d} | "
                 f"{st['slope_pts_h']:+.1f} | {st['efficiency']:.2f} | **{st['trend']}** | {st['vs_previous']} |")
    L += ["", "### Comportement aux niveaux", ""]
    names = [s["name"] for s in day["segments"] if s["stats"]]
    if names:
        L += ["| Niveau | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for k in day["levels"]:
            L.append(f"| {k} | " + " | ".join(s["stats"]["levels"][k] for s in day["segments"] if s["stats"]) + " |")
    L += ["", "### Lecture", ""]
    for s in day["segments"]:
        st = s["stats"]
        if not st:
            continue
        side = "acheteurs" if st["delta"] > 0 else "vendeurs"
        vw = st.get("session_vwap_end")
        rel = "au-dessus du" if vw is not None and st["close"] >= vw else "sous le"
        L.append(f"- **{s['name']}** : {st['trend'].lower()} (efficience {st['efficiency']:.2f}, clôture à "
                 f"{st['close_pos'] * 100:.0f}% du range), agression nette côté {side} ({st['delta']:+d}), "
                 f"clôture {rel} VWAP séance ({f(vw)}). {st['vs_previous']}.")
    L += ["", "## Test des stops (déplacement du stop)", "",
          "Points par contrat (MNQ = 2 $/pt). Sortie au stop ou à la clôture 15:59. Pessimiste intrabarre.", ""]
    if not tests:
        L.append("_Aucun signal exploitable (données insuffisantes)._")
    for t in tests:
        L += [f"### {t['name']} — {'LONG' if t['side'] > 0 else 'SHORT'} @ {f(t['entry'])} ({t['entry_t']})",
              f"_{t['why']}; ATR(14×5 m) = {f(t['atr5'])}_", "",
              "| Stop | Distance | Résultat pts | $ | Sortie | MAE | MFE |", "|---|---|---|---|---|---|---|"]
        for r in t["runs"]:
            if "skip" in r:
                L.append(f"| {r['rule']} | — | {r['skip']} | | | | |")
                continue
            L.append(f"| {r['rule']} | {f(r['stop_dist'])} | {r['pts']:+.2f} | {r['pts'] * POINT_VALUE:+.0f} | "
                     f"{r['exit']} {r['exit_t']} | {f(r['mae'])} | {f(r['mfe'])} |")
        L.append("")
    return "\n".join(L)


def render_aggregate(days: list[tuple[dict, list[dict]]]) -> str:
    agg: dict[tuple[str, str], list[float]] = {}
    trends: dict[str, dict[str, int]] = {}
    for day, tests in days:
        for s in day["segments"]:
            if s["stats"]:
                trends.setdefault(s["name"], {}).setdefault(s["stats"]["trend"], 0)
                trends[s["name"]][s["stats"]["trend"]] += 1
        for t in tests:
            for r in t["runs"]:
                if "pts" in r:
                    agg.setdefault((t["name"], r["rule"]), []).append(r["pts"])
    L = [f"# Agrégat sur {len(days)} jours", "", "## Tendance par segment", "",
         "| Segment | HAUSSIÈRE | BAISSIÈRE | RANGE |", "|---|---|---|---|"]
    for name, c in trends.items():
        L.append(f"| {name} | {c.get('HAUSSIÈRE', 0)} | {c.get('BAISSIÈRE', 0)} | {c.get('RANGE', 0)} |")
    L += ["", "## Stops — total et espérance par signal", "",
          "| Signal | Stop | Trades | Total pts | Moy. pts | % gagnants | Pire |", "|---|---|---|---|---|---|---|"]
    for (sig, rule), xs in sorted(agg.items(), key=lambda kv: (kv[0][0], -sum(kv[1]))):
        w = sum(1 for x in xs if x > 0)
        L.append(f"| {sig} | {rule} | {len(xs)} | {sum(xs):+.2f} | {sum(xs) / len(xs):+.2f} | "
                 f"{100 * w / len(xs):.0f}% | {min(xs):+.2f} |")
    L += ["", f"_Échantillon de {len(days)} jours : indicatif, pas statistiquement significatif._"]
    return "\n".join(L)


# ------------------------------------------------------------------------------------------- input
# The recorder files a session under the UTC date of its START (<root>/<YYYY-MM-DD>/<session_id>/):
# a session running for days stays in its first date folder. Days are therefore NOT selected by
# folder name: every recent session is replayed and its bars are split by New York trading day.
def session_dirs(path: Path) -> list[Path]:
    if any(path.glob("part-*.hrec")):
        return [path]
    found = sorted(p for p in path.iterdir() if p.is_dir() and any(p.glob("part-*.hrec")))
    if found:
        return found
    return sorted(p for p in path.glob("*/*") if p.is_dir() and any(p.glob("part-*.hrec")))


def recent_sessions(root: Path, since_epoch: float) -> list[Path]:
    """Every session that wrote a part file after ``since_epoch`` (whatever its date folder)."""
    out = []
    for d in session_dirs(root):
        parts = list(d.glob("part-*.hrec"))
        if parts and max(x.stat().st_mtime for x in parts) >= since_epoch:
            out.append(d)
    return out


def load_bars(dirs: list[Path], cfg) -> tuple[list[B], dict]:
    from hermes.storage.reader import iter_raw_events
    lists, ctx = [], {}
    for d in dirs:
        try:
            bl, c = replay_bars(iter_raw_events(d), cfg)
        except Exception as e:  # one damaged session must not kill the daily report
            print(f"{d}: replay impossible ({e!r}), session ignorée", file=sys.stderr)
            continue
        lists.append(bl)
        if bl and (not ctx.get("_last") or bl[-1].t > ctx["_last"]):
            ctx = {**c, "_last": bl[-1].t}
    return merge_bars(lists), ctx


def trading_days(bars: list[B]) -> list[date]:
    return sorted({b.t.date() for b in bars if in_win(b, dtime(9, 30), dtime(16, 0))})


def slice_day(bars: list[B], day: date) -> list[B]:
    """Globex day for ``day``: previous evening 18:00 NY -> 16:00 NY."""
    a = datetime.combine(day - timedelta(days=1), dtime(18, 0), NY)
    z = datetime.combine(day, dtime(16, 0), NY)
    return [b for b in bars if a <= b.t < z]


def prev_rth(bars: list[B], day: date) -> dict | None:
    before = [d for d in trading_days(bars) if d < day]
    if not before:
        return None
    d = before[-1]
    rth = [b for b in bars if b.t.date() == d and in_win(b, dtime(9, 30), dtime(16, 0))]
    return {"date": d.isoformat(), "high": max(b.h for b in rth), "low": min(b.l for b in rth),
            "close": rth[-1].c, "complete": len(rth) >= 380}


def day_context(bars: list[B], day: date, ctx: dict) -> dict:
    out = {"symbol": ctx.get("symbol", "MNQ")}
    p = prev_rth(bars, day)
    if p is None and ctx.get("prev") and ctx.get("_last") and ctx["_last"].date() == day:
        p = ctx["prev"]  # engine's previous session (only valid when the replay ended on this day)
    if p is not None:
        out["prev"] = p
    return out


def synthetic_day(seed: int = 7) -> tuple[list[B], dict]:
    import random
    rng = random.Random(seed)
    out, px = [], 21000.0
    base = datetime(2026, 11, 2, 9, 30, tzinfo=NY)
    for m in range(390):
        drift = 0.35 if m < 90 else (-0.05 if m < 270 else -0.3)
        o = px
        c = round((o + drift + rng.gauss(0, 3)) * 4) / 4
        h = max(o, c) + round(abs(rng.gauss(0, 1.5)) * 4) / 4
        lo = min(o, c) - round(abs(rng.gauss(0, 1.5)) * 4) / 4
        v = rng.randint(200, 900)
        buy = int(v * (0.5 + (0.1 if c > o else -0.1)))
        out.append(B(base + timedelta(minutes=m), o, h, lo, c, v, buy, v - buy, v * (o + h + lo + c) / 4))
        px = c
    return out, {"symbol": "MNQ (SYNTHÉTIQUE)", "prev": {"date": "20261030", "high": 21040.0, "low": 20950.0,
                                                          "close": 20990.0, "complete": True}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=str(DEFAULT_ROOT),
                    help="racine des enregistrements, un dossier de date ou un dossier de session")
    ap.add_argument("--date", help="jour de trading YYYY-MM-DD (heure de New York). Défaut : dernier jour disponible")
    ap.add_argument("--days", type=int, default=1, help="analyser les N derniers jours de trading + agrégat")
    ap.add_argument("--lookback", type=int, default=0,
                    help="ne rejouer que les sessions modifiées dans les N derniers jours (0 = auto)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--slip", type=float, default=0.25, help="glissement par sortie, en points")
    ap.add_argument("--out", help="écrire le rapport Markdown ici")
    ap.add_argument("--json", help="écrire les résultats JSON ici")
    ap.add_argument("--synthetic", action="store_true")
    args = ap.parse_args(argv)

    results: list[tuple[dict, list[dict]]] = []
    if args.synthetic:
        bars, ctx = synthetic_day()
        results.append((analyse_day(bars, ctx), stop_tests(bars, args.slip)))
    else:
        cfg = load_config(args.config)
        root = Path(args.path).expanduser()
        if not root.is_dir():
            print(f"Dossier introuvable : {root}", file=sys.stderr)
            return 2
        want = date.fromisoformat(args.date) if args.date else None
        lookback = args.lookback or (args.days + 4)
        if want is not None:
            since = datetime.combine(want - timedelta(days=1), dtime(0, 0), NY).timestamp()
            if not args.lookback:
                lookback = 0
        else:
            since = (datetime.now(NY) - timedelta(days=lookback)).timestamp()
        dirs = recent_sessions(root, since) if (want is not None or lookback) else session_dirs(root)
        if not dirs:
            print(f"Aucune session enregistrée récente sous {root}", file=sys.stderr)
            return 2
        print(f"Sessions rejouées : {len(dirs)}", file=sys.stderr)
        bars, ctx = load_bars(dirs, cfg)
        days = trading_days(bars)
        if want is not None:
            days = [d for d in days if d == want]
            if not days:
                print(f"Aucune barre RTH le {want} (jours disponibles : "
                      f"{', '.join(d.isoformat() for d in trading_days(bars)) or 'aucun'})", file=sys.stderr)
                return 3
        else:
            days = days[-args.days:]
        if not days:
            print("Aucune séance RTH dans les enregistrements rejoués", file=sys.stderr)
            return 3
        for d in days:
            db = slice_day(bars, d)
            results.append((analyse_day(db, day_context(bars, d, ctx)), stop_tests(db, args.slip)))
    text = "\n\n---\n\n".join(render(d, t) for d, t in results)
    if len(results) > 1:
        text += "\n\n---\n\n" + render_aggregate(results)
    print(text)
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).expanduser().write_text(
            json.dumps([{"day": d, "stop_tests": t} for d, t in results], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
