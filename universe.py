#!/usr/bin/env python3
"""
Ежедневный отбор монет «в игре» по всему рынку Binance USDT-M (включая делистнутые).

Правило: на день D монета в игре, если по итогам дня D-1 (UTC) она входит
в топ-N по количеству сделок ИЛИ в топ-N по росту (закрытие/открытие дня).
Данные: дневные свечи из архива data.binance.vision (в них есть число сделок).
"""
import io
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
VISION = "https://data.binance.vision/data/futures/um"
# BTC/ETH и стейблы, как в основном бэктесте; плюс индексы и «стейбл против стейбла»
EXCLUDE = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "BTCDOMUSDT", "BTCSTUSDT",
           "TUSDUSDT", "USDPUSDT", "DEFIUSDT", "BUSDUSDT", "EURUSDT", "USDEUSDT"}
KCOLS = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbb", "tbq", "x"]

_local = None


def _session():
    global _local
    import threading
    if _local is None:
        _local = threading.local()
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def fetch_zip_csv(url, tries=4):
    """CSV из zip архива Binance как DataFrame с колонками KCOLS; None при 404."""
    for i in range(tries):
        try:
            r = _session().get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode()
            skip = 1 if raw[:1].isalpha() else 0
            df = pd.read_csv(io.StringIO(raw), header=None, skiprows=skip)
            df = df.iloc[:, :12]
            df.columns = KCOLS[: df.shape[1]]
            return df
        except (requests.RequestException, zipfile.BadZipFile) as e:
            if i == tries - 1:
                print(f"  не удалось {url}: {e}")
                return None
            time.sleep(2 * (i + 1))


def list_symbols():
    """Все USDT-M символы из архива (включая делистнутые)."""
    out, marker = [], ""
    while True:
        r = _session().get(S3, params={"delimiter": "/", "prefix": "data/futures/um/daily/klines/",
                                       "marker": marker}, timeout=60)
        r.raise_for_status()
        pref = re.findall(r"<Prefix>data/futures/um/daily/klines/([^/<]+)/</Prefix>", r.text)
        out += pref
        if "<IsTruncated>true</IsTruncated>" not in r.text:
            break
        marker = re.search(r"<NextMarker>([^<]+)</NextMarker>", r.text).group(1)
    syms = sorted({s for s in out if s.endswith("USDT") and s not in EXCLUDE
                   and not re.search(r"_\d{6}$", s)})       # без квартальных контрактов
    return syms


def _symbol_klines(sym, interval, start, end):
    """Свечи интервала interval за [start, end): месячные файлы, текущий месяц — дневные."""
    parts, m = [], start.tz_localize(None).to_period("M")
    end_p = end.tz_localize(None).to_period("M")
    while m <= end_p:
        ms = m.to_timestamp().tz_localize("UTC")
        me = (m + 1).to_timestamp().tz_localize("UTC")
        got = None
        if me <= end:
            got = fetch_zip_csv(f"{VISION}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{m}.zip")
            if got is None:            # за закрытый месяц нет файла — символа в этом месяце не было
                m += 1
                continue
        if got is None:
            day = max(ms, start.normalize())
            while day < min(me, end):
                g = fetch_zip_csv(f"{VISION}/daily/klines/{sym}/{interval}/{sym}-{interval}-{day:%Y-%m-%d}.zip")
                if g is not None:
                    parts.append(g)
                day += pd.Timedelta(days=1)
        else:
            parts.append(got)
        m += 1
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    df = df[["t", "o", "h", "l", "c", "v", "qv", "n"]].copy()
    df["t"] = pd.to_numeric(df["t"])
    df.loc[df["t"] > 1e14, "t"] //= 1000                    # в новых файлах время в микросекундах
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df[(df["t"] >= start) & (df["t"] < end)]
    df["symbol"] = sym
    return df


