#!/usr/bin/env python3
"""
Регистратор стакана и ленты Binance USDT-M — данные для проверки скальпинга по плотностям.

Что пишет (папка recorder.data_dir/ГГГГ-ММ-ДД/):
  {SYM}_trades.csv.gz   — каждая сделка: ts_ms, price, qty, buyer_maker (1 = агрессор продавец)
  {SYM}_top.jsonl.gz    — верх стакана 20×20 раз в top_every_s секунд: {"t":ms,"b":[[p,q]..],"a":[[p,q]..]}
  {SYM}_book.jsonl.gz   — глубокий снимок ±depth_pct% раз в snapshot_every_s секунд
  anomalies.csv.gz      — аномальные заявки (≥ anomaly_mult × медианы уровня в пределах ±depth_pct%)
                          и «переставляши» (крупная заявка сдвинулась ближе к цене)
Монеты: каждые reselect_minutes — топ по |изменению за 24 ч| и топ по числу сделок (оборот ≥ min_quote_volume).

  python recorder.py                # работает постоянно (на VPS — отдельной службой, см. README_BOT.md)
"""
import gzip
import json
import logging
import os
import shutil
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

log = logging.getLogger("recorder")
REST = "https://fapi.binance.com"
WS = "wss://fstream.binance.com/stream?streams="
EXCLUDE = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "BTCDOMUSDT"}

DEFAULTS = {"data_dir": "ob_data", "max_symbols": 20, "top_by_change": 10, "top_by_trades": 10,
            "min_quote_volume": 20_000_000, "reselect_minutes": 30, "top_every_s": 0.5,
            "snapshot_every_s": 60, "depth_pct": 3.0, "anomaly_mult": 10.0, "keep_days": 21}


# ---------------------------------------------------------------- чистые функции (покрыты тестами)
def select_symbols(tickers, cfg):
    """tickers: список словарей /fapi/v1/ticker/24hr. Возвращает список символов для записи."""
    ok = [t for t in tickers if t["symbol"].endswith("USDT") and t["symbol"] not in EXCLUDE
          and float(t["quoteVolume"]) >= cfg["min_quote_volume"]]
    by_change = sorted(ok, key=lambda t: abs(float(t["priceChangePercent"])), reverse=True)[:cfg["top_by_change"]]
    by_count = sorted(ok, key=lambda t: int(t["count"]), reverse=True)[:cfg["top_by_trades"]]
    out = []
    for t in by_change + by_count:
        if t["symbol"] not in out:
            out.append(t["symbol"])
    return out[:cfg["max_symbols"]]


def find_anomalies(bids, asks, depth_pct, mult):
    """bids/asks: [[price, qty], ...]. Уровни в пределах ±depth_pct% от середины с объёмом ≥ mult × медианы.
    Возвращает список {side, price, notional, ratio, dist_pct}."""
    if not bids or not asks:
        return []
    mid = (float(bids[0][0]) + float(asks[0][0])) / 2
    levels = []
    for side, book in (("bid", bids), ("ask", asks)):
        for p, q in book:
            p, q = float(p), float(q)
            dist = abs(p / mid - 1) * 100
            if dist <= depth_pct:
                levels.append((side, p, p * q, dist))
    if len(levels) < 10:
        return []
    med = statistics.median(n for _, _, n, _ in levels)
    if med <= 0:
        return []
    return [{"side": s, "price": p, "notional": round(n, 2), "ratio": round(n / med, 1), "dist_pct": round(d, 3)}
            for s, p, n, d in levels if n >= mult * med]


def moved_closer(prev, cur, tol=0.3):
    """«Переставляш»: та же сторона, похожий объём (±tol), стала ближе к цене. Возвращает пары (было, стало)."""
    out = []
    for c in cur:
        for p in prev:
            if p["side"] == c["side"] and abs(c["notional"] / p["notional"] - 1) <= tol \
                    and c["dist_pct"] < p["dist_pct"] - 0.05 and c["price"] != p["price"]:
                out.append((p, c))
                break
    return out


