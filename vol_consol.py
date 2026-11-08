#!/usr/bin/env python3
"""
Стратегия из видео «Пробой уровня в диапазоне»: всплеск объёма → консолидация → часовой уровень (≥3 закрытия) →
отход и возврат → пробой. Правила зафиксированы до проверки (часовки всего рынка Binance USDT-M).

  всплеск   : часовой оборот ≥ SURGE × среднего за 24 ч до него и ≥ MIN_QV; свеча вверх → лонг, вниз → шорт
  уровень   : в течение LEVEL_WIN ч после всплеска ≥ 3 закрытия в полосе ±BAND от одной цены, ни одного закрытия
              за полосой (выше для лонга), и с момента всплеска был отход ≥ 1 ATR от уровня в другую сторону
  вход      : цена прошла уровень на BREAK (стоп-ордер) в течение ENTRY_WIN ч после формирования уровня
  стоп      : 1,5 ATR(24, часовой) от входа; цель: максимум (для шорта минимум) с момента всплеска;
              если цель ближе одного стопа — пропуск; максимум HOLD ч; стоп и цель в одном часе → стоп
  издержки  : 0,2% на круг

  python vol_consol.py            # только период разработки
  python vol_consol.py --holdout  # контрольный период (один раз!)
"""
import sys

import numpy as np
import pandas as pd

END = pd.Timestamp("2026-11-05", tz="UTC")
HOLDOUT = END - pd.Timedelta(days=60)
SURGE, MIN_QV, BAND, BREAK = 5.0, 2e6, 0.003, 0.002
LEVEL_WIN, ENTRY_WIN, HOLD, ATR_N, STOP_ATR = 48, 48, 48, 24, 1.5
COST = 0.002


def symbol_trades(x):
    o, h, l, c, qv = (x[k].to_numpy(float) for k in ("o", "h", "l", "c", "qv"))
    t = x.t.to_numpy()
    n = len(c)
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]), abs(l - np.r_[c[0], c[:-1]])))
    atr = pd.Series(tr).rolling(ATR_N).mean().to_numpy()
    avg = pd.Series(qv).shift(1).rolling(24).mean().to_numpy()
    out, busy = [], -1
    for s in range(ATR_N + 1, n - 3):
        if s <= busy or not (qv[s] >= SURGE * avg[s] and qv[s] >= MIN_QV) or not np.isfinite(atr[s]):
            continue
        d = 1 if c[s] > o[s] else -1
        # уровень
        lvl, t_lvl = None, None
        for k in range(s + 3, min(s + 1 + LEVEL_WIN, n)):
            win = c[s + 1:k + 1]
            for L in win[::-1]:                         # кандидаты — недавние закрытия
                band = np.abs(win / L - 1) <= BAND
                if band.sum() < 3:
                    continue
                Lm = win[band].mean()
                if (d * (win - Lm * (1 + d * BAND)) > 0).any():
                    continue                            # было закрытие за полосой — это не сопротивление
                extreme = (l if d == 1 else h)[s + 1:k + 1]
                if not (d * (Lm - extreme) >= atr[k]).any():
                    continue                            # не было отхода ≥ 1 ATR
                lvl, t_lvl = Lm, k
                break
            if lvl is not None:
                break
        if lvl is None:
            continue
        trig = lvl * (1 + d * BREAK)
        target = (h if d == 1 else l)[s:t_lvl + 1].max() if d == 1 else l[s:t_lvl + 1].min()
        e = next((j for j in range(t_lvl + 1, min(t_lvl + 1 + ENTRY_WIN, n))
                  if (h[j] if d == 1 else l[j]) * d >= trig * d), None)
        if e is None:
            busy = t_lvl
            continue
        entry = max(o[e], trig) if d == 1 else min(o[e], trig)
        stop = entry - d * STOP_ATR * atr[t_lvl]
        risk = d * (entry - stop) / entry
        if d * (target - entry) < d * (entry - stop) or risk <= 0:
            busy = e
            continue
        px, why = None, "время"
        jj = e
        for jj in range(e, min(e + HOLD, n)):
            if d * ((l[jj] if d == 1 else h[jj]) - stop) <= 0:
                px, why = stop, "стоп"; break
            if d * ((h[jj] if d == 1 else l[jj]) - target) >= 0:
                px, why = target, "цель"; break
        if px is None:
            px = c[jj]
        ret = d * (px / entry - 1) - COST
        out.append({"t": t[e], "side": "long" if d == 1 else "short", "ret": ret, "R": ret / risk,
                    "risk_pct": 100 * risk, "why": why, "hours": jj - e + 1})
        busy = jj
    return out


def main():
    holdout = "--holdout" in sys.argv
    h = pd.read_csv("data_cache/hourly_all_20260508_20261105.csv.gz", parse_dates=["t"]).sort_values(["symbol", "t"])
    h = h[~h.symbol.isin(["BTCUSDT", "ETHUSDT"])]
    rows = []
    for sym, x in h.groupby("symbol"):
        for r in symbol_trades(x.reset_index(drop=True)):
            r["sym"] = sym
            rows.append(r)
    t = pd.DataFrame(rows)
    t["t"] = pd.to_datetime(t.t, utc=True)
    t = t[(t.t >= HOLDOUT) & (t.t < END)] if holdout else t[t.t < HOLDOUT]
    print(f"=== {'КОНТРОЛЬ' if holdout else 'РАЗРАБОТКА'} ===")
    for side, g in [("все", t)] + list(t.groupby("side")):
        v = g.ret
        tst = v.mean() / v.std() * np.sqrt(len(v)) if len(v) > 2 else np.nan
        bm = g.groupby(g.t.dt.strftime("%m")).ret.mean()
        print(f"{side:5s}: сделок {len(g):4d}, ср {100*v.mean():+.2f}%, медиана {100*v.median():+.2f}%, "
              f"в плюсе {100*(v>0).mean():.0f}%, ср R {g.R.mean():+.2f}, t={tst:.2f}, "
              f"ср стоп {g.risk_pct.mean():.1f}%, мес+ {(bm>0).sum()}/{len(bm)} "
              f"{dict((k, round(100*x, 2)) for k, x in bm.items())}")
    print("выходы:", t.groupby("why").ret.agg(["count", "mean"]).round(4).to_dict("index"))
    t.to_csv(f"results/vol_consol_{'holdout' if holdout else 'dev'}.csv", index=False)


if __name__ == "__main__":
    main()
