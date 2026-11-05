#!/usr/bin/env python3
"""
Версия 3 «по ленте»: пробой уровня у монет в игре, вход по ускорению ленты (aggTrades Binance).

Шаг 1 (candidates): по 5m данным v2 находим свечи, где цена пересекла уровень
  (уровень ≥2 касаний, консолидация, монета в игре в этот час) — без фильтра объёма.
Шаг 2 (simulate): для каждой такой свечи качаем ленту за день и моделируем по сделкам:
  пересечение = первая сделка за уровнем (+break_buf);
  вход (вариант TAPE) — в течение trig_window с после пересечения: сделок за 10 с ≥ speed_mult ×
     обычного темпа (30 мин до свечи) и доля агрессора по направлению ≥ aggr_share;
     вход (вариант CROSS, для сравнения) — сразу на сделке пересечения;
  выход: стоп за уровнем, тейк max(tp_atr×ATR, tp_min), «нет импульса» за noimp_s,
     «лента затихла» после хода ≥ 1×ATR, максимум max_hold_s.
Цены — по сделкам ленты, поэтому порядок стоп/тейк известен точно.

  python tape_v3.py candidates      # кандидаты в results/v3/candidates.csv
  python tape_v3.py simulate        # моделирование по ленте (только период разработки)
"""
import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

import strategy_v2 as v
import universe as u
from backtest_universe import load_symbol, segments

OUT = Path("results/v3")
END = pd.Timestamp("2026-11-05", tz="UTC")
START = END - pd.Timedelta(days=180)
HOLDOUT = END - pd.Timedelta(days=60)


@dataclass
class P3:
    stop_buf: float = 0.006
    break_buf: float = 0.0005
    trig_window_s: int = 60
    speed_win_s: int = 10
    speed_mult: float = 3.0
    aggr_share: float = 0.6
    base_min: int = 30
    tp_atr: float = 3.0
    tp_min: float = 0.01
    noimp_s: int = 60
    noimp_atr: float = 0.5
    fade_after_atr: float = 1.0
    fade_speed: float = 1.5
    max_hold_s: int = 15 * 60
    fee: float = 0.0005
    slippage: float = 0.0005
    min_stop: float = 0.002
    max_stop: float = 0.03


# ---------------------------------------------------------------- кандидаты
def candidates():
    hourly = u.load_hourly_all(END - pd.Timedelta(days=181), END)
    uni = u.hourly_universe(hourly)
    uni = uni[(uni.t >= START) & (uni.t < HOLDOUT)]
    hours_by = {(s, sd): set(g.t) for (s, sd), g in uni.groupby(["symbol", "side"])}
    p2 = v.P2()
    rows = []
    for s, g in uni.groupby("symbol"):
        ds = {d + pd.Timedelta(days=k) for d in set(g.t.dt.normalize()) for k in (-1, 0, 1)}
        df = load_symbol(s, {d for d in ds if d < END}, END)
        if df is None or df.empty:
            continue
        for seg in segments(df):
            if len(seg) < 200:
                continue
            for side in ("long", "short"):
                hrs = hours_by.get((s, side))
                if not hrs:
                    continue
                y = seg if side == "long" else pd.DataFrame(
                    {"o": 1 / seg.o, "h": 1 / seg.l, "l": 1 / seg.h, "c": 1 / seg.c, "v": seg.v}, index=seg.index)
                x = v.prepare_v2(y, p2)
                lvl = x.level
                cons_low = x.l.rolling(p2.cons_bars).min().shift(1)
                cross = (lvl.notna() & x.atr.notna() & (x.c.shift(1) <= lvl)
                         & (x.h > lvl * (1 + p2.break_buf))
                         & (cons_low >= lvl * (1 - p2.cons_atr * x.atr)))
                cross &= x.index.floor("1h").isin(list(hrs))
                for t in x.index[cross.to_numpy()]:
                    L = lvl.at[t]
                    rows.append({"symbol": s, "side": side, "bar": t,
                                 "level": L if side == "long" else 1 / L, "atr": x.atr.at[t]})
    c = pd.DataFrame(rows).sort_values(["bar", "symbol"])
    OUT.mkdir(parents=True, exist_ok=True)
    c.to_csv(OUT / "candidates.csv", index=False)
    print(f"кандидатов: {len(c)}, монето-дней: {c.groupby(['symbol', c.bar.dt.normalize()]).ngroups}")


