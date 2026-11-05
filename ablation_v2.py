#!/usr/bin/env python3
"""Абляция v2.1: каждый фильтр по отдельности поверх v2 (только период разработки, стоп 0,6%).
  python ablation_v2.py <имя>   где имя: move20 | move10 | surge3 | surge2 | touches3 | calm
"""
import sys
from dataclasses import replace
import numpy as np
import pandas as pd
import strategy_v2 as v
import universe as u
from backtest_universe import load_symbol, segments

name = sys.argv[1]
end = pd.Timestamp("2026-11-05", tz="UTC"); start = end - pd.Timedelta(days=180)
holdout = end - pd.Timedelta(days=60)
hourly = u.load_hourly_all(end - pd.Timedelta(days=181), end)
uni_kw = {"move20": dict(min_move=0.2), "move10": dict(min_move=0.1),
          "surge3": dict(vol_surge=3.0), "surge2": dict(vol_surge=2.0)}.get(name, {})
uni = u.hourly_universe(hourly, **uni_kw)
uni = uni[(uni.t >= start) & (uni.t < holdout)]
p = replace(v.P2(), stop_buf=0.006)
if name == "touches3":
    p = replace(p, min_touches=3)
if name == "calm":
    p = replace(p, calm_bars=3, calm_mult=2.0)
hours_by = {(s, sd): pd.DatetimeIndex(g.t.sort_values()) for (s, sd), g in uni.groupby(["symbol", "side"])}
empty = pd.DatetimeIndex([], tz="UTC")
trades = []
for s, g in uni.groupby("symbol"):
    ds = {d + pd.Timedelta(days=k) for d in set(g.t.dt.normalize()) for k in (-1, 0, 1)}
    df = load_symbol(s, {d for d in ds if d < end}, end)
    if df is None or df.empty:
        continue
    for seg in segments(df):
        if len(seg) >= 200:
            trades += v.run_symbol(s, seg, hours_by.get((s, "long"), empty), hours_by.get((s, "short"), empty), p)
t = pd.DataFrame(trades).drop_duplicates(["symbol", "side", "entry_time"])
t = t[t.entry_time < holdout]
t.to_csv(f"results/v2/ablation_{name}.csv", index=False)
slip = 0.0005
lg = t.side == "long"
e_raw = np.where(lg, t.entry / (1 + slip), t.entry / (1 - slip))
x_raw = np.where(t.reason == "tp", t.exit, np.where(lg, t.exit / (1 - slip), t.exit / (1 + slip)))
t["gross"] = np.where(lg, x_raw / e_raw - 1, 1 - x_raw / e_raw) * 100
t["month"] = t.entry_time.dt.strftime("%m")
for side, g in [("все", t)] + list(t.groupby("side")):
    bm = g.groupby("month").R.mean()
    print(f"{name:9s} {side:5s} сделок {len(g):5d}  R {g.R.mean():+.3f}  pnl {g.pnl_pct.mean():+.3f}%  "
          f"до издержек {g.gross.mean():+.3f}%  мес+ {(bm > 0).sum()}/{len(bm)}", flush=True)