# ---------------------------------------------------------------- запись
class Writer:
    """Дописывает строки в gzip-файлы по дням (gzip из нескольких частей читается целиком)."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.buf = {}

    def add(self, name, line):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            self.buf.setdefault((day, name), []).append(line)

    def flush(self):
        with self.lock:
            buf, self.buf = self.buf, {}
        for (day, name), lines in buf.items():
            d = self.root / day
            d.mkdir(parents=True, exist_ok=True)
            with gzip.open(d / name, "at", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    def cleanup(self, keep_days):
        if not self.root.exists():
            return
        days = sorted(p for p in self.root.iterdir() if p.is_dir())
        for p in days[:-keep_days] if len(days) > keep_days else []:
            shutil.rmtree(p, ignore_errors=True)


class Recorder:
    def __init__(self, cfg, notifier=None):
        self.cfg = cfg
        self.w = Writer(cfg["data_dir"])
        self.symbols = []
        self.ws = None
        self.last_top = {}
        self.prev_anom = {}
        self.counts = {"trades": 0, "top": 0, "book": 0, "anom": 0, "moved": 0}
        self.notify = notifier or (lambda text: log.info(text))
        self.stop = False

    # --- WebSocket: сделки и верх стакана
    def _on_message(self, _, msg):
        m = json.loads(msg)
        stream, d = m.get("stream", ""), m.get("data", {})
        sym = stream.split("@")[0].upper()
        if "@aggTrade" in stream:
            self.w.add(f"{sym}_trades.csv.gz", f"{d['T']},{d['p']},{d['q']},{int(d['m'])}")
            self.counts["trades"] += 1
        elif "@depth20" in stream:
            now = time.time()
            if now - self.last_top.get(sym, 0) >= self.cfg["top_every_s"]:
                self.last_top[sym] = now
                self.w.add(f"{sym}_top.jsonl.gz", json.dumps({"t": d.get("E", int(now * 1000)),
                                                              "b": d["b"], "a": d["a"]}, separators=(",", ":")))
                self.counts["top"] += 1

    def _run_ws(self, symbols):
        import websocket
        streams = "/".join(f"{s.lower()}@aggTrade/{s.lower()}@depth20@500ms" for s in symbols)
        self.ws = websocket.WebSocketApp(WS + streams, on_message=self._on_message,
                                         on_error=lambda _, e: log.warning(f"ws ошибка: {e}"))
        self.ws.run_forever(ping_interval=60, ping_timeout=20)

    def _ws_loop(self):
        while not self.stop:
            syms = list(self.symbols)
            if not syms:
                time.sleep(5); continue
            log.info(f"ws подключение: {len(syms)} монет")
            self._run_ws(syms)                      # возвращается при обрыве или при смене списка
            time.sleep(3)

    # --- REST: выбор монет и глубокие снимки
    def reselect(self):
        r = requests.get(f"{REST}/fapi/v1/ticker/24hr", timeout=20)
        r.raise_for_status()
        new = select_symbols(r.json(), self.cfg)
        if new != self.symbols:
            self.symbols = new
            self.notify(f"📼 Регистратор: пишу {len(new)} монет: {', '.join(new)}")
            if self.ws:
                self.ws.close()                     # переподключится с новым списком

    def snapshot(self, sym):
        r = requests.get(f"{REST}/fapi/v1/depth", params={"symbol": sym, "limit": 1000}, timeout=20)
        r.raise_for_status()
        d = r.json()
        bids, asks = d["bids"], d["asks"]
        if not bids or not asks:
            return
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        lo, hi = mid * (1 - self.cfg["depth_pct"] / 100), mid * (1 + self.cfg["depth_pct"] / 100)
        b = [x for x in bids if float(x[0]) >= lo]
        a = [x for x in asks if float(x[0]) <= hi]
        ts = int(time.time() * 1000)
        self.w.add(f"{sym}_book.jsonl.gz", json.dumps({"t": ts, "b": b, "a": a}, separators=(",", ":")))
        self.counts["book"] += 1
        an = find_anomalies(b, a, self.cfg["depth_pct"], self.cfg["anomaly_mult"])
        for x in an:
            self.w.add("anomalies.csv.gz", f"{ts},{sym},anomaly,{x['side']},{x['price']},{x['notional']},"
                                           f"{x['ratio']},{x['dist_pct']}")
        for p, c in moved_closer(self.prev_anom.get(sym, []), an):
            self.w.add("anomalies.csv.gz", f"{ts},{sym},moved,{c['side']},{c['price']},{c['notional']},"
                                           f"{c['ratio']},{c['dist_pct']}")
            self.counts["moved"] += 1
        self.counts["anom"] += len(an)
        self.prev_anom[sym] = an

    def run(self):
        threading.Thread(target=self._ws_loop, daemon=True).start()
        last_sel = last_snap = last_flush = last_report = 0
        day = None
        while not self.stop:
            now = time.time()
            try:
                if now - last_sel >= self.cfg["reselect_minutes"] * 60:
                    self.reselect(); last_sel = now
                if now - last_snap >= self.cfg["snapshot_every_s"]:
                    for s in list(self.symbols):
                        self.snapshot(s)
                        time.sleep(0.2)
                    last_snap = now
                if now - last_flush >= 10:
                    self.w.flush(); last_flush = now
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if today != day:
                    if day is not None:
                        self.notify(f"📼 Регистратор за {day}: сделок {self.counts['trades']}, снимков верха "
                                    f"{self.counts['top']}, глубоких {self.counts['book']}, аномальных заявок "
                                    f"{self.counts['anom']}, переставляшей {self.counts['moved']}")
                        self.counts = dict.fromkeys(self.counts, 0)
                        self.w.cleanup(self.cfg["keep_days"])
                    day = today
            except Exception as e:
                log.warning(f"ошибка: {e}")
                if now - last_report > 1800:
                    self.notify(f"⚠️ Регистратор: {type(e).__name__}: {str(e)[:200]}")
                    last_report = now
            time.sleep(1)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("recorder.log", encoding="utf-8")])
    path = os.environ.get("BOT_CONFIG", "config.yaml")
    with open(path, encoding="utf-8") as f:
        full = yaml.safe_load(f)
    cfg = dict(DEFAULTS, **(full.get("recorder") or {}))
    from notifier import Notifier
    n = Notifier(full)
    Recorder(cfg, n.send).run()


if __name__ == "__main__":
    main()
