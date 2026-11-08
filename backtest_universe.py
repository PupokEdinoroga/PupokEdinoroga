#!/usr/bin/env python3
"""
Бэктест пробоя на 5-минутках с ежедневным отбором монет «в игре» по всему рынку.

Отбор (universe.py): на день D — топ-20 по числу сделок ИЛИ топ-20 по росту за день D-1,
среди всех USDT-M фьючерсов Binance, включая делистнутые (нет смещения выжившего).
Сделки открываются только в дни, когда монета в списке. Данные — архив data.binance.vision.

  python backtest_universe.py                 # 180 дней, сетка вход × стоп × выход
  python backtest_universe.py --days 90 --top-trades 20 --top-growth 20
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

import breakout_backtest as bb
import universe as u

CACHE5 = Path("data_cache/5m")


def load_5m_month(sym, month: pd.Period):
    path = CACHE5 / sym / f"{month}.csv.gz"
    if path.exists():
        return pd.read_csv(path)
    df = u.fetch_zip_csv(f"{u.VISION}/monthly/klines/{sym}/5m/{sym}-5m-{month}.zip")
    if df is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df[["t", "o", "h", "l", "c", "v", "qv"]]
    df.to_csv(path, index=False)
    return df


def load_5m_day(sym, day: pd.Timestamp):
    path = CACHE5 / sym / f"{day:%Y-%m-%d}.csv.gz"
    if path.exists():
        return pd.read_csv(path)
    df = u.fetch_zip_csv(f"{u.VISION}/daily/klines/{sym}/5m/{sym}-5m-{day:%Y-%m-%d}.zip")
    if df is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df[["t", "o", "h", "l", "c", "v", "qv"]]
    df.to_csv(path, index=False)
    return df


def load_symbol(sym, days: set, end):
    """5m свечи за нужные дни: месячный файл, если в месяце нужно ≥ 8 дней (и месяц закрыт),
    иначе дневные файлы."""
    by_month = {}
    for d in days:
        by_month.setdefault(d.to_period("M"), []).append(d)
    parts = []
    for m, ds in by_month.items():
        m_end = (m + 1).to_timestamp().tz_localize("UTC")
        got = load_5m_month(sym, m) if (len(ds) >= 8 and m_end <= end) else None
        if got is not None:
            parts.append(got)
            continue
        for d in ds:
            g = load_5m_day(sym, d)
            if g is not None:
                parts.append(g)
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    df["t"] = pd.to_numeric(df["t"])
    df.loc[df["t"] > 1e14, "t"] //= 1000
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index().astype(float)
    keep = df.index.normalize().isin(pd.DatetimeIndex(sorted(days)))
    return df[keep]


def segments(df, bar=pd.Timedelta(minutes=5)):
    """Непрерывные куски без пропусков больше одного бара."""
    gap = df.index.to_series().diff() > bar
    seg_id = gap.cumsum().to_numpy()
    for k in np.unique(seg_id):
        yield df[seg_id == k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--end", default=None, help="конец периода (UTC, не включительно), по умолчанию сегодня")
    ap.add_argument("--top-trades", type=int, default=20)
    ap.add_argument("--top-growth", type=int, default=20)
    ap.add_argument("--entries", default="market,retest,stop")
    ap.add_argument("--stop-bufs", default="0.003,0.006,0.01")
    ap.add_argument("--fade-tfs", default="5,15")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--holdout-days", type=int, default=60,
                    help="контрольный период в конце: не показывается, пока не задан --reveal")
    ap.add_argument("--reveal", default=None,
                    help="открыть контрольный период для ОДНОЙ конфигурации: вход,стоп,выход_ТФ,вариант "
                         "например retest,0.006,5,A. Делать один раз, после выбора на разработке")
    ap.add_argument("--out", default="results/universe_5m")
    args = ap.parse_args()

    end = (pd.Timestamp(args.end, tz="UTC") if args.end
           else pd.Timestamp.now(tz="UTC").normalize())
    start = end - pd.Timedelta(days=args.days)
    daily = u.load_daily_all(start - pd.Timedelta(days=20), end)
    uni = u.daily_universe(daily, args.top_trades, args.top_growth)
    atr = u.daily_atr_pct(daily)

    trade_days = pd.date_range(start, end - pd.Timedelta(days=1), freq="D")
    active = {}                                  # символ -> дни, когда он в игре
    for d in trade_days:
        for s in uni.get(d, ()):
            active.setdefault(s, set()).add(d)
    need = {s: {x + pd.Timedelta(days=k) for x in ds for k in (-1, 0, 1)
                if x + pd.Timedelta(days=k) < end} for s, ds in active.items()}
    n_sd = sum(len(v) for v in active.values())
    print(f"Период {start:%Y-%m-%d} - {end:%Y-%m-%d}: {len(active)} монет, "
          f"{n_sd} монето-дней в игре (в среднем {n_sd / len(trade_days):.0f} монет в день)")

    print("Качаю 5m свечи...")
    with ThreadPoolExecutor(args.workers) as ex:
        loaded = dict(zip(need, ex.map(lambda s: load_symbol(s, need[s], end), need)))

    base = bb.Params(bar_min=5, vol_window=6, retest_window=6, max_hold_min=288,
                     max_stop=0.03, retest_buf=0.0)
    prepared = []
    for s, df in loaded.items():
        if df is None or df.empty:
            continue
        act = pd.DatetimeIndex(sorted(active[s]))
        for seg in segments(df):
            if len(seg) < 300:                   # меньше суток — не хватит на уровни
                continue
            x = bb.prepare(seg, base)
            x["in_play"] = x.index.normalize().isin(act)
            a = atr.get(s)
            x["atr_pct"] = (a.reindex(x.index.normalize()).to_numpy() if a is not None else np.nan)
            prepared.append((s, x))
    print(f"Подготовлено кусков: {len(prepared)}")

    holdout = end - pd.Timedelta(days=args.holdout_days)
    if args.reveal:
        m, sb, ft, v = args.reveal.split(",")
        p = replace(base, entry_mode=m, stop_buf=float(sb), fade_tf=int(ft))
        t = pd.DataFrame([tr for s, x in prepared for tr in bb.simulate(x, s, v, True, p)])
        dev, hold = t[t.entry_time < holdout], t[t.entry_time >= holdout]
        print(f"\n=== ОТКРЫТИЕ КОНТРОЛЬНОГО ПЕРИОДА: {args.reveal} ===")
        for name, g in (("разработка", dev), ("контроль", hold)):
            print(name, bb.stats(g, base), "ср_pnl_%", round(g.pnl_pct.mean(), 3))
        out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
        t.to_csv(out / f"reveal_{args.reveal.replace(',', '_')}.csv", index=False)
        return

    trades = []
    fade_tfs = [int(v) for v in args.fade_tfs.split(",")]
    for mode in args.entries.split(","):
        for sb in (float(v) for v in args.stop_bufs.split(",")):
            for ft in fade_tfs:
                p = replace(base, entry_mode=mode, stop_buf=sb, fade_tf=ft)
                for s, x in prepared:
                    for v in ("A", "B", "C"):
                        if v == "A" and ft != fade_tfs[0]:
                            continue
                        trades += bb.simulate(x, s, v, True, p)
    t = pd.DataFrame(trades)
    t = t[t.entry_time < holdout]                # контрольный период не смотрим и не сохраняем
    print(f"Разработка: {start:%Y-%m-%d} - {holdout:%Y-%m-%d}; "
          f"контроль {holdout:%Y-%m-%d} - {end:%Y-%m-%d} скрыт")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    t.to_csv(out / "trades.csv", index=False)
    if t.empty:
        print("Сделок нет"); return

    t["month"] = t.entry_time.dt.strftime("%Y-%m")
    months = sorted(t.month.unique())
    rows = []
    for (mode, sb, ft, v), g in t.groupby(["entry_mode", "stop_buf", "fade_tf", "variant"]):
        row = {"вход": mode, "стоп_%": sb * 100, "выход_ТФ": "-" if v == "A" else ft, "вариант": v,
               "ср_стоп_%": round(g.risk_pct.mean(), 2)}
        st = bb.stats(g, base)
        row.update({k: st.get(k) for k in ("сделок", "винрейт_%", "средний_R", "профит_фактор",
                                           "макс_просадка_R")})
        row["ср_pnl_%"] = round(g.pnl_pct.mean(), 3)
        by_m = g.groupby("month").R.mean()
        for mo in months:
            row["R_" + mo[2:]] = round(by_m.get(mo, np.nan), 2)
        row["месяцев_в_плюсе"] = f"{int((by_m > 0).sum())}/{len(by_m)}"
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary.to_csv(out / "summary.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(summary.to_string(index=False))
    print(f"\nФайлы: {out/'summary.csv'}, {out/'trades.csv'} (только период разработки)")


if __name__ == "__main__":
    main()
