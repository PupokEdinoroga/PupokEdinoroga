#!/usr/bin/env python3
"""
Новые идеи (только период разработки, правила зафиксированы до проверки).
  funding_contra — фандинг ≥ +0,1%/8ч → шорт; ≤ −0,1% → лонг; держим 8 или 24 ч; фандинг за удержание учтён.
  listings       — шорт через 24 ч после первой часовой свечи фьючерса (листинги после 10.05), держим 3 / 7 дней.
  hours          — средняя доходность по часам суток UTC (ликвидные альты, равные веса).
  leadlag        — после минуты, где BTC сдвинулся ≥ 0,4%, идут ли альты следом 1–5 минут.
Издержки 0,2% на круг (комиссия+проскальзывание по рынку).
"""
import sys
import numpy as np
import pandas as pd

END = pd.Timestamp("2026-11-05", tz="UTC")
HOLDOUT = END - pd.Timedelta(days=60)
COST = 0.002


def hourly():
    h = pd.read_csv("data_cache/hourly_all_20260508_20261105.csv.gz", parse_dates=["t"])
    return h.sort_values(["symbol", "t"])


def tstat(v):
    v = pd.Series(v).dropna()
    return v.mean() / v.std() * np.sqrt(len(v)) if len(v) > 2 and v.std() > 0 else np.nan


