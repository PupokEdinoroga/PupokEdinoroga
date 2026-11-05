#!/usr/bin/env python3
"""
Арбитраж фандинга: лонг спот + шорт перпетуал (Binance), нейтрально к цене.

Правила (зафиксированы до проверки):
  монеты — есть USDT-перп и спот Binance, средний дневной оборот перпа за 7 дн ≥ 5 млн $;
  вход — после начисления фандинга со ставкой ≥ entry (в пересчёте на 8 ч); это начисление НЕ получаем;
  держим, пока очередная ставка ≥ exit (на 8 ч); как только ниже — выходим после этой выплаты;
  результат = Σ фандинга (шорт получает +rate) + (1 − perp_exit/perp_entry) + (spot_exit/spot_entry − 1) − издержки.
  Цены входа/выхода — открытие часовой свечи в момент начисления (спот и перп).
  Капитал на позицию = 1,5 × объём (спот 1× + маржа перпа при плече 2).

  python funding.py download     # фандинг всех символов + список спота + спотовые часовки
  python funding.py dev          # период разработки
  python funding.py holdout E    # контроль для порога входа E (например 0.0005) — один раз
"""
import io
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

import universe as u

END = pd.Timestamp("2026-11-05", tz="UTC")
START = pd.Timestamp("2026-05-08", tz="UTC")
HOLDOUT = END - pd.Timedelta(days=60)
MONTHS = pd.period_range("2026-05", "2026-10", freq="M")
CACHE = Path("data_cache")
COSTS = {"рынок": 2 * (0.0005 + 0.0005 + 0.001 + 0.0005),   # перп комиссия+проск. + спот комиссия+проск., ×2
         "лимит": 2 * (0.0002 + 0.00075)}                      # мейкер перп 0,02% + спот 0,075% (BNB)
CAPITAL = 1.5


def _fund(sym):
    parts = []
    for m in MONTHS:
        r = u._session().get(f"{u.VISION}/monthly/fundingRate/{sym}/{sym}-fundingRate-{m}.zip", timeout=60)
        if r.status_code != 200:
            continue
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
        df.columns = ["calc_time", "interval_h", "rate"]
        df["symbol"] = sym
        parts.append(df)
    return pd.concat(parts) if parts else None


def spot_symbols():
    out, marker = [], ""
    while True:
        r = u._session().get(u.S3, params={"delimiter": "/", "prefix": "data/spot/monthly/klines/",
                                           "marker": marker}, timeout=60)
        out += re.findall(r"<Prefix>data/spot/monthly/klines/([^/<]+)/</Prefix>", r.text)
        if "<IsTruncated>true</IsTruncated>" not in r.text:
            return set(out)
        marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", r.text).group(1)


def _spot_1h(sym):
    parts = []
    base = "https://data.binance.vision/data/spot"
    for m in MONTHS:
        g = u.fetch_zip_csv(f"{base}/monthly/klines/{sym}/1h/{sym}-1h-{m}.zip")
        if g is not None:
            parts.append(g)
    day = pd.Timestamp("2026-11-01", tz="UTC")
    while day < END:
        g = u.fetch_zip_csv(f"{base}/daily/klines/{sym}/1h/{sym}-1h-{day:%Y-%m-%d}.zip")
        if g is not None:
            parts.append(g)
        day += pd.Timedelta(days=1)
    if not parts:
        return None
    d = pd.concat(parts)[["t", "o"]].copy()
    d["t"] = pd.to_numeric(d["t"])
    d.loc[d["t"] > 1e14, "t"] //= 1000
    d["t"] = pd.to_datetime(d["t"], unit="ms", utc=True)
    d["symbol"] = sym
    return d


def download():
    hourly = pd.read_csv(CACHE / "hourly_all_20260508_20261105.csv.gz", parse_dates=["t"])
    perps = sorted(hourly.symbol.unique())
    with ThreadPoolExecutor(32) as ex:
        f = [x for x in ex.map(_fund, perps) if x is not None]
    fund = pd.concat(f)
    fund.to_csv(CACHE / "funding_all.csv.gz", index=False)
    spot = spot_symbols()
    both = [s for s in perps if s in spot]
    print(f"перпов {len(perps)}, с фандингом {fund.symbol.nunique()}, есть спот: {len(both)}")
    with ThreadPoolExecutor(32) as ex:
        s = [x for x in ex.map(_spot_1h, both) if x is not None]
    pd.concat(s).to_csv(CACHE / "spot_1h.csv.gz", index=False)
    print("спотовых символов с часовками:", len(s))


