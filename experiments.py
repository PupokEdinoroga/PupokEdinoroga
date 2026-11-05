#!/usr/bin/env python3
"""
Эксперимент: широкий стоп и вход на ретесте уровня.

Берёт уже скачанные минутки из data_cache/ (сначала запусти breakout_backtest.py)
и прогоняет сетку: вход {рынок, ретест} × отступ стопа × варианты выхода A/B/C,
только с фильтром «монета в игре». Итог по всему периоду, обучению и проверке (OOS).

  python experiments.py
  python experiments.py --stop-bufs 0.003,0.006,0.01 --retest-window 30
"""
import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

import breakout_backtest as bb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data_cache")
    ap.add_argument("--stop-bufs", default="0.003,0.006,0.01")
    ap.add_argument("--max-stop", type=float, default=0.03)
    ap.add_argument("--retest-buf", type=float, default=0.001)
    ap.add_argument("--retest-window", type=int, default=30)
    ap.add_argument("--no-filter", action="store_true", help="также без фильтра «в игре»")
    ap.add_argument("--out", default="results/experiments")
    args = ap.parse_args()

    files = sorted(Path(args.cache).glob("*_1m_*.csv.gz"))
    if not files:
        raise SystemExit("В data_cache пусто: сначала запусти breakout_backtest.py")
    base = bb.Params(max_stop=args.max_stop, retest_buf=args.retest_buf,
                     retest_window=args.retest_window)
    prepared = {}
    for f in files:
        sym = f.name.split("_")[0]
        raw = pd.read_csv(f, index_col=0, parse_dates=True)
        if len(raw) >= 20 * 1440:
            prepared[sym] = bb.prepare(raw, base)
    print("Монеты:", ", ".join(prepared))

    filters = (True, False) if args.no_filter else (True,)
    stop_bufs = [float(x) for x in args.stop_bufs.split(",")]
    trades = []
    for mode in ("market", "retest"):
        for sb in stop_bufs:
            p = replace(base, entry_mode=mode, stop_buf=sb)
            for sym, df in prepared.items():
                for v in ("A", "B", "C"):
                    for f in filters:
                        trades += bb.simulate(df, sym, v, f, p)
    t = pd.DataFrame(trades)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    t.to_csv(out / "trades.csv", index=False)

    start = min(d.index[0] for d in prepared.values())
    end = max(d.index[-1] for d in prepared.values())
    cutoff = start + (end - start) * (1 - base.oos_frac)
    rows = []
    for (mode, sb, v, f), g in t.groupby(["entry_mode", "stop_buf", "variant", "in_play_filter"]):
        row = {"вход": mode, "стоп_%": sb * 100, "вариант": v, "фильтр": "да" if f else "нет",
               "ср_стоп_%": round(g.risk_pct.mean(), 2)}
        for part, gg in (("", g), ("обуч_", g[g.entry_time < cutoff]),
                         ("OOS_", g[g.entry_time >= cutoff])):
            st = bb.stats(gg, base)
            keys = ("сделок", "винрейт_%", "средний_R", "сумма_R", "профит_фактор") if not part \
                else ("сделок", "средний_R", "профит_фактор")
            row.update({part + k: st.get(k) for k in keys})
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary.to_csv(out / "summary.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(summary.to_string(index=False))
    print(f"\nПериод: {start:%Y-%m-%d} - {end:%Y-%m-%d}, граница OOS: {cutoff:%Y-%m-%d}")
    print(f"Файлы: {out/'summary.csv'}, {out/'trades.csv'}")


if __name__ == "__main__":
    main()
