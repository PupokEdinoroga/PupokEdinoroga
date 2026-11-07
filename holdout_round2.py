#!/usr/bin/env python3
"""Контрольный период (с 2026-09-06), открыт один раз. Правила зафиксированы до открытия.
1) Шорт листингов: шорт через 24 ч после первой часовой свечи перпа, держим 3 дня, издержки 0,2%.
2) Арбитраж фандинга: вход при фандинге ≥ 0,05%/8ч, выход < 0,01%; издержки «рынок» и «лимит»;
   отдельно — без сделок, где нога сдвинулась > 30% меньше чем за сутки (заранее объявленный фильтр выбросов)."""
import numpy as np
import pandas as pd
import funding as F
from new_ideas import hourly, tstat, COST, HOLDOUT, END

print("=== 1. ШОРТ ЛИСТИНГОВ — КОНТРОЛЬ ===")
h = hourly()
first = h.groupby("symbol").t.min()
last_t = h.t.max()
first = first[(first >= HOLDOUT) & (first + pd.Timedelta(days=4) <= last_t)]
px = h.set_index(["symbol", "t"]).c
rows = []
for sym, t0 in first.items():
    e = t0 + pd.Timedelta(hours=24)
    pe = px.get((sym, e))
    x = h[(h.symbol == sym) & (h.t > e) & (h.t <= e + pd.Timedelta(days=3))]
    if not pe or len(x) < 60:
        continue
    rows.append({"sym": sym, "t": t0, "ret": 1 - x.c.iloc[-1] / pe - COST, "worst": 1 - x.h.max() / pe})
t = pd.DataFrame(rows)
bm = t.groupby(t.t.dt.strftime("%Y-%m")).ret.agg(["mean", "count"])
print(f"листингов {len(t)}: ср {100*t.ret.mean():+.2f}%, медиана {100*t.ret.median():+.2f}%, в плюсе {100*(t.ret>0).mean():.0f}%, "
      f"t={tstat(t.ret):.2f}, худший итог {100*t.ret.min():+.0f}%, лучший {100*t.ret.max():+.0f}%")
print("по месяцам:", {k: f"{100*r['mean']:+.1f}% (n={int(r['count'])})" for k, r in bm.iterrows()})
t.to_csv("results/listings_holdout.csv", index=False)

print("\n=== 2. АРБИТРАЖ ФАНДИНГА (вход ≥ 0,05%) — КОНТРОЛЬ ===")
a = F.simulate(0.0005, t_from=HOLDOUT)
F.report(a, "КОНТРОЛЬ, все сделки")
clean = a[~((a.perp_leg.abs() > 0.3) & (a.days < 1))]
F.report(clean, "КОНТРОЛЬ, без выбросов (нога > 30% быстрее суток)")
if not a.empty:
    for c in ("лимит", "рынок"):
        v = a.funding + a.basis - F.COSTS[c]
        print(f"t-стат ({c}): {tstat(v):.2f}")
    a.to_csv("results/funding_holdout.csv", index=False)