def funding_contra():
    h = hourly()
    f = pd.read_csv("data_cache/funding_all.csv.gz")
    f["t"] = pd.to_datetime(f.calc_time // 1000 * 1000, unit="ms", utc=True).dt.floor("1h")
    f["r8"] = f.rate * 8 / f.interval_h
    f = f[f.t < HOLDOUT]
    h["day"] = h.t.dt.normalize()
    dq = h.groupby(["symbol", "day"]).qv.sum().rename("dqv").reset_index()
    dq["liq"] = dq.groupby("symbol").dqv.transform(lambda v: v.shift(1).rolling(7, min_periods=7).mean())
    liq = dq.set_index(["symbol", "day"]).liq
    px = h.set_index(["symbol", "t"]).o
    rows = []
    for sym, g in f.groupby("symbol"):
        g = g.sort_values("t")
        for hold in (8, 24):
            busy = pd.Timestamp(0, tz="UTC")
            for r in g.itertuples():
                if r.t < busy or abs(r.r8) < 0.001 or liq.get((sym, r.t.normalize()), 0) < 5e6:
                    continue
                t1 = r.t + pd.Timedelta(hours=hold)
                p0, p1 = px.get((sym, r.t)), px.get((sym, t1))
                if not (p0 and p1):
                    continue
                d = -np.sign(r.r8)                                   # против толпы
                fund = g[(g.t > r.t) & (g.t <= t1)].rate.sum()       # шорт получает +rate, лонг платит
                ret = d * (p1 / p0 - 1) - d * fund - COST
                rows.append({"hold": hold, "side": "short" if d < 0 else "long", "t": r.t, "sym": sym, "ret": ret})
                busy = t1
    t = pd.DataFrame(rows)
    for (hold, side), g in t.groupby(["hold", "side"]):
        bm = g.groupby(g.t.dt.strftime("%m")).ret.mean()
        print(f"держим {hold:2d}ч, {side:5s}: сделок {len(g):4d}, ср {100*g.ret.mean():+.2f}%, медиана {100*g.ret.median():+.2f}%, "
              f"в плюсе {100*(g.ret>0).mean():.0f}%, t={tstat(g.ret):.2f}, худшая {100*g.ret.min():+.0f}%, "
              f"мес+ {(bm>0).sum()}/{len(bm)}")


def listings():
    h = hourly()
    first = h.groupby("symbol").t.min()
    first = first[(first >= pd.Timestamp("2026-05-10", tz="UTC")) & (first < HOLDOUT - pd.Timedelta(days=7))]
    px = h.set_index(["symbol", "t"]).c
    rows = []
    for sym, t0 in first.items():
        e = t0 + pd.Timedelta(hours=24)
        pe = px.get((sym, e))
        if not pe:
            continue
        r = {"sym": sym, "t": t0}
        for d in (3, 7):
            x = h[(h.symbol == sym) & (h.t > e) & (h.t <= e + pd.Timedelta(days=d))]
            if len(x) < d * 20:
                continue
            r[f"ret{d}"] = 1 - x.c.iloc[-1] / pe - COST
            r[f"worst{d}"] = 1 - x.h.max() / pe                      # худший момент для шорта
        rows.append(r)
    t = pd.DataFrame(rows)
    print(f"листингов: {len(t)}")
    for d in (3, 7):
        v = t[f"ret{d}"].dropna()
        bm = t.dropna(subset=[f"ret{d}"]).groupby(t.t.dt.strftime("%m"))[f"ret{d}"].mean()
        print(f"шорт на {d} дн: n={len(v)}, ср {100*v.mean():+.1f}%, медиана {100*v.median():+.1f}%, в плюсе {100*(v>0).mean():.0f}%, "
              f"t={tstat(v):.2f}, худший итог {100*v.min():+.0f}%, худший момент (медиана) {100*t[f'worst{d}'].median():+.0f}%, "
              f"мес+ {(bm>0).sum()}/{len(bm)}")


def hours():
    h = hourly()
    h = h[h.t < HOLDOUT]
    h["day"] = h.t.dt.normalize()
    dq = h.groupby(["symbol", "day"]).qv.sum()
    liq_syms = dq[dq >= 5e6].reset_index()[["symbol", "day"]]
    h = h.merge(liq_syms, on=["symbol", "day"])
    h = h[~h.symbol.isin(["BTCUSDT", "ETHUSDT"])]
    h["ret"] = h.c / h.o - 1
    cs = h.groupby(["t"]).ret.mean().rename("r").reset_index()          # равновзвешенный альт-индекс по часам
    cs["hour"] = cs.t.dt.hour
    out = cs.groupby("hour").r.agg(["mean", "count", tstat]).rename(columns={"tstat": "t"})
    out["mean_%"] = (100 * out["mean"]).round(3)
    print(out[["mean_%", "t", "count"]].round(2).to_string())


def leadlag():
    def load(sym):
        import glob
        f = sorted(glob.glob(f"data_cache/{sym}_1m_180d_*.csv.gz"))[-1]
        return pd.read_csv(f, index_col=0, parse_dates=True).c
    btc = load("BTCUSDT")
    br = btc.pct_change()
    alts = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT", "LTCUSDT", "DOTUSDT",
            "BCHUSDT", "NEARUSDT", "AAVEUSDT", "UNIUSDT", "TRXUSDT", "BNBUSDT"]
    ev = br[(br.abs() >= 0.004) & (br.index < HOLDOUT)]
    print(f"минут с |BTC| ≥ 0,4%: {len(ev)}")
    for k in (1, 3, 5):
        res = []
        for a in alts:
            c = load(a)
            ar = c.pct_change()
            same = ar.reindex(ev.index)                                     # ход альта в ту же минуту
            fwd = (c.shift(-k) / c - 1).reindex(ev.index)                   # ход альта за k минут после
            res.append(pd.DataFrame({"sign": np.sign(ev), "same": same, "fwd": fwd}))
        r = pd.concat(res).dropna()
        follow = r.sign * r.fwd
        print(f"через {k} мин: альт идёт за BTC в среднем {100*follow.mean():+.3f}% (t={tstat(follow):.1f}), "
              f"после издержек 0,2%: {100*(follow-COST).mean():+.3f}%; в ту же минуту альт уже сходил {100*(r.sign*r.same).mean():+.2f}%")


if __name__ == "__main__":
    {"funding_contra": funding_contra, "listings": listings, "hours": hours, "leadlag": leadlag}[sys.argv[1]]()