def _has_recent(sym, start):
    """Быстрая проверка: есть ли у символа данные после start (по 1d месячным/дневным файлам)."""
    for i in range(4):
        try:
            r = _session().get(S3, params={
                "prefix": f"data/futures/um/daily/klines/{sym}/1h/{sym}-1h-",
                "marker": f"data/futures/um/daily/klines/{sym}/1h/{sym}-1h-{start:%Y-%m-%d}"}, timeout=60)
            r.raise_for_status()
            return "<Key>" in r.text
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return True                       # не смогли проверить — лучше скачать лишнее


def load_hourly_all(start, end, cache_dir=Path("data_cache"), workers=32):
    """Часовые свечи всех USDT-M символов за [start, end), включая делистнутые. Кэшируется."""
    path = cache_dir / f"hourly_all_{start:%Y%m%d}_{end:%Y%m%d}.csv.gz"
    if path.exists():
        return pd.read_csv(path, parse_dates=["t"])
    syms = list_symbols()
    with ThreadPoolExecutor(workers) as ex:
        alive = list(ex.map(lambda s: _has_recent(s, start), syms))
    syms = [s for s, a in zip(syms, alive) if a]
    print(f"Символов с данными в периоде: {len(syms)}, качаю часовки...", flush=True)
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda s: _symbol_klines(s, "1h", start, end), syms))
    d = pd.concat([r for r in res if r is not None], ignore_index=True)
    d = d.drop_duplicates(["symbol", "t"]).sort_values(["t", "symbol"])
    cache_dir.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp.gz")
    d.to_csv(tmp, index=False)
    tmp.replace(path)
    return d


def hourly_universe(hourly: pd.DataFrame, top_trades=20, top_move=20, min_move=0.0,
                    vol_surge=0.0, base_days=7):
    """Списки «в игре» на каждый час по ТЕКУЩЕМУ дню UTC.

    На час, начинающийся в T, используются только закрытые часовые свечи дня до T:
    рост = close последнего часа / open первого часа дня − 1, сделки = сумма за день.
      long:  топ-N по росту  ∪  (топ-N по сделкам, у кого рост > 0)
      short: топ-N по падению ∪ (топ-N по сделкам, у кого рост < 0)
    Опции версии 2.1:
      min_move  — дополнительно |рост от открытия дня| ≥ min_move (0.2 = 20%);
      vol_surge — темп оборота сегодня (оборот за прошедшие часы × 24/часов) ≥ vol_surge ×
                  средний дневной оборот за base_days прошлых дней.
    В первый час дня (00:00–01:00 UTC) списков нет.
    Возвращает DataFrame [t, symbol, side, growth, trades_today]."""
    h = hourly[["symbol", "t", "o", "c", "n", "qv"]].copy()
    h["day"] = h["t"].dt.normalize()
    h = h.sort_values(["symbol", "t"])
    g = h.groupby(["symbol", "day"])
    h["day_open"] = g["o"].transform("first")
    h["n_cum"] = g["n"].cumsum()
    h["qv_pace"] = g["qv"].cumsum() * 24 / (g.cumcount() + 1)
    dq = h.groupby(["symbol", "day"])["qv"].sum().rename("dqv").reset_index()
    dq["base_qv"] = (dq.groupby("symbol")["dqv"]
                     .transform(lambda v: v.shift(1).rolling(base_days, min_periods=3).mean()))
    h = h.merge(dq[["symbol", "day", "base_qv"]], on=["symbol", "day"], how="left")
    h["growth"] = h["c"] / h["day_open"] - 1
    h["t_valid"] = h["t"] + pd.Timedelta(hours=1)          # доступно после закрытия часа
    h = h[h["t_valid"].dt.normalize() == h["day"]]          # последний час дня не переносим на завтра
    rows = []
    for t, x in h.groupby("t_valid"):
        top_n = x.nlargest(top_trades, "n_cum")
        longs = set(x.nlargest(top_move, "growth").symbol) | set(top_n[top_n.growth > 0].symbol)
        shorts = set(x.nsmallest(top_move, "growth").symbol) | set(top_n[top_n.growth < 0].symbol)
        if min_move > 0 or vol_surge > 0:
            ok = pd.Series(True, index=x.index)
            if vol_surge > 0:
                ok &= x.qv_pace >= vol_surge * x.base_qv
            longs &= set(x[ok & (x.growth >= min_move)].symbol)
            shorts &= set(x[ok & (x.growth <= -min_move)].symbol)
        xi = x.set_index("symbol")
        for side, ss in (("long", longs), ("short", shorts)):
            for s in ss:
                rows.append((t, s, side, xi.at[s, "growth"], xi.at[s, "n_cum"]))
    return pd.DataFrame(rows, columns=["t", "symbol", "side", "growth", "trades_today"])


