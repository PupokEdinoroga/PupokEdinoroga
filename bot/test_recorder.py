"""Проверка регистратора и анализатора без сети:  python test_recorder.py"""
import gzip
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import analyze_ob as A
import recorder as R


def test_select():
    tk = [{"symbol": f"C{i}USDT", "quoteVolume": str(1e8), "priceChangePercent": str(i), "count": str(1000 - i)}
          for i in range(30)]
    tk += [{"symbol": "BTCUSDT", "quoteVolume": "1e10", "priceChangePercent": "99", "count": "999999"},
           {"symbol": "DEADUSDT", "quoteVolume": "1e5", "priceChangePercent": "90", "count": "5"}]
    s = R.select_symbols(tk, R.DEFAULTS)
    assert "BTCUSDT" not in s and "DEADUSDT" not in s and len(s) == 20
    assert s[0] == "C29USDT" and "C0USDT" in s, s                 # топ по изменению + топ по сделкам
    print("выбор монет: топ по изменению и по сделкам, без BTC и неликвида — ок")


def test_anomalies_and_replacer():
    bids = [[str(100 - i * 0.1), "10"] for i in range(20)]
    asks = [[str(100.1 + i * 0.1), "10"] for i in range(20)]
    bids[5][1] = "200"                                            # 20× медианы
    an = R.find_anomalies(bids, asks, 3.0, 10.0)
    assert len(an) == 1 and an[0]["side"] == "bid" and an[0]["ratio"] >= 19, an
    bids2 = [list(x) for x in bids]
    bids2[5][1] = "10"; bids2[2][1] = "205"                       # та же заявка ближе к цене
    an2 = R.find_anomalies(bids2, asks, 3.0, 10.0)
    mv = R.moved_closer(an, an2)
    assert len(mv) == 1 and mv[0][1]["dist_pct"] < mv[0][0]["dist_pct"], mv
    print("аномальные заявки (≥10× медианы) и «переставляш» — ок")


def test_writer_and_analyzer():
    root = Path(tempfile.mkdtemp())
    w = R.Writer(root)
    day = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    t0 = 1_800_000_000_000
    # сценарий 1: бид-стена на 100.0, цена подходит к 100.05, отскакивает до 100.6 → тейк
    trades = [(t0 + i * 1000, 100.5 - i * 0.05) for i in range(10)]          # до 100.05
    trades += [(t0 + 10_000 + i * 1000, 100.05 + i * 0.1) for i in range(10)]  # вверх до 100.95
    # сценарий 2: аск-стена 200.0, цена 199.85, стену съедают — цена 200.4
    trades2 = [(t0 + i * 1000, 199.7 + i * 0.05) for i in range(15)]
    for t, p in trades:
        w.add("AAAUSDT_trades.csv.gz", f"{t},{p},1,0")
    for t, p in trades2:
        w.add("BBBUSDT_trades.csv.gz", f"{t},{p},1,0")
    w.add("anomalies.csv.gz", f"{t0},AAAUSDT,anomaly,bid,100.0,50000,25,0.5")
    w.add("anomalies.csv.gz", f"{t0},BBBUSDT,anomaly,ask,200.0,50000,12,0.15")
    w.add("anomalies.csv.gz", f"{t0 + 60_000},AAAUSDT,anomaly,bid,100.0,50000,25,0.5")    # дубль — пропуск
    w.flush()
    assert (root / day / "anomalies.csv.gz").exists()
    res = A.run(root)
    wb = res["wall_bounce"]
    assert len(wb) == 2, wb
    a = [r for r in wb if r["sym"] == "AAAUSDT"][0]
    b = [r for r in wb if r["sym"] == "BBBUSDT"][0]
    assert a["why"] == "тейк" and a["pnl"] > 0, a
    assert b["why"] in ("съели", "проскок") and b["pnl"] < 0, b
    w.cleanup(0)
    print("запись gzip по дням, анализатор (тейк от стены, выход при съедании, дубли) — ок")


def test_ws_messages():
    import json
    root = Path(tempfile.mkdtemp())
    cfg = dict(R.DEFAULTS, data_dir=str(root), top_every_s=0)
    rec = R.Recorder(cfg)
    rec._on_message(None, json.dumps({"stream": "solusdt@aggTrade",
                                      "data": {"T": 1, "p": "150.1", "q": "2", "m": True}}))
    rec._on_message(None, json.dumps({"stream": "solusdt@depth20@500ms",
                                      "data": {"E": 2, "b": [["150", "1"]], "a": [["150.2", "1"]]}}))
    rec.w.flush()
    day = next(root.iterdir())
    tr = gzip.open(day / "SOLUSDT_trades.csv.gz", "rt").read().strip()
    top = gzip.open(day / "SOLUSDT_top.jsonl.gz", "rt").read().strip()
    assert tr == "1,150.1,2,1" and '"b":[["150","1"]]' in top, (tr, top)
    print("разбор сообщений WebSocket (сделки, верх стакана) — ок")


if __name__ == "__main__":
    test_select()
    test_anomalies_and_replacer()
    test_writer_and_analyzer()
    test_ws_messages()
    print("ВСЕ ПРОВЕРКИ РЕГИСТРАТОРА ПРОЙДЕНЫ")
