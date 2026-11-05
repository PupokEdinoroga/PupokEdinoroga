#!/usr/bin/env python3
"""
Бэктест стратегии «пробой уровня» на альткоинах (Binance USDT-M фьючерсы, только лонг).

Правила (версия 1):
  1. Монета «в игре»: средний оборот за последние 3 дня выше оборота
     за предыдущую неделю в IN_PLAY_MULT раз (считается только по прошлым дням).
  2. Уровень: на 5-минутках ищем локальные максимумы. Если за последние
     12 часов цена касалась одной зоны (допуск 0,2%) минимум MIN_TOUCHES раз
     и ни разу не закрылась выше неё, это уровень сопротивления.
  3. Вход: минутная свеча закрылась выше уровня, а её объём больше среднего
     за 30 минут в VOL_MULT раз. Вход по открытию следующей минуты.
  4. Стоп: чуть ниже уровня (0,3%). Сделки со стопом меньше 0,2% или больше 1,5% пропускаем.
  5. Выход, три варианта для сравнения:
       A: тейк по волатильности монеты (2 дневных ATR, в пределах 2-10%),
          после +1,5% стоп в безубыток.
       B: без тейка. После +1,5% стоп в безубыток, дальше плавающий стоп 1,5%
          от максимума. Выход, если минутка закрылась обратно под уровнем
          («пробой закончился»).
       C: половина позиции фиксируется на +2,5%, остаток как в варианте B.

Честные допущения:
  - Если внутри одной минуты задеты и стоп, и тейк, считаем, что сначала сработал стоп.
  - Комиссия 0,05% на каждую сторону, проскальзывание 0,05% на рыночные входы и выходы.
  - Список монет берётся по сегодняшнему обороту: это завышает результат
    (смещение выжившего). Учитывай это при выводах.
  - Сделки по разным монетам могут пересекаться по времени; кривая капитала
    считается упрощённо, по 1% риска на сделку подряд.

Запуск:
  pip install pandas numpy requests matplotlib
  python breakout_backtest.py                  # топ-15 альтов, 90 дней
  python breakout_backtest.py --days 60 --top 10
  python breakout_backtest.py --symbols SOLUSDT,DOGEUSDT,XRPUSDT
  python breakout_backtest.py --source vision  # архив data.binance.vision, если API закрыт
"""

import argparse
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE = "https://fapi.binance.com"
VISION = "https://data.binance.vision/data/futures/um"
# запасной список, если API недоступен и --symbols не задан (только для источника vision)
DEFAULT_ALTS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
                "SUIUSDT", "LTCUSDT", "DOTUSDT", "TRXUSDT", "BCHUSDT", "NEARUSDT", "AAVEUSDT", "UNIUSDT"]
EXCLUDE = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "BTCDOMUSDT", "BTCSTUSDT"}


@dataclass
class Params:
    # отбор монет
    top_n: int = 15
    days: int = 90
    min_listing_days: int = 120
    # монеты «в игре»
    in_play_recent_days: int = 3
    in_play_base_days: int = 7
    in_play_mult: float = 1.5
    # уровни (5m)
    pivot_k: int = 3                 # локальный максимум: выше 3 свечей слева и не ниже 3 справа
    level_lookback_bars: int = 144   # 12 часов на 5-минутках
    touch_tol: float = 0.002         # допуск касания 0,2%
    min_touches: int = 2
    max_dist_to_level: float = 0.03  # уровень не дальше 3% от цены
    # вход (1m)
    break_buf: float = 0.0005        # закрытие выше уровня хотя бы на 0,05%
    vol_mult: float = 2.0
    vol_window: int = 30
    # стоп
    stop_buf: float = 0.003
    min_stop: float = 0.002
    max_stop: float = 0.015
    # выход
    be_trigger: float = 0.015
    trail_pct: float = 0.015
    tp_atr_mult: float = 2.0
    tp_min: float = 0.02
    tp_max: float = 0.10
    partial_tp: float = 0.025
    max_hold_min: int = 24 * 60
    # издержки и риск
    fee: float = 0.0005
    slippage: float = 0.0005
    risk_per_trade: float = 0.01
    oos_frac: float = 0.33


# ---------------------------------------------------------------- загрузка данных

