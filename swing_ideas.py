#!/usr/bin/env python3
"""
Быстрые проверки двух «медленных» идей на часовках всего рынка (только период разработки).

1) momentum — импульс за неделю, лонг лидеров / шорт аутсайдеров.
   Раз в hold дней в 00:00 UTC: ранжируем ликвидные монеты (ср. дневной оборот за 7 дн ≥ 5 млн $,
   история ≥ 14 дней) по доходности за lookback дней; лонг top_n лучших, шорт top_n худших,
   равные доли, держим hold дней. Издержки 0,2% на круг на каждую позицию. Фандинг не учтён.
2) pumpfade — шорт после пампа: первый час дня, когда рост от открытия дня ≥ thr;
   шорт по закрытию этого часа, стоп +stop, тейк −tp, максимум 24 ч; сначала стоп (худший случай).

  python swing_ideas.py momentum
  python swing_ideas.py pumpfade
"""
import sys
import numpy as np
import pandas as pd

END = pd.Timestamp("2026-11-05", tz="UTC")
HOLDOUT = END - pd.Timedelta(days=60)
COST = 0.002                      # 0,05% комиссия + 0,05% проскальзывание на сторону


def load():
    h = pd.read_csv("data_cache/hourly_all_20260508_20261105.csv.gz", parse_dates=["t"])
    return h[h.t < HOLDOUT].sort_values(["symbol", "t"])


def momentum():
    h = load()
    h["day"] = h.t.dt.normalize()
    d = h.groupby(["day", "symbol"]).agg(c=("c", "last"), qv=("qv", "sum")).reset_index()
    close = d.pivot(index="day", columns="symbol", values="c")
    qv = d.pivot(index="day", columns="symbol", values="qv")
    age = close.notna().cumsum()
    liq = qv.rolling(7, min_periods=7).mean()
    rows = []
    for lookback in (3, 7, 14):
        for hold in (1, 3, 7):
            for top_n in (10,):
                days = close.index[14::hold]
                for i, day in enumerate(days[:-1]):
                    nxt = days[i + 1]
                    # ранжируем по закрытию ВЧЕРАШНЕГО дня (день day ещё не закрыт)
                    prev = close.index[close.index.get_loc(day) - 1]
                    lb = close.index[close.index.get_loc(day) - 1 - lookback]
                    ret_lb = close.loc[prev] / close.loc[lb] - 1
                    ok = (liq.loc[prev] >= 5e6) & (age.loc[prev] >= 14) & ret_lb.notna()
                    r = ret_lb[ok]
                    if len(r) < 4 * top_n:
                        continue
                    longs, shorts = r.nlargest(top_n).index, r.nsmallest(top_n).index
                    # держим с закрытия prev до закрытия дня перед nxt; если монета пропала — последняя цена
                    end_day = close.index[close.index.get_loc(nxt) - 1]
                    seg = close.loc[prev:end_day]
                    last = seg.ffill().iloc[-1]
                    fwd = last / close.loc[prev] - 1
                    rows.append({"lookback": lookback, "hold": hold, "date": day,
                                 "long": fwd[longs].mean() - COST, "short": -fwd[shorts].mean() - COST,
                                 "market": fwd[r.index].mean()})
    t = pd.DataFrame(rows)
    t["ls"] = (t["long"] + t["short"]) / 2
    t["month"] = t.date.dt.strftime("%m")
    out = []
    for (lb, hd), g in t.groupby(["lookback", "hold"]):
        per_year = 365 / hd
        bm = g.groupby("month").ls.sum()
        out.append({"lookback_дн": lb, "держим_дн": hd, "периодов": len(g),
                    "лонг_ср_%": round(100 * g["long"].mean(), 2), "шорт_ср_%": round(100 * g["short"].mean(), 2),
                    "L/S_ср_%": round(100 * g.ls.mean(), 2),
                    "L/S_итого_%": round(100 * ((1 + g.ls).prod() - 1), 1),
                    "Шарп_год": round(g.ls.mean() / g.ls.std() * np.sqrt(per_year), 2) if g.ls.std() > 0 else np.nan,
                    "рынок_ср_%": round(100 * g.market.mean(), 2),
                    "месяцев_L/S_в_плюсе": f"{(bm > 0).sum()}/{len(bm)}"})
    pd.set_option("display.width", 250)
    print(pd.DataFrame(out).to_string(index=False))


def pumpfade():
    h = load()
    h["day"] = h.t.dt.normalize()
    h["day_open"] = h.groupby(["symbol", "day"]).o.transform("first")
    h["g"] = h.c / h.day_open - 1
    res = []
    for sym, x in h.groupby("symbol"):
        c, hi, lo, g, d = (x[k].to_numpy() for k in ("c", "h", "l", "g", "day"))
        t = x.t.to_numpy()
        for thr in (0.2, 0.3, 0.4):
            seen = set()
            for i in np.flatnonzero(g >= thr):
                if d[i] in seen or i + 1 >= len(c):
                    continue
                seen.add(d[i])
                entry = c[i]
                for stop, tp in ((0.10, 0.10), (0.15, 0.15), (0.20, 0.20), (0.10, 0.20), (0.20, 0.10)):
                    px, why = None, "time"
                    for j in range(i + 1, min(i + 25, len(c))):
                        if hi[j] >= entry * (1 + stop):
                            px, why = entry * (1 + stop), "stop"; break
                        if lo[j] <= entry * (1 - tp):
                            px, why = entry * (1 - tp), "tp"; break
                    if px is None:
                        px = c[min(i + 24, len(c) - 1)]
                    res.append({"thr": thr, "stop": stop, "tp": tp, "sym": sym, "t": t[i],
                                "ret": 1 - px / entry - COST, "why": why})
    r = pd.DataFrame(res)
    r["month"] = pd.to_datetime(r.t).dt.strftime("%m")
    out = []
    for (thr, st, tp), g in r.groupby(["thr", "stop", "tp"]):
        bm = g.groupby("month").ret.mean()
        out.append({"порог_%": int(thr * 100), "стоп_%": int(st * 100), "тейк_%": int(tp * 100),
                    "сделок": len(g), "винрейт_%": round(100 * (g.ret > 0).mean(), 1),
                    "ср_%": round(100 * g.ret.mean(), 2), "медиана_%": round(100 * g.ret.median(), 2),
                    "стопов_%": round(100 * (g.why == "stop").mean(), 1),
                    "месяцев_в_плюсе": f"{(bm > 0).sum()}/{len(bm)}",
                    **{"м" + k: round(100 * v, 1) for k, v in bm.items()}})
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(pd.DataFrame(out).to_string(index=False))


if __name__ == "__main__":
    {"momentum": momentum, "pumpfade": pumpfade}[sys.argv[1]]()
