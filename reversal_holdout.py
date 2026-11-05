#!/usr/bin/env python3
"""
Недельный разворот: проверка на КОНТРОЛЬНОМ периоде (правила зафиксированы до открытия).
Раз в 3 дня в 00:00 UTC: среди ликвидных альтов (ср. дневной оборот за 7 дн ≥ 5 млн $, история ≥ 14 дн)
шорт 10 лидеров по доходности за 7 дней, лонг 10 аутсайдеров, равные доли, держим 3 дня.
Издержки 0,2% на круг на позицию. Фандинг не учтён.
"""
import numpy as np
import pandas as pd

END = pd.Timestamp("2026-11-05", tz="UTC")
HOLDOUT = END - pd.Timedelta(days=60)
COST, LOOKBACK, HOLD, TOP = 0.002, 7, 3, 10

h = pd.read_csv("data_cache/hourly_all_20260508_20261105.csv.gz", parse_dates=["t"])
h["day"] = h.t.dt.normalize()
d = h.groupby(["day", "symbol"]).agg(c=("c", "last"), qv=("qv", "sum")).reset_index()
close = d.pivot(index="day", columns="symbol", values="c")
qv = d.pivot(index="day", columns="symbol", values="qv")
age = close.notna().cumsum()
liq = qv.rolling(7, min_periods=7).mean()
idx = close.index


def run(first_day, last_day):
    rows = []
    days = [x for x in idx[14::HOLD] if first_day <= x < last_day]
    for day in days:
        k = idx.get_loc(day)
        if k + HOLD > len(idx):
            break
        prev, lb = idx[k - 1], idx[k - 1 - LOOKBACK]
        end_day = idx[min(k + HOLD - 1, len(idx) - 1)]
        if end_day >= last_day:
            break
        r = close.loc[prev] / close.loc[lb] - 1
        ok = (liq.loc[prev] >= 5e6) & (age.loc[prev] >= 14) & r.notna()
        r = r[ok]
        if len(r) < 4 * TOP:
            continue
        winners, losers = r.nlargest(TOP).index, r.nsmallest(TOP).index
        last = close.loc[prev:end_day].ffill().iloc[-1]
        fwd = last / close.loc[prev] - 1
        rows.append({"date": day, "short_winners": -fwd[winners].mean() - COST,
                     "long_losers": fwd[losers].mean() - COST, "market": fwd[r.index].mean(),
                     "worst_short": -fwd[winners].max()})
    t = pd.DataFrame(rows)
    t["ls"] = (t.short_winners + t.long_losers) / 2
    return t


def show(name, t):
    m = t.groupby(t.date.dt.strftime("%Y-%m")).ls.apply(lambda v: (1 + v).prod() - 1)
    print(f"\n=== {name}: {t.date.min():%Y-%m-%d} – {t.date.max():%Y-%m-%d}, периодов {len(t)} ===")
    print(f"Л/Ш в среднем за 3 дня: {100*t.ls.mean():+.2f}%  (шорт лидеров {100*t.short_winners.mean():+.2f}%, "
          f"лонг аутсайдеров {100*t.long_losers.mean():+.2f}%)")
    print(f"итого: {100*((1+t.ls).prod()-1):+.1f}%, периодов в плюсе {(t.ls>0).sum()}/{len(t)}, "
          f"Шарп (год) {t.ls.mean()/t.ls.std()*np.sqrt(365/HOLD):.2f}, худший период {100*t.ls.min():+.1f}%, "
          f"худший шорт одной монеты {100*t.worst_short.min():+.0f}%")
    print("по месяцам, %:", {k: round(100 * v, 1) for k, v in m.items()})


show("РАЗРАБОТКА", run(idx[0], HOLDOUT))
hold = run(HOLDOUT, END)
show("КОНТРОЛЬ (открыт впервые)", hold)
hold.to_csv("results/reversal_holdout.csv", index=False)
