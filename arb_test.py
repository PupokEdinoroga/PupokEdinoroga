#!/usr/bin/env python3
"""
Межбиржевой арбитраж Binance ↔ Bybit (USDT-перпы), проверка на минутках в дни, когда монета «в игре».

Спред = close_Bybit / close_Binance − 1 по минуте (только минуты со сделками на обеих биржах).
Вход: |спред| ≥ порог; исполнение по закрытиям СЛЕДУЮЩЕЙ минуты (задержка 1 мин); шорт дорогой, лонг дешёвой.
Выход: спред сошёлся до ±exit_band, иначе в конце дня по рынку (сделка «не сошлась»).
Результат на сделку (на объём одной ноги) = захваченное схождение − издержки 4 сделок.
Фандинг не учтён. Только период разработки.

  python arb_test.py [число_монето_дней]
"""
import gzip
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

import universe as u

END = pd.Timestamp("2026-11-05", tz="UTC")
HOLDOUT = END - pd.Timedelta(days=60)
OUT = Path("results/arb"); OUT.mkdir(parents=True, exist_ok=True)
COSTS = {"рынок": 4 * 0.001, "лимит": 4 * 0.0002}
THRESH = (0.005, 0.01, 0.02, 0.03)
EXIT_BAND = 0.001


def bybit_1m(sym, day):
    r = u._session().get(f"https://public.bybit.com/trading/{sym}/{sym}{day:%Y-%m-%d}.csv.gz", timeout=120)
    if r.status_code != 200:
        return None
    df = pd.read_csv(io.BytesIO(gzip.decompress(r.content)), usecols=["timestamp", "price", "size"])
    df["t"] = pd.to_datetime((df.timestamp * 1000).astype("int64"), unit="ms", utc=True).dt.floor("1min")
    return df.groupby("t").agg(c=("price", "last"), n=("price", "size"))


def binance_1m(sym, day):
    g = u.fetch_zip_csv(f"{u.VISION}/daily/klines/{sym}/1m/{sym}-1m-{day:%Y-%m-%d}.zip")
    if g is None:
        return None
    t = pd.to_numeric(g["t"])
    t = np.where(t > 1e14, t // 1000, t)
    return pd.DataFrame({"c": g["c"].astype(float).to_numpy(), "n": g["n"].astype(float).to_numpy()},
                        index=pd.to_datetime(t, unit="ms", utc=True))


def one(item):
    sym, day = item
    a, b = binance_1m(sym, day), bybit_1m(sym, day)
    if a is None or b is None:
        return None
    m = a.join(b, lsuffix="_bn", rsuffix="_by", how="inner")
    m = m[(m.n_bn > 0) & (m.n_by > 0)]
    if len(m) < 60:
        return None
    m["spread"] = m.c_by / m.c_bn - 1
    s, cb, cy, idx = m.spread.to_numpy(), m.c_bn.to_numpy(), m.c_by.to_numpy(), m.index
    trades = []
    for thr in THRESH:
        i = 0
        while i < len(s) - 1:
            if abs(s[i]) < thr:
                i += 1; continue
            e = i + 1                                   # исполнение на следующей минуте
            d = np.sign(s[i])                           # +1: Bybit дороже → шорт Bybit, лонг Binance
            bn0, by0, s0 = cb[e], cy[e], s[e]
            j = e
            while j < len(s) - 1 and not (d * s[j] <= EXIT_BAND):
                j += 1
            # P&L на объём одной ноги: лонг дешёвой + шорт дорогой
            if d > 0:
                pnl = (cb[j] / bn0 - 1) + (1 - cy[j] / by0)
            else:
                pnl = (cy[j] / by0 - 1) + (1 - cb[j] / bn0)
            worst = (d * (s[e:j + 1] - s0)).max() if j > e else 0.0
            trades.append({"symbol": sym, "day": day, "thr": thr, "t_signal": idx[i], "spread_signal": s[i],
                           "spread_entry": s0, "minutes": int((idx[j] - idx[e]).total_seconds() // 60),
                           "converged": d * s[j] <= EXIT_BAND, "gross": pnl, "worst_widen": worst})
            i = j + 1
    return {"sym": sym, "day": day, "minutes": len(m), "abs_spread_p99": float(np.quantile(np.abs(s), 0.99)),
            "share_over": float((np.abs(s) >= 0.005).mean()), "trades": trades}


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    hourly = u.load_hourly_all(END - pd.Timedelta(days=181), END)
    uni = u.hourly_universe(hourly)
    uni = uni[(uni.t < HOLDOUT)]
    sd = uni.groupby(["symbol", uni.t.dt.normalize()]).size()
    sd = sd[sd >= 6].reset_index()[["symbol", "t"]]
    sd = sd.sample(min(n_sample, len(sd)), random_state=1)
    items = list(sd.itertuples(index=False, name=None))
    print(f"монето-дней «в игре» (≥6 ч): выборка {len(items)}", flush=True)
    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, items) if r]
    print(f"есть на обеих биржах: {len(res)}")
    st = pd.DataFrame([{k: v for k, v in r.items() if k != "trades"} for r in res])
    print(f"|спред| по минутам: медиана 99-го перцентиля {100*st.abs_spread_p99.median():.2f}%, "
          f"доля минут со спредом ≥0,5%: в среднем {100*st.share_over.mean():.2f}%")
    t = pd.DataFrame([x for r in res for x in r["trades"]])
    t.to_csv(OUT / "trades_dev.csv", index=False)
    rows = []
    for thr, g in t.groupby("thr"):
        row = {"порог_%": thr * 100, "сделок": len(g), "монето_дней": g.groupby(["symbol", "day"]).ngroups,
               "сошлось_%": round(100 * g.converged.mean(), 1), "мин_удержания_медиана": int(g.minutes.median()),
               "спред_на_входе_ср_%": round(100 * g.spread_entry.abs().mean(), 2),
               "до_издержек_ср_%": round(100 * g.gross.mean(), 3), "до_издержек_медиана_%": round(100 * g.gross.median(), 3),
               "худшее_расширение_%": round(100 * g.worst_widen.max(), 1)}
        for name, c in COSTS.items():
            row[f"{name}_ср_%"] = round(100 * (g.gross - c).mean(), 3)
            row[f"{name}_в_плюсе_%"] = round(100 * ((g.gross - c) > 0).mean(), 0)
        rows.append(row)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