# ---------------------------------------------------------------- лента
TAPE = Path("data_cache/aggtrades")


def load_tape(sym, day):
    """Лента за день: t (мс), price, qty, buy (агрессор — покупатель). Кэш в npz."""
    path = TAPE / sym / f"{day:%Y-%m-%d}.npz"
    if path.exists():
        z = np.load(path)
        return z["t"], z["p"], z["q"], z["b"]
    url = f"{u.VISION}/daily/aggTrades/{sym}/{sym}-aggTrades-{day:%Y-%m-%d}.zip"
    for i in range(4):
        try:
            r = u._session().get(url, timeout=120)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            break
        except Exception:
            if i == 3:
                return None
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        raw = zf.read(zf.namelist()[0])
    skip = 1 if raw[:1].isalpha() else 0
    df = pd.read_csv(io.BytesIO(raw), header=None, skiprows=skip, usecols=[1, 2, 5, 6],
                     names=["p", "q", "t", "m"])
    t = df.t.to_numpy(np.int64)
    if t.max() > 1e14:
        t //= 1000
    m = df.m.astype(str).str.lower().eq("true").to_numpy()
    p, q, b = df.p.to_numpy(np.float64), df.q.to_numpy(np.float64), ~m
    # на диск не сохраняем: ~8 тыс. монето-дней ленты не поместятся в квоту
    return t, p, q, b


