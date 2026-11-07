#!/usr/bin/env python3
"""
Версия 2: скальп на пробое у монет «в игре» (по мотивам разбора сделок ProScalping).

Отбор, каждый час по текущему дню UTC (universe.hourly_universe):
  лонг  — топ-20 по росту от открытия дня ∪ топ-20 по числу сделок с ростом > 0
  шорт  — топ-20 по падению ∪ топ-20 по числу сделок с падением
Сетап (5m): уровень ≥ 2 касаний за 12 ч (как в v1) + консолидация: минимумы 6 свечей
  перед пробоем не дальше cons_atr × ATR(5m) от уровня.
Вход: закрытие 5m за уровнем (+0,05%) с объёмом > 2× среднего за 6 свечей; вход по открытию следующей.
Выход:
  тейк на импульсе  — лимит на +max(tp_atr × ATR, tp_min);
  нет импульса      — если за noimp_bars свечей цена не прошла 1 × ATR, выход по закрытию;
  возврат за уровень — закрытие 5m за уровнем -> выход;
  максимум max_bars свечей.
Стоп: за уровнем на stop_buf. Шорт — зеркально (считаем на перевёрнутых ценах 1/p).
Сначала стоп, потом тейк, если оба в одной свече.

  python strategy_v2.py                        # 180 дней, последние 60 скрыты
  python strategy_v2.py --reveal 0.006         # открыть контроль для одного стопа (один раз!)
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

import breakout_backtest as bb
import universe as u
from backtest_universe import load_symbol, segments


@dataclass
class P2:
    # уровни (как v1)
    pivot_k: int = 3
    level_lookback_bars: int = 144
    touch_tol: float = 0.002
    min_touches: int = 2
    max_dist_to_level: float = 0.03
    # сетап/вход
    break_buf: float = 0.0005
    vol_mult: float = 2.0
    vol_window: int = 6
    cons_bars: int = 6
    cons_atr: float = 3.0
    calm_bars: int = 0               # v2.1: без «резкого захода» — 0 = выключено
    calm_mult: float = 2.0           #   ни одна из calm_bars свечей до пробоя не шире calm_mult × ATR
    atr_bars: int = 48
    # стоп/выход
    stop_buf: float = 0.006
    min_stop: float = 0.002
    max_stop: float = 0.03
    tp_atr: float = 3.0
    tp_min: float = 0.01
    noimp_bars: int = 2
    max_bars: int = 12
    # издержки
    fee: float = 0.0005
    slippage: float = 0.0005
    risk_per_trade: float = 0.01


def _levels(df, p: P2):
    lp = bb.Params(bar_min=5, pivot_k=p.pivot_k, level_lookback_bars=p.level_lookback_bars,
                   touch_tol=p.touch_tol, min_touches=p.min_touches,
                   max_dist_to_level=p.max_dist_to_level)
    lv = bb.compute_levels(df[["o", "h", "l", "c", "v"]], lp)
    return lv.reindex(df.index, method="ffill")


def prepare_v2(df: pd.DataFrame, p: P2) -> pd.DataFrame:
    """df — 5m свечи в «лонговом» пространстве (для шорта уже перевёрнуты)."""
    x = df.copy()
    lv = _levels(x, p)
    x["level"], x["touches"] = lv["level"], lv["touches"].fillna(0).astype(int)
    tr = pd.concat([x.h - x.l, (x.h - x.c.shift()).abs(), (x.l - x.c.shift()).abs()], axis=1).max(axis=1)
    x["atr"] = tr.rolling(p.atr_bars).mean() / x.c
    vol_avg = x.v.rolling(p.vol_window).mean().shift(1)
    lvl = x.level
    cons_low = x.l.rolling(p.cons_bars).min().shift(1)
    x["signal"] = ((x.c > lvl * (1 + p.break_buf)) & (x.c.shift(1) <= lvl)
                   & (x.v > p.vol_mult * vol_avg) & lvl.notna() & x.atr.notna()
                   & (cons_low >= lvl * (1 - p.cons_atr * x.atr)))
    if p.calm_bars > 0:
        rng = ((x.h - x.l) / x.c).rolling(p.calm_bars).max().shift(1)
        x["signal"] &= rng <= p.calm_mult * x.atr
    return x


def simulate_v2(x: pd.DataFrame, in_play: np.ndarray, p: P2):
    """Лонговая логика. Возвращает сделки в ценах того же пространства."""
    o, h, l, c = (x[k].to_numpy() for k in ("o", "h", "l", "c"))
    lvl_a, atr = x.level.to_numpy(), x.atr.to_numpy()
    sig = x.signal.to_numpy() & in_play
    n, idx, out, busy = len(x), x.index, [], -1
    for s in np.flatnonzero(sig):
        if s <= busy or s + 1 >= n:
            continue
        e, level, a = s + 1, lvl_a[s], atr[s]
        entry = o[e] * (1 + p.slippage)
        stop = level * (1 - p.stop_buf)
        risk = (entry - stop) / entry
        if not (p.min_stop <= risk <= p.max_stop):
            continue
        tp = entry * (1 + max(p.tp_atr * a, p.tp_min))
        imp = entry * (1 + a)
        peak, reason, px = entry, "time", None
        j_end = min(e + p.max_bars - 1, n - 1)
        for j in range(e, j_end + 1):
            if l[j] <= stop:
                px, reason = min(o[j], stop) * (1 - p.slippage), "stop"; break
            if h[j] >= tp:
                px, reason = max(tp, o[j]), "tp"; break
            if c[j] < level:
                px, reason = c[j] * (1 - p.slippage), "fade"; break
            peak = max(peak, h[j])
            if j == e + p.noimp_bars - 1 and peak < imp:
                px, reason = c[j] * (1 - p.slippage), "no_impulse"; break
        if px is None:
            px = c[j] * (1 - p.slippage)
        out.append((s, e, j, level, entry, stop, px, reason, a))
        busy = j
    return out


def run_symbol(sym, df, inplay_long, inplay_short, p: P2):
    """inplay_*: DatetimeIndex часов (начало часа), когда монета в списке."""
    trades = []
    for side in ("long", "short"):
        hours = inplay_long if side == "long" else inplay_short
        if len(hours) == 0:
            continue
        if side == "long":
            y = df
        else:
            y = pd.DataFrame({"o": 1 / df.o, "h": 1 / df.l, "l": 1 / df.h, "c": 1 / df.c, "v": df.v},
                             index=df.index)
        x = prepare_v2(y, p)
        ip = x.index.floor("1h").isin(hours)
        for s, e, j, level, entry, stop, px, reason, a in simulate_v2(x, ip, p):
            if side == "long":
                pnl = px / entry - 1 - 2 * p.fee
                risk = (entry - stop) / entry
                lvl_real, entry_real, exit_real = level, entry, px
            else:                                     # обратно из 1/p в реальные цены
                entry_real, exit_real, stop_real = 1 / entry, 1 / px, 1 / stop
                pnl = 1 - exit_real / entry_real - 2 * p.fee
                risk = (stop_real - entry_real) / entry_real
                lvl_real = 1 / level
            trades.append({"symbol": sym, "side": side, "signal_time": x.index[s],
                           "entry_time": x.index[e], "exit_time": x.index[j],
                           "level": lvl_real, "entry": entry_real, "exit": exit_real,
                           "atr_pct": a * 100, "risk_pct": risk * 100, "pnl_pct": pnl * 100,
                           "R": pnl / risk, "reason": reason, "bars": j - e + 1})
    return trades


def report(t: pd.DataFrame, p: P2, title: str):
    print(f"\n=== {title} ===")
    if t.empty:
        print("сделок нет"); return
    t = t.copy()
    t["month"] = t.entry_time.dt.strftime("%Y-%m")
    rows = []
    for key, g in [("все", t)] + list(t.groupby("side")):
        st = bb.stats(g, bb.Params(risk_per_trade=p.risk_per_trade))
        bm = g.groupby("month").R.mean()
        rows.append({"сторона": key, **{k: st.get(k) for k in ("сделок", "винрейт_%", "средний_R",
                     "профит_фактор", "макс_просадка_R")}, "ср_pnl_%": round(g.pnl_pct.mean(), 3),
                     "ср_стоп_%": round(g.risk_pct.mean(), 2),
                     "месяцев_в_плюсе": f"{int((bm > 0).sum())}/{len(bm)}",
                     **{"R_" + m[2:]: round(v, 2) for m, v in bm.items()}})
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(pd.DataFrame(rows).to_string(index=False))
    print("причины выхода:", t.groupby("reason").R.agg(["count", "mean"]).round(2).to_dict("index"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--end", default="2026-11-05")
    ap.add_argument("--holdout-days", type=int, default=60)
    ap.add_argument("--stop-bufs", default="0.003,0.006")
    ap.add_argument("--reveal", type=float, default=None,
                    help="открыть контрольный период для одного stop_buf (делать один раз)")
    ap.add_argument("--v21", action="store_true",
                    help="фильтры v2.1: |рост дня| ≥ 20%%, всплеск оборота ≥ 3×, ≥ 3 касаний, без резкого захода")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--out", default="results/v2")
    args = ap.parse_args()

    end = pd.Timestamp(args.end, tz="UTC")
    start = end - pd.Timedelta(days=args.days)
    holdout = end - pd.Timedelta(days=args.holdout_days)
    hourly = u.load_hourly_all(end - pd.Timedelta(days=181), end)
    hourly = hourly[hourly.t >= start - pd.Timedelta(days=1)]
    uni = (u.hourly_universe(hourly, min_move=0.20, vol_surge=3.0) if args.v21
           else u.hourly_universe(hourly))
    uni = uni[(uni.t >= start) & (uni.t < end)]
    if args.reveal is None:
        uni_sim = uni[uni.t < holdout]                # контроль даже не симулируем
    else:
        uni_sim = uni
    print(f"Разработка {start:%Y-%m-%d} - {holdout:%Y-%m-%d}, контроль {holdout:%Y-%m-%d} - {end:%Y-%m-%d}"
          f"{' (ОТКРЫТ)' if args.reveal is not None else ' (скрыт)'}")
    print(f"Записей «в игре»: {len(uni_sim)}, монет: {uni_sim.symbol.nunique()}, "
          f"в среднем в час: лонг {uni_sim[uni_sim.side == 'long'].groupby('t').size().mean():.0f}, "
          f"шорт {uni_sim[uni_sim.side == 'short'].groupby('t').size().mean():.0f}")

    hours_by = {(s, side): pd.DatetimeIndex(g.t.sort_values())
                for (s, side), g in uni_sim.groupby(["symbol", "side"])}
    days_by = {}
    for s, g in uni_sim.groupby("symbol"):
        ds = set(g.t.dt.normalize())
        days_by[s] = {d + pd.Timedelta(days=k) for d in ds for k in (-1, 0, 1)
                      if d + pd.Timedelta(days=k) < end}
    print(f"Качаю 5m по {len(days_by)} монетам ({sum(len(v) for v in days_by.values())} монето-дней)...",
          flush=True)
    with ThreadPoolExecutor(args.workers) as ex:
        data = dict(zip(days_by, ex.map(lambda s: load_symbol(s, days_by[s], end), days_by)))

    stop_bufs = [args.reveal] if args.reveal is not None else [float(v) for v in args.stop_bufs.split(",")]
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    empty = pd.DatetimeIndex([], tz="UTC")
    for sb in stop_bufs:
        p = replace(P2(), stop_buf=sb)
        if args.v21:
            p = replace(p, min_touches=3, calm_bars=3, calm_mult=2.0)
        trades = []
        for s, df in data.items():
            if df is None or df.empty:
                continue
            for seg in segments(df):
                if len(seg) < 200:
                    continue
                trades += run_symbol(s, seg, hours_by.get((s, "long"), empty),
                                     hours_by.get((s, "short"), empty), p)
        t = pd.DataFrame(trades)
        if t.empty:
            print("сделок нет"); continue
        t = t.drop_duplicates(["symbol", "side", "entry_time"])
        dev = t[t.entry_time < holdout]
        tag = "v21_" if args.v21 else ""
        dev.to_csv(out / f"trades_dev_{tag}stop{sb}.csv", index=False)
        report(dev, p, f"РАЗРАБОТКА {'v2.1' if args.v21 else 'v2'}, стоп {sb * 100:.1f}%")
        if args.reveal is not None:
            hold = t[t.entry_time >= holdout]
            hold.to_csv(out / f"trades_holdout_{tag}stop{sb}.csv", index=False)
            report(hold, p, f"КОНТРОЛЬ, стоп {sb * 100:.1f}%")


if __name__ == "__main__":
    main()
