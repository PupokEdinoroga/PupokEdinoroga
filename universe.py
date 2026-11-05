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