def _symbol_daily(sym, start, end):
    parts, m = [], start.to_period("M")
    while m <= end.to_period("M"):
        ms = m.to_timestamp().tz_localize("UTC")
        me = (m + 1).to_timestamp().tz_localize("UTC")
        got = None
        if me <= end:
            got = fetch_zip_csv(f"{VISION}/monthly/klines/{sym}/1d/{sym}-1d-{m}.zip")
        if got is None:                                   # текущий месяц или нет месячного файла
            day = max(ms, start)
            while day < min(me, end):
                g = fetch_zip_csv(f"{VISION}/daily/klines/{sym}/1d/{sym}-1d-{day:%Y-%m-%d}.zip")
                if g is not None:
                    parts.append(g)
                day += pd.Timedelta(days=1)
        else:
            parts.append(got)
        m += 1
    if not parts:
        return None
    df = pd.concat(parts, ignore_index=True)
    df["symbol"] = sym
    return df


def load_daily_all(start, end, cache_dir=Path("data_cache"), workers=16):
    """Дневные свечи всех символов за [start, end). Кэшируется."""
    path = cache_dir / f"daily_all_{start:%Y%m%d}_{end:%Y%m%d}.csv.gz"
    if path.exists():
        return pd.read_csv(path, parse_dates=["date"])
    syms = list_symbols()
    print(f"Символов в архиве: {len(syms)}, качаю дневки...")
    # для символов без 1d за весь месяц будет пачка 404 — это нормально
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda s: _symbol_daily(s, start, end), syms))
    d = pd.concat([r for r in res if r is not None], ignore_index=True)
    d = d[["symbol", "t", "o", "h", "l", "c", "qv", "n"]].copy()
    d["t"] = pd.to_numeric(d["t"])
    d.loc[d["t"] > 1e14, "t"] //= 1000                    # в новых файлах время в микросекундах
    d["date"] = pd.to_datetime(d["t"], unit="ms", utc=True).dt.normalize()
    d = d[(d["date"] >= start) & (d["date"] < end)].drop(columns="t")
    d = d.drop_duplicates(["symbol", "date"]).sort_values(["date", "symbol"])
    cache_dir.mkdir(exist_ok=True)
    d.to_csv(path, index=False)
    return d


def daily_universe(daily: pd.DataFrame, top_trades=20, top_growth=20):
    """{дата торговли D: set(символы)} по итогам дня D-1."""
    d = daily.copy()
    d["growth"] = d["c"] / d["o"] - 1
    uni = {}
    for date, g in d.groupby("date"):
        a = set(g.nlargest(top_trades, "n")["symbol"])
        b = set(g.nlargest(top_growth, "growth")["symbol"])
        uni[date + pd.Timedelta(days=1)] = a | b
    return uni


def daily_atr_pct(daily: pd.DataFrame, n=14):
    """ATR(n)/close по дням, сдвинутый на день (доступен на день D по итогам D-1)."""
    out = {}
    for sym, g in daily.sort_values("date").groupby("symbol"):
        g = g.set_index("date")
        tr = pd.concat([g.h - g.l, (g.h - g.c.shift()).abs(), (g.l - g.c.shift()).abs()],
                       axis=1).max(axis=1)
        out[sym] = (tr.rolling(n).mean() / g.c).shift(1)
    return out