def _get(session, path, params=None, tries=6):
    import requests
    for i in range(tries):
        try:
            r = session.get(BASE + path, params=params, timeout=30)
            if r.status_code in (418, 429):
                wait = int(r.headers.get("Retry-After", 60))
                print(f"  лимит запросов биржи, жду {wait} с...")
                time.sleep(wait)
                continue
            if r.status_code in (403, 451):        # регион заблокирован, повторять бесполезно
                raise RuntimeError(f"Binance отказал в доступе (HTTP {r.status_code}), регион заблокирован")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            print(f"  ошибка сети ({e}), повтор...")
            time.sleep(3 * (i + 1))


def pick_symbols(session, p: Params):
    info = _get(session, "/fapi/v1/exchangeInfo")
    now_ms = int(time.time() * 1000)
    min_age_ms = p.min_listing_days * 86_400_000
    ok = set()
    for s in info["symbols"]:
        if (s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING" and s["symbol"] not in EXCLUDE
                and now_ms - int(s.get("onboardDate", now_ms)) > min_age_ms):
            ok.add(s["symbol"])
    tick = _get(session, "/fapi/v1/ticker/24hr")
    tick = [t for t in tick if t["symbol"] in ok]
    tick.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in tick[: p.top_n]]


def download_klines(session, symbol, start_ms, end_ms, cache_dir: Path):
    tag = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
    days = round((end_ms - start_ms) / 86_400_000)
    path = cache_dir / f"{symbol}_1m_{days}d_{tag}.csv.gz"
    if path.exists():
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df
    rows, cur = [], start_ms
    while cur < end_ms:
        batch = _get(session, "/fapi/v1/klines", {
            "symbol": symbol, "interval": "1m", "startTime": cur,
            "endTime": end_ms - 1, "limit": 1500})
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 60_000
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v", "ct", "qv",
                                     "n", "tbb", "tbq", "x"])
    df = df[["t", "o", "h", "l", "c", "v", "qv"]].astype(float)
    df["t"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index()
    df.to_csv(path)
    return df


def _vision_zip(session, url):
    """Скачивает zip с data.binance.vision; None, если файла нет (404)."""
    import io, zipfile
    import requests
    for i in range(4):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode()
            first = raw.split("\n", 1)[0]
            skip = 1 if first[:1].isalpha() else 0          # в новых файлах есть заголовок
            return pd.read_csv(io.StringIO(raw), header=None, skiprows=skip, usecols=range(8))
        except requests.RequestException as e:
            if i == 3:
                raise
            print(f"  ошибка сети ({e}), повтор...")
            time.sleep(3 * (i + 1))


def download_klines_vision(session, symbol, start_ms, end_ms, cache_dir: Path):
    """То же, что download_klines, но из публичного архива data.binance.vision:
    полные месяцы берём месячными файлами, остаток — дневными."""
    tag = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
    days = round((end_ms - start_ms) / 86_400_000)
    path = cache_dir / f"{symbol}_1m_{days}d_{tag}.csv.gz"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)
    start = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    end = pd.Timestamp(end_ms, unit="ms", tz="UTC")          # не включительно
    parts, day = [], start.normalize()
    while day < end:
        month_end = day + pd.offsets.MonthBegin(1)
        if day.day == 1 and month_end <= end:
            url = f"{VISION}/monthly/klines/{symbol}/1m/{symbol}-1m-{day:%Y-%m}.zip"
            got = _vision_zip(session, url)
            if got is not None:
                parts.append(got)
                day = month_end
                continue
        url = f"{VISION}/daily/klines/{symbol}/1m/{symbol}-1m-{day:%Y-%m-%d}.zip"
        got = _vision_zip(session, url)
        if got is not None:
            parts.append(got)
        day += pd.Timedelta(days=1)
    if not parts:
        raise RuntimeError("в архиве нет данных")
    raw = pd.concat(parts, ignore_index=True)
    df = pd.DataFrame({"t": raw[0], "o": raw[1], "h": raw[2], "l": raw[3],
                       "c": raw[4], "v": raw[5], "qv": raw[7]}).astype(float)
    df = df[(df["t"] >= start_ms) & (df["t"] < end_ms)]
    df["t"] = pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index()
    df.to_csv(path)
    return df