def simulate(entry, exit_=0.0001, t_from=START, t_to=END):
    fund = pd.read_csv(CACHE / "funding_all.csv.gz")
    fund["t"] = pd.to_datetime(fund.calc_time // 1000 * 1000, unit="ms", utc=True).dt.floor("1h")
    fund["r8"] = fund.rate * 8 / fund.interval_h
    hourly = pd.read_csv(CACHE / "hourly_all_20260508_20261105.csv.gz", parse_dates=["t"])
    spot = pd.read_csv(CACHE / "spot_1h.csv.gz", parse_dates=["t"])
    hourly["day"] = hourly.t.dt.normalize()
    dq = hourly.groupby(["symbol", "day"]).qv.sum().rename("dqv").reset_index().sort_values(["symbol", "day"])
    dq["liq"] = dq.groupby("symbol").dqv.transform(lambda v: v.shift(1).rolling(7, min_periods=7).mean())
    liq = dq.set_index(["symbol", "day"]).liq
    perp_o = hourly.set_index(["symbol", "t"]).o
    spot_o = spot.drop_duplicates(["symbol", "t"]).set_index(["symbol", "t"]).o
    trades = []
    for sym, f in fund[fund.symbol.isin(spot.symbol.unique())].groupby("symbol"):
        f = f.sort_values("t")
        f = f[(f.t >= t_from) & (f.t < t_to)]
        pos = None
        for row in f.itertuples():
            if pos is None:
                if row.r8 >= entry and liq.get((sym, row.t.normalize()), 0) >= 5e6:
                    pe, se = perp_o.get((sym, row.t)), spot_o.get((sym, row.t))
                    if pe and se and np.isfinite(pe) and np.isfinite(se):
                        pos = {"t0": row.t, "pe": pe, "se": se, "fund": 0.0, "n": 0}
                continue
            pos["fund"] += row.rate
            pos["n"] += 1
            if row.r8 < exit_:
                px, sx = perp_o.get((sym, row.t)), spot_o.get((sym, row.t))
                if not (px and sx):
                    continue
                trades.append({"symbol": sym, "t0": pos["t0"], "t1": row.t, "n_fund": pos["n"],
                               "funding": pos["fund"], "perp_leg": 1 - px / pos["pe"],
                               "spot_leg": sx / pos["se"] - 1})
                pos = None
    t = pd.DataFrame(trades)
    if t.empty:
        return t
    t["basis"] = t.perp_leg + t.spot_leg
    t["days"] = (t.t1 - t.t0).dt.total_seconds() / 86400
    return t


def report(t, title):
    print(f"\n=== {title} ===")
    if t.empty:
        print("сделок нет"); return
    print(f"сделок {len(t)}, монет {t.symbol.nunique()}, медиана удержания {t.days.median():.1f} дн, "
          f"фандинг в среднем {100*t.funding.mean():.3f}%, базис {100*t.basis.mean():+.3f}% "
          f"(худший {100*t.basis.min():+.1f}%)")
    span = (t.t1.max() - t.t0.min()).total_seconds() / 86400
    for name, c in COSTS.items():
        pnl = t.funding + t.basis - c
        cap_days = (t.days * CAPITAL).sum()
        apr_deployed = pnl.sum() / cap_days * 365
        bm = pnl.groupby(t.t1.dt.strftime("%m")).sum()
        print(f"  издержки «{name}» {100*c:.2f}%: на сделку {100*pnl.mean():+.3f}%, в плюсе {100*(pnl>0).mean():.0f}%, "
              f"годовых на задействованный капитал {100*apr_deployed:+.1f}%, "
              f"одновременно позиций в среднем {t.days.sum()/span:.1f}, по месяцам Σ% {dict((k, round(100*v,1)) for k,v in bm.items())}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "download":
        download()
    elif cmd == "dev":
        for e in (0.0003, 0.0005, 0.001):
            report(simulate(e, t_to=HOLDOUT), f"РАЗРАБОТКА, вход ≥ {100*e:.2f}% за 8 ч, выход < 0.01%")
    elif cmd == "holdout":
        e = float(sys.argv[2])
        report(simulate(e, t_from=HOLDOUT), f"КОНТРОЛЬ, вход ≥ {100*e:.2f}% за 8 ч")
