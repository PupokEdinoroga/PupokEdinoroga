#!/usr/bin/env python3
"""Проверка гипотезы F «шорт ложного пробоя» на кэше data_cache/.
  python fail_short.py --days 180
"""
import argparse
from pathlib import Path
import pandas as pd
import breakout_backtest as bb

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=90)
ap.add_argument("--cache", default="data_cache")
ap.add_argument("--out", default="results/fail_short")
args = ap.parse_args()

p = bb.Params()
trades, span = [], []
for f in sorted(Path(args.cache).glob(f"*_1m_{args.days}d_*.csv.gz")):
    sym = f.name.split("_")[0]
    raw = pd.read_csv(f, index_col=0, parse_dates=True)
    df = bb.prepare(raw, p)
    span += [df.index[0], df.index[-1]]
    for flt in (True, False):
        trades += bb.simulate_fail_short(df, sym, flt, p)
t = pd.DataFrame(trades)
out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
t.to_csv(out / "trades.csv", index=False)
start, end = min(span), max(span)
cutoff = start + (end - start) * (1 - p.oos_frac)
rows = []
for flt, g in t.groupby("in_play_filter"):
    for part, gg in (("весь период", g), ("обучение", g[g.entry_time < cutoff]),
                     ("проверка (OOS)", g[g.entry_time >= cutoff])):
        rows.append({"фильтр": "да" if flt else "нет", "часть": part, **bb.stats(gg, p),
                     "ср_pnl_%": round(gg.pnl_pct.mean(), 3)})
pd.set_option("display.width", 250)
print(pd.DataFrame(rows).to_string(index=False))
print(t.groupby(["in_play_filter", "reason"]).R.agg(["count", "mean"]).round(2).to_string())
print(f"Период: {start:%Y-%m-%d} - {end:%Y-%m-%d}, граница OOS: {cutoff:%Y-%m-%d}")