def sim_one(c, tape, p: P3, mode):
    t, pr, q, b = tape
    d = 1 if c.side == "long" else -1
    L, a = c.level, c.atr
    t0 = int(c.bar.value // 10**6)
    i0, i1 = np.searchsorted(t, [t0, t0 + 5 * 60_000])
    if i1 <= i0:
        return None
    trig = L * (1 + d * p.break_buf)
    seg = pr[i0:i1]
    hit = np.flatnonzero(d * (seg - trig) > 0)
    if len(hit) == 0:
        return None
    ic = i0 + hit[0]                                  # сделка пересечения
    tc = t[ic]
    jb0 = np.searchsorted(t, t0 - p.base_min * 60_000)
    base = (i0 - jb0) / (p.base_min * 60) * p.speed_win_s   # обычное число сделок за 10 с
    base = max(base, 1.0)
    qv = q * pr
    aggr = b if d == 1 else ~b

    def speed_at(k):
        j = np.searchsorted(t, t[k] - p.speed_win_s * 1000, side="right")
        n = k - j + 1
        vol = qv[j:k + 1]
        share = vol[aggr[j:k + 1]].sum() / vol.sum() if vol.sum() > 0 else 0
        return n / base, share

    if mode == "CROSS":
        ie = ic
    else:
        ie = None
        iend = np.searchsorted(t, tc + p.trig_window_s * 1000)
        for k in range(ic, iend):
            if d * (pr[k] - L) < 0:                    # вернулись за уровень — отмена
                break
            sp, sh = speed_at(k)
            if sp >= p.speed_mult and sh >= p.aggr_share:
                ie = k
                break
        if ie is None:
            return None
    if ie + 1 >= len(t):
        return None
    entry = pr[ie + 1] * (1 + d * p.slippage)          # исполнение на следующей сделке
    stop = L * (1 - d * p.stop_buf)
    risk = d * (entry - stop) / entry
    if not (p.min_stop <= risk <= p.max_stop):
        return None
    tp = entry * (1 + d * max(p.tp_atr * a, p.tp_min))
    te = t[ie + 1]
    k_end = np.searchsorted(t, te + p.max_hold_s * 1000)
    best, reason, px, kx = 0.0, "time", None, None
    k_noimp = np.searchsorted(t, te + p.noimp_s * 1000)
    for k in range(ie + 1, min(k_end, len(t))):
        mv = d * (pr[k] / entry - 1)
        if d * (pr[k] - stop) <= 0:
            px, reason, kx = pr[k] * (1 - d * p.slippage), "stop", k; break
        if d * (pr[k] - tp) > 0:
            px, reason, kx = tp, "tp", k; break
        best = max(best, mv)
        if k >= k_noimp and best < p.noimp_atr * a:
            px, reason, kx = pr[k] * (1 - d * p.slippage), "no_impulse", k; break
        if best >= p.fade_after_atr * a and k % 20 == 0:
            sp, _ = speed_at(k)
            if sp < p.fade_speed:
                px, reason, kx = pr[k] * (1 - d * p.slippage), "tape_fade", k; break
    if px is None:
        kx = min(k_end, len(t)) - 1
        px = pr[kx] * (1 - d * p.slippage)
    pnl = d * (px / entry - 1) - 2 * p.fee
    gross = d * ((px / (1 - d * p.slippage) if reason != "tp" else px) / (entry / (1 + d * p.slippage)) - 1)
    return {"symbol": c.symbol, "side": c.side, "mode": mode, "bar": c.bar,
            "cross_ms": int(tc), "entry_ms": int(te), "exit_ms": int(t[kx]),
            "delay_s": (te - tc) / 1000, "hold_s": (t[kx] - te) / 1000,
            "level": L, "entry": entry, "exit": px, "atr_pct": a * 100, "risk_pct": risk * 100,
            "pnl_pct": pnl * 100, "gross_pct": gross * 100, "R": pnl / risk, "reason": reason}


def sim_pre(c, tape, p: P3):
    """Режим PRE — правила из видео «9 шагов пробоя»: стоп-ордер за 1–2 тика ДО уровня
    (уровень × (1 − 0,02%)), стоп 0,2% от входа, 50% фиксируем на +1%, 50% на +2,5% (лимитки),
    «нет импульса» — за 30 с не прошли +0,3% → выход по рынку; максимум 15 мин."""
    t, pr, q, b = tape
    d = 1 if c.side == "long" else -1
    L = c.level
    t0 = int(c.bar.value // 10**6)
    i0, i1 = np.searchsorted(t, [t0, t0 + 5 * 60_000])
    if i1 <= i0:
        return None
    trig = L * (1 - d * 0.0002)
    hit = np.flatnonzero(d * (pr[i0:i1] - trig) >= 0)
    if len(hit) == 0:
        return None
    ie = i0 + hit[0]
    entry = max(d * pr[ie], d * trig) * d * (1 + d * p.slippage)
    stop = entry * (1 - d * 0.002)
    tp1, tp2 = entry * (1 + d * 0.01), entry * (1 + d * 0.025)
    te = t[ie]
    k_end = min(np.searchsorted(t, te + p.max_hold_s * 1000), len(t))
    k_noimp = np.searchsorted(t, te + 30_000)
    size, pnl, best, reason, kx = 1.0, 0.0, 0.0, "time", None
    for k in range(ie + 1, k_end):
        if d * (pr[k] - stop) <= 0:
            pnl += size * (d * (pr[k] * (1 - d * p.slippage) / entry - 1)); size = 0
            reason, kx = ("stop" if reason != "tp1" else "tp1+stop"), k; break
        if size == 1.0 and d * (pr[k] - tp1) > 0:
            pnl += 0.5 * 0.01; size = 0.5; reason = "tp1"
        if size == 0.5 and d * (pr[k] - tp2) > 0:
            pnl += 0.5 * 0.025; size = 0; reason, kx = "tp2", k; break
        best = max(best, d * (pr[k] / entry - 1))
        if size == 1.0 and k >= k_noimp and best < 0.003:
            pnl += d * (pr[k] * (1 - d * p.slippage) / entry - 1); size = 0
            reason, kx = "no_impulse", k; break
    if size > 0:
        kx = k_end - 1
        pnl += size * (d * (pr[kx] * (1 - d * p.slippage) / entry - 1))
    gross = pnl + 2 * p.slippage * 0.5          # грубо: без проскальзывания по рыночным частям
    pnl -= 2 * p.fee
    return {"symbol": c.symbol, "side": c.side, "mode": "PRE", "bar": c.bar,
            "cross_ms": int(te), "entry_ms": int(te), "exit_ms": int(t[kx]), "delay_s": 0.0,
            "hold_s": (t[kx] - te) / 1000, "level": L, "entry": entry, "exit": np.nan,
            "atr_pct": c.atr * 100, "risk_pct": 0.2, "pnl_pct": pnl * 100, "gross_pct": gross * 100,
            "R": pnl / 0.002, "reason": reason}


def add_liquidity(c):
    """Оборот и число сделок за 24 ч до свечи (из часовок) — для фильтров из видео (≥100 млн $, ≥2 млн сделок)."""
    h = pd.read_csv("data_cache/hourly_all_20260508_20261105.csv.gz", parse_dates=["t"]).sort_values(["symbol", "t"])
    h["qv24"] = h.groupby("symbol").qv.transform(lambda v: v.rolling(24, min_periods=1).sum())
    h["n24"] = h.groupby("symbol").n.transform(lambda v: v.rolling(24, min_periods=1).sum())
    h["t_av"] = h.t + pd.Timedelta(hours=1)
    c = c.sort_values("bar")
    out = pd.merge_asof(c, h[["symbol", "t_av", "qv24", "n24"]].sort_values("t_av"),
                        left_on="bar", right_on="t_av", by="symbol", direction="backward")
    return out.drop(columns="t_av")


def simulate(workers=8):
    c = pd.read_csv(OUT / "candidates.csv", parse_dates=["bar"])
    c = c[c.bar < HOLDOUT]
    c = add_liquidity(c)
    c["day"] = c.bar.dt.normalize()
    groups = list(c.groupby(["symbol", "day"]))
    p = P3()
    print(f"кандидатов {len(c)}, монето-дней ленты: {len(groups)}", flush=True)

    def work(item):
        (sym, day), g = item
        tape = load_tape(sym, day)
        if tape is None:
            return []
        out = []
        busy = {m: {"long": 0, "short": 0} for m in ("CROSS", "TAPE", "PRE")}
        for r in g.sort_values("bar").itertuples():
            for mode in ("CROSS", "TAPE", "PRE"):
                if int(r.bar.value // 10**6) < busy[mode][r.side]:
                    continue
                res = sim_pre(r, tape, p) if mode == "PRE" else sim_one(r, tape, p, mode)
                if res:
                    res["qv24"], res["n24"] = r.qv24, r.n24
                if res:
                    out.append(res)
                    busy[mode][r.side] = res["exit_ms"]
        return out

    res, done = [], 0
    with ThreadPoolExecutor(workers) as ex:
        for out in ex.map(work, groups):
            res += out
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(groups)} монето-дней, сделок {len(res)}", flush=True)
    t = pd.DataFrame(res)
    t.to_csv(OUT / "trades_dev.csv", index=False)
    report(t)


def report(t):
    t = t.copy()
    t["month"] = pd.to_datetime(t.entry_ms, unit="ms").dt.strftime("%m")
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    rows = []
    for (mode, side), g in [((m, "все"), g) for m, g in t.groupby("mode")] + list(t.groupby(["mode", "side"])):
        bm = g.groupby("month").R.mean()
        rows.append({"режим": mode, "сторона": side, "сделок": len(g),
                     "винрейт_%": round(100 * (g.R > 0).mean(), 1), "средний_R": round(g.R.mean(), 3),
                     "ср_pnl_%": round(g.pnl_pct.mean(), 3), "до_издержек_%": round(g.gross_pct.mean(), 3),
                     "ср_стоп_%": round(g.risk_pct.mean(), 2), "медиана_удерж_с": int(g.hold_s.median()),
                     "месяцев_в_плюсе": f"{(bm > 0).sum()}/{len(bm)}"})
    print(pd.DataFrame(rows).to_string(index=False))
    for mode, g in t.groupby("mode"):
        print(mode, "выходы:", g.groupby("reason").R.agg(["count", "mean"]).round(2).to_dict("index"))


if __name__ == "__main__":
    {"candidates": candidates, "simulate": simulate}[sys.argv[1]]()