# ---------------------------------------------------------------- подготовка

def compute_levels(m5: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Для каждой закрытой 5m свечи: ближайший уровень сверху и число касаний."""
    h, c = m5["h"].to_numpy(), m5["c"].to_numpy()
    n, k = len(h), p.pivot_k
    piv = np.zeros(n, dtype=bool)
    if n > 2 * k:
        from numpy.lib.stride_tricks import sliding_window_view as swv
        left = swv(h, k)[: n - 2 * k].max(axis=1)        # h[j-k:j]
        right = swv(h, k)[k + 1: n - k + 1].max(axis=1)  # h[j+1:j+k+1]
        mid = h[k: n - k]
        piv[k: n - k] = (mid > left) & (mid >= right)
    piv_idx = np.flatnonzero(piv)

    level = np.full(n, np.nan)
    touches = np.zeros(n, dtype=int)
    ptr_lo = 0
    for t in range(n):
        # подтверждённые максимумы: j + k <= t, в пределах окна
        hi = np.searchsorted(piv_idx, t - k, side="right")
        while ptr_lo < hi and piv_idx[ptr_lo] < t - p.level_lookback_bars:
            ptr_lo += 1
        act = piv_idx[ptr_lo:hi]
        if len(act) < p.min_touches:
            continue
        prices = h[act]
        best = np.inf
        best_t = 0
        for P in prices:
            if P <= c[t] or (P - c[t]) / c[t] > p.max_dist_to_level:
                continue
            mask = np.abs(prices - P) / P <= p.touch_tol
            cnt = int(mask.sum())
            if cnt < p.min_touches:
                continue
            lvl = prices[mask].max()
            first = act[mask].min()
            if c[first: t + 1].max() > lvl:   # уже пробивали закрытием, уровень не действует
                continue
            if lvl < best:
                best, best_t = lvl, cnt
        if np.isfinite(best):
            level[t], touches[t] = best, best_t
    out = pd.DataFrame({"level": level, "touches": touches}, index=m5.index)
    out.index = out.index + pd.Timedelta(minutes=5)   # доступно только после закрытия свечи
    return out


def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    df = df.copy()
    m5 = df.resample("5min", label="left", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna()
    lv = compute_levels(m5, p)
    lv = lv.reindex(df.index, method="ffill")
    df["level"] = lv["level"]
    df["touches"] = lv["touches"].fillna(0).astype(int)

    d = df.resample("1D").agg({"h": "max", "l": "min", "c": "last", "qv": "sum"})
    recent = d["qv"].shift(1).rolling(p.in_play_recent_days).mean()
    base = d["qv"].shift(1 + p.in_play_recent_days).rolling(p.in_play_base_days).mean()
    d["in_play"] = recent > p.in_play_mult * base
    tr = pd.concat([d["h"] - d["l"], (d["h"] - d["c"].shift()).abs(),
                    (d["l"] - d["c"].shift()).abs()], axis=1).max(axis=1)
    d["atr_pct"] = (tr.rolling(14).mean() / d["c"]).shift(1)
    day = df.index.floor("1D")
    df["in_play"] = d["in_play"].reindex(day).fillna(False).astype(bool).to_numpy()
    df["atr_pct"] = d["atr_pct"].reindex(day).to_numpy()

    vol_avg = df["v"].rolling(p.vol_window).mean().shift(1)
    lvl = df["level"]
    df["signal"] = ((df["c"] > lvl * (1 + p.break_buf)) & (df["c"].shift(1) <= lvl)
                    & (df["v"] > p.vol_mult * vol_avg) & lvl.notna())
    return df


# ---------------------------------------------------------------- симуляция

def simulate(df: pd.DataFrame, symbol: str, variant: str, use_in_play: bool, p: Params):
    o, h, l, c = (df[x].to_numpy() for x in ("o", "h", "l", "c"))
    lvl_arr, atr = df["level"].to_numpy(), df["atr_pct"].to_numpy()
    touches = df["touches"].to_numpy()
    sig = df["signal"].to_numpy()
    if use_in_play:
        sig = sig & df["in_play"].to_numpy()
    idx = df.index
    n = len(df)
    trades, busy_until = [], -1
    slip, fee = p.slippage, p.fee

    for s in np.flatnonzero(sig):
        if s <= busy_until or s + 1 >= n:
            continue
        e = s + 1
        level = lvl_arr[s]
        entry = o[e] * (1 + slip)
        stop = level * (1 - p.stop_buf)
        risk = (entry - stop) / entry
        if not (p.min_stop <= risk <= p.max_stop):
            continue
        tp = None
        if variant == "A":
            a = atr[s]
            tp_pct = p.tp_min if not np.isfinite(a) else min(max(p.tp_atr_mult * a, p.tp_min), p.tp_max)
            tp = entry * (1 + tp_pct)

        size, pnl, peak, be_on, partial_done = 1.0, 0.0, entry, False, False
        reason, j_end = "time", min(e + p.max_hold_min, n - 1)
        exits = []
        j = e
        for j in range(e, j_end + 1):
            # 1) стоп (худший случай проверяем первым)
            if l[j] <= stop:
                px = min(o[j], stop) * (1 - slip)
                exits.append((size, px))
                reason = "be/trail" if be_on else "stop"
                size = 0
                break
            # 2) тейк (лимитный ордер)
            if tp is not None and h[j] >= tp:
                exits.append((size, max(tp, o[j])))
                reason, size = "tp", 0
                break
            # 3) частичная фиксация (C)
            if variant == "C" and not partial_done and h[j] >= entry * (1 + p.partial_tp):
                exits.append((0.5, max(entry * (1 + p.partial_tp), o[j])))
                size, partial_done = 0.5, True
                be_on = True
                stop = max(stop, entry)
            # 4) пробой закончился: закрытие обратно под уровнем (B, C)
            if variant in ("B", "C") and c[j] < level:
                exits.append((size, c[j] * (1 - slip)))
                reason, size = "fade", 0
                break
            # 5) обновление стопа по итогам свечи (действует со следующей)
            peak = max(peak, h[j])
            if not be_on and peak >= entry * (1 + p.be_trigger):
                be_on = True
                stop = max(stop, entry)
            if be_on and variant in ("B", "C"):
                stop = max(stop, peak * (1 - p.trail_pct))
        if size > 0:
            exits.append((size, c[j] * (1 - slip)))
        for sz, px in exits:
            pnl += sz * ((px / entry - 1) - 2 * fee)
        exit_px = sum(sz * px for sz, px in exits)
        trades.append({
            "symbol": symbol, "variant": variant, "in_play_filter": use_in_play,
            "entry_time": idx[e], "exit_time": idx[j], "level": level,
            "touches": int(touches[s]), "entry": entry, "exit_avg": exit_px,
            "risk_pct": risk * 100, "pnl_pct": pnl * 100, "R": pnl / risk,
            "reason": reason, "hold_min": j - e + 1,
        })
        busy_until = j
    return trades


# ---------------------------------------------------------------- отчёт

def stats(t: pd.DataFrame, p: Params) -> dict:
    if t.empty:
        return {"сделок": 0}
    t = t.sort_values("exit_time")
    R = t["R"].to_numpy()
    cum = np.cumsum(R)
    dd = (np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum).max()
    gains, losses = R[R > 0].sum(), -R[R < 0].sum()
    eq = np.prod(1 + p.risk_per_trade * R)
    return {
        "сделок": len(R),
        "винрейт_%": round(100 * (R > 0).mean(), 1),
        "средний_R": round(R.mean(), 3),
        "сумма_R": round(R.sum(), 1),
        "профит_фактор": round(gains / losses, 2) if losses > 0 else np.inf,
        "макс_просадка_R": round(dd, 1),
        "доходность_%_при_риске_1%": round(100 * (eq - 1), 1),
    }


def main():
    ap = argparse.ArgumentParser(description="Бэктест пробоя уровня на альткоинах")
    ap.add_argument("--symbols", help="список через запятую, например SOLUSDT,DOGEUSDT")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default="results")
    ap.add_argument("--source", choices=["auto", "api", "vision"], default="auto",
                    help="api: REST Binance Futures; vision: архив data.binance.vision; "
                         "auto: API, а при его недоступности архив")
    args = ap.parse_args()

    import requests
    p = Params(top_n=args.top, days=args.days)
    out = Path(args.out); out.mkdir(exist_ok=True)
    cache = Path("data_cache"); cache.mkdir(exist_ok=True)
    session = requests.Session()

    source = args.source
    if args.symbols:
        symbols = args.symbols.upper().split(",")
    elif source == "vision":
        symbols = DEFAULT_ALTS[: p.top_n]
    else:
        try:
            symbols = pick_symbols(session, p)
        except Exception as e:
            if source == "api":
                sys.exit(f"Не удалось подключиться к Binance Futures: {e}\n"
                         "Проверь интернет или VPN (в некоторых странах Binance недоступен).")
            print(f"API Binance недоступен ({e}); беру архив data.binance.vision "
                  "и запасной список монет.")
            source, symbols = "vision", DEFAULT_ALTS[: p.top_n]
    print("Монеты:", ", ".join(symbols), f"(источник: {source})")

    end_ms = int(time.time() // 86400 * 86400 * 1000)   # до начала сегодняшнего дня UTC
    start_ms = end_ms - p.days * 86_400_000
    data = {}
    for i, s in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] загружаю {s}...")
        try:
            loader = download_klines_vision if source == "vision" else download_klines
            data[s] = loader(session, s, start_ms, end_ms, cache)
        except Exception as e:
            print(f"  пропускаю {s}: {e}")

    if not data:
        sys.exit("Не удалось загрузить данные ни по одной монете.")
    run(data, p, out)


def run(data: dict, p: Params, out: Path):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    all_trades = []
    for s, raw in data.items():
        if len(raw) < 20 * 1440:
            print(f"  {s}: мало данных, пропускаю")
            continue
        df = prepare(raw, p)
        for v in ("A", "B", "C"):
            for f in (True, False):
                all_trades += simulate(df, s, v, f, p)
    trades = pd.DataFrame(all_trades)
    if trades.empty:
        print("Сделок не найдено. Попробуй ослабить фильтры (например, --days больше).")
        return
    trades.to_csv(out / "trades.csv", index=False)

    start = min(d.index[0] for d in data.values())
    end = max(d.index[-1] for d in data.values())
    cutoff = start + (end - start) * (1 - p.oos_frac)
    rows = []
    for (v, f), g in trades.groupby(["variant", "in_play_filter"]):
        for part, gg in (("весь период", g), ("обучение", g[g.entry_time < cutoff]),
                         ("проверка (OOS)", g[g.entry_time >= cutoff])):
            rows.append({"вариант": v, "фильтр_в_игре": "да" if f else "нет",
                         "часть": part, **stats(gg, p)})
    summary = pd.DataFrame(rows)
    summary.to_csv(out / "summary.csv", index=False)

    per_sym = (trades[trades.in_play_filter]
               .groupby(["variant", "symbol"])["R"]
               .agg(сделок="count", сумма_R="sum", средний_R="mean").round(2))
    per_sym.to_csv(out / "by_symbol.csv")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print("\n=== ИТОГ (R = сколько рисков заработано/потеряно, 1R = размер стопа) ===")
    print(summary[summary["часть"] == "весь период"].drop(columns="часть").to_string(index=False))
    print("\n=== Обучение vs проверка на новых данных ===")
    print(summary[summary["часть"] != "весь период"].to_string(index=False))
    print(f"\nПериод: {start:%Y-%m-%d} - {end:%Y-%m-%d}, граница OOS: {cutoff:%Y-%m-%d}")
    print(f"Файлы: {out/'summary.csv'}, {out/'trades.csv'}, {out/'by_symbol.csv'}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(11, 5))
        for (v, f), g in trades.groupby(["variant", "in_play_filter"]):
            g = g.sort_values("exit_time")
            ax.plot(g["exit_time"], g["R"].cumsum(),
                    label=f"{v}, фильтр {'вкл' if f else 'выкл'}",
                    linestyle="-" if f else "--")
        ax.axvline(cutoff, color="gray", linewidth=1)
        ax.set_title("Накопленный результат в R (после комиссий)")
        ax.set_ylabel("R"); ax.legend(); ax.grid(alpha=0.3)
        fig.tight_layout(); fig.savefig(out / "equity.png", dpi=120)
        print(f"График: {out/'equity.png'}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
