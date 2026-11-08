#!/usr/bin/env python3
"""
Проверка скальпинга по плотностям на данных регистратора (ob_data/ГГГГ-ММ-ДД/...).

Стратегии (правила зафиксированы заранее):
  wall_bounce — после появления аномальной заявки: лимитка на bounce_offset ПЕРЕД ней (для bid — выше, лонг;
                для ask — ниже, шорт), ждём исполнения fill_window_s; тейк tp_bounce, выход по рынку, если цена
                прошла сквозь плотность на eaten_buf (её съели/сняли), максимум hold_bounce_s.
  replacer    — крупная заявка переставилась ближе к цене: вход по рынку в её сторону, тейк tp_replacer,
                выход, если цена ушла за заявку, максимум hold_replacer_s.
Издержки: вход лимиткой — мейкер 0,02%; вход/выход по рынку — 0,05% + проскальзывание 0,05%; тейк — лимит 0,02%.

  python analyze_ob.py [папка_данных]
"""
import csv
import gzip
import math
import sys
from bisect import bisect_left
from pathlib import Path

P = dict(bounce_offset=0.0005, fill_window_s=300, tp_bounce=0.005, eaten_buf=0.001, hold_bounce_s=300,
         tp_replacer=0.01, hold_replacer_s=120, dedup_s=600,
         maker=0.0002, taker=0.0005, slip=0.0005)


def load_trades(path):
    t, p = [], []
    with gzip.open(path, "rt") as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                t.append(int(row[0])); p.append(float(row[1]))
    order = sorted(range(len(t)), key=t.__getitem__)
    return [t[i] for i in order], [p[i] for i in order]


def load_events(day_dir):
    path = Path(day_dir) / "anomalies.csv.gz"
    if not path.exists():
        return []
    with gzip.open(path, "rt") as f:
        return [{"ts": int(r[0]), "sym": r[1], "kind": r[2], "side": r[3], "price": float(r[4]),
                 "notional": float(r[5]), "ratio": float(r[6]), "dist": float(r[7])} for r in csv.reader(f) if r]


def sim_bounce(ev, T, Pr):
    d = 1 if ev["side"] == "bid" else -1                  # bid-стена → лонг перед ней
    wall = ev["price"]
    entry = wall * (1 + d * P["bounce_offset"])
    i0 = bisect_left(T, ev["ts"])
    i_end = bisect_left(T, ev["ts"] + P["fill_window_s"] * 1000)
    ie = next((i for i in range(i0, min(i_end, len(T))) if d * (Pr[i] - entry) <= 0), None)
    if ie is None:
        return None                                       # не дошли до нашей лимитки
    if d * (Pr[ie] - wall * (1 - d * P["eaten_buf"])) <= 0:
        return {"pnl": d * (Pr[ie] * (1 - d * P["slip"]) / entry - 1) - P["maker"] - P["taker"], "why": "проскок"}
    tp = entry * (1 + d * P["tp_bounce"])
    k_end = bisect_left(T, T[ie] + P["hold_bounce_s"] * 1000)
    for k in range(ie + 1, min(k_end, len(T))):
        if d * (Pr[k] - wall * (1 - d * P["eaten_buf"])) <= 0:
            return {"pnl": d * (Pr[k] * (1 - d * P["slip"]) / entry - 1) - P["maker"] - P["taker"], "why": "съели"}
        if d * (Pr[k] - tp) > 0:
            return {"pnl": P["tp_bounce"] - 2 * P["maker"], "why": "тейк"}
    k = min(k_end, len(T)) - 1
    return {"pnl": d * (Pr[k] * (1 - d * P["slip"]) / entry - 1) - P["maker"] - P["taker"], "why": "время"}


def sim_replacer(ev, T, Pr):
    d = 1 if ev["side"] == "bid" else -1
    i0 = bisect_left(T, ev["ts"])
    if i0 >= len(T):
        return None
    entry = Pr[i0] * (1 + d * P["slip"])
    tp = entry * (1 + d * P["tp_replacer"])
    stop = ev["price"] * (1 - d * P["eaten_buf"])
    k_end = bisect_left(T, T[i0] + P["hold_replacer_s"] * 1000)
    for k in range(i0 + 1, min(k_end, len(T))):
        if d * (Pr[k] - stop) <= 0:
            return {"pnl": d * (Pr[k] * (1 - d * P["slip"]) / entry - 1) - 2 * P["taker"], "why": "ушла за заявку"}
        if d * (Pr[k] - tp) > 0:
            return {"pnl": d * (tp / entry - 1) - P["taker"] - P["maker"], "why": "тейк"}
    k = min(k_end, len(T)) - 1
    return {"pnl": d * (Pr[k] * (1 - d * P["slip"]) / entry - 1) - 2 * P["taker"], "why": "время"}


def run(root):
    res = {"wall_bounce": [], "replacer": []}
    for day in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        events = load_events(day)
        cache, seen = {}, {}
        for ev in sorted(events, key=lambda e: e["ts"]):
            key = (ev["sym"], ev["kind"], ev["side"], round(ev["price"], 10))
            if ev["ts"] - seen.get(key, -10**15) < P["dedup_s"] * 1000:
                continue                                  # та же заявка уже учтена
            seen[key] = ev["ts"]
            tp = day / f"{ev['sym']}_trades.csv.gz"
            if not tp.exists():
                continue
            if ev["sym"] not in cache:
                cache[ev["sym"]] = load_trades(tp)
            T, Pr = cache[ev["sym"]]
            r = sim_bounce(ev, T, Pr) if ev["kind"] == "anomaly" else sim_replacer(ev, T, Pr)
            if r:
                r.update(day=day.name, sym=ev["sym"], ratio=ev["ratio"])
                res["wall_bounce" if ev["kind"] == "anomaly" else "replacer"].append(r)
    for name, rows in res.items():
        print(f"\n=== {name} ===")
        if not rows:
            print("сделок нет"); continue
        v = [r["pnl"] for r in rows]
        n, avg = len(v), sum(v) / len(v)
        sd = math.sqrt(sum((x - avg) ** 2 for x in v) / (n - 1)) if n > 1 else 0
        print(f"сделок {n}, средний {100*avg:+.3f}%, в плюсе {100*sum(x>0 for x in v)/n:.0f}%, "
              f"t={avg/sd*math.sqrt(n) if sd else 0:.2f}")
        why = {}
        for r in rows:
            why.setdefault(r["why"], []).append(r["pnl"])
        print("выходы:", {k: f"{len(x)} шт, {100*sum(x)/len(x):+.2f}%" for k, x in why.items()})
        by_day = {}
        for r in rows:
            by_day.setdefault(r["day"], []).append(r["pnl"])
        print("по дням:", {k: f"{100*sum(x)/len(x):+.2f}% ({len(x)})" for k, x in by_day.items()})
        strong = [r["pnl"] for r in rows if r["ratio"] >= 20]
        if strong:
            print(f"только заявки ≥ 20× медианы: {len(strong)} сделок, средний {100*sum(strong)/len(strong):+.3f}%")
    return res


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "ob_data")
