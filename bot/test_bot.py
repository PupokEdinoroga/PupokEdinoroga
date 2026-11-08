"""Проверка логики бота на имитации биржи (без сети):  python test_bot.py"""
import os
import sys
import tempfile
import time as _time

sys.path.insert(0, os.path.dirname(__file__))
import yaml

import main as M
import pipeline
from broker import Broker
from notifier import Notifier
from state import State
from strategies import funding_arb, hour_drift, listing_short

H = 3600


class Clock:
    t = 1_800_000_000.0

    def time(self):
        return self.t


class FakeMarket:
    def __init__(self, clock):
        self.c = clock
        self.prices = {"NEWUSDT": 1.00, "OLDUSDT": 5.0}
        self.onboard = {"NEWUSDT": int((clock.t - 24.5 * H) * 1000), "OLDUSDT": int((clock.t - 400 * 24 * H) * 1000)}
        self.rate, self.next_ms, self.mark = {"OLDUSDT": 0.0001}, {"OLDUSDT": int((clock.t + H) * 1000)}, {"OLDUSDT": 5.0}
        self.spot = {"OLDUSDT": 5.0}
        self.orders = []

    def perpetuals(self):
        return {s: {"symbol": s.replace("USDT", "/USDT:USDT"), "onboard_ms": o, "status": "TRADING"}
                for s, o in self.onboard.items()}

    def tickers(self):
        return {s: {"last": p, "quote_volume": getattr(self, "vol", {}).get(s, 1e8)} for s, p in self.prices.items()}

    def premium_index(self):
        return {s: {"rate": self.rate[s], "next_ms": self.next_ms[s], "mark": self.mark[s]} for s in self.rate}

    def funding_intervals(self):
        return {}

    def spot_prices(self):
        return dict(self.spot)

    def prepare(self, symbol, leverage):
        self.orders.append(("prepare", symbol, leverage))

    def amount_for(self, symbol, notional, price):
        return round(notional / price, 3)

    def position_size(self, symbol):
        return 0.0 if symbol in getattr(self, "liquidated", set()) else 1.0

    def market_order(self, symbol, side, amount, reduce_only=False):
        sid = symbol.split("/")[0] + "USDT"
        self.orders.append((side, symbol, amount, reduce_only))
        return self.prices[sid], amount


def make(mode_listing="paper"):
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.example.yaml"), encoding="utf-8"))
    cfg["strategies"]["listing_short"]["mode"] = mode_listing
    cfg["pipeline"]["min_paper_trades"] = 3
    clock = Clock()
    for mod in (listing_short, funding_arb, hour_drift, M, pipeline):
        mod.time = clock
    import broker as B
    B.time = clock
    st = State(tempfile.mktemp(suffix=".sqlite"))
    n = Notifier(cfg, st)
    fm = FakeMarket(clock)
    ctx = M.Ctx(cfg, fm, st, Broker(fm, st, n, cfg), n)
    return cfg, clock, fm, st, ctx


def test_listing_paper():
    cfg, clock, fm, st, ctx = make()
    listing_short.step(ctx)
    op = st.open_positions("listing_short")
    assert len(op) == 1 and op[0].symbol == "NEWUSDT" and op[0].side == "short", op
    ctx.new_pass(); listing_short.step(ctx)
    assert len(st.open_positions("listing_short")) == 1, "повторный вход в тот же листинг"
    fm.prices["NEWUSDT"] = 0.90                                  # упала на 10% — шорт в плюсе
    clock.t += 72 * H; ctx.new_pass(); listing_short.step(ctx)
    cl = st.closed_positions("listing_short")
    assert len(cl) == 1 and cl[0].pnl_pct > 9, cl
    print(f"листинг (бумага): вход/выход ок, результат {cl[0].pnl_pct:+.2f}% ({cl[0].pnl_usd:+.2f}$)")


def test_listing_window_missed():
    cfg, clock, fm, st, ctx = make()
    fm.onboard["NEWUSDT"] = int((clock.t - 30 * H) * 1000)       # бот был выключен, окно 24–26 ч прошло
    listing_short.step(ctx)
    assert not st.open_positions("listing_short")
    print("пропущенное окно входа: сделку не открывает — ок")


def test_listing_live_and_risk():
    cfg, clock, fm, st, ctx = make("live")
    listing_short.step(ctx)
    assert any(o[0] == "sell" for o in fm.orders) and any(o[0] == "prepare" for o in fm.orders), fm.orders
    # лимит: 4 позиции максимум
    for i in range(6):
        sid = f"N{i}USDT"
        fm.prices[sid] = 1.0
        fm.onboard[sid] = int((clock.t - 24.2 * H) * 1000)
    ctx.new_pass(); listing_short.step(ctx)
    assert len(st.open_positions(mode="live")) == 4, len(st.open_positions(mode="live"))
    # стоп-файл
    open("STOP", "w").close()
    try:
        assert not ctx.can_open("listing_short", "live")
    finally:
        os.remove("STOP")
    # дневной лимит убытка
    for p in st.open_positions(mode="live"):
        fm.prices[p.symbol] = 2.0                               # +100% против шорта
    clock.t += 72 * H; ctx.new_pass(); listing_short.step(ctx)
    assert not ctx.can_open("listing_short", "live"), "дневной лимит убытка не сработал"
    # ликвидированная биржей позиция: бот не шлёт ордер, а записывает потерю маржи
    cfg2, clock2, fm2, st2, ctx2 = make("live")
    listing_short.step(ctx2)
    p = st2.open_positions(mode="live")[0]
    fm2.liquidated = {p.extra["ccxt_symbol"]}
    n_orders = len(fm2.orders)
    clock2.t += 72 * H; ctx2.new_pass(); listing_short.step(ctx2)
    c = st2.closed_positions(mode="live")[0]
    assert c.reason == "ликвидация" and len(fm2.orders) == n_orders and abs(c.pnl_usd + 10) < 1e-6, (c, fm2.orders)
    print("живой режим: ордера, максимум позиций, STOP-файл, дневной лимит, ликвидация — ок")


def test_funding_paper():
    cfg, clock, fm, st, ctx = make()
    cfg["strategies"]["listing_short"]["mode"] = "off"
    funding_arb.step(ctx)                                       # запомнили время следующего начисления
    fm.rate["OLDUSDT"] = 0.001; fm.next_ms["OLDUSDT"] += 8 * H * 1000; clock.t += H + 1
    ctx.new_pass(); funding_arb.step(ctx)                       # начисление 0,1% → вход
    assert len(st.open_positions("funding_arb")) == 1
    fm.rate["OLDUSDT"] = 0.0008; fm.next_ms["OLDUSDT"] += 8 * H * 1000; clock.t += 8 * H
    ctx.new_pass(); funding_arb.step(ctx)                       # получили 0,08%
    fm.rate["OLDUSDT"] = 0.00005; fm.next_ms["OLDUSDT"] += 8 * H * 1000; clock.t += 8 * H
    fm.mark["OLDUSDT"] = 5.10; fm.spot["OLDUSDT"] = 5.10        # цена выросла — пара нейтральна
    ctx.new_pass(); funding_arb.step(ctx)
    cl = st.closed_positions("funding_arb")
    assert len(cl) == 1, cl
    print(f"фандинг (бумага): вход/накопление/выход ок, результат {cl[0].pnl_pct:+.3f}% "
          f"(фандинг 0,085% − издержки ≈0,5% + базис)")


def test_pipeline():
    cfg, clock, fm, st, ctx = make()
    from state import Position
    for pnl in (2.0, 1.5, 2.5, 1.8):
        pid = st.add_position(Position(0, "listing_short", "paper", "X", "short", 1, 20, 2, 1, clock.t))
        st.close_position(pid, 1, clock.t, pnl / 100 * 20, pnl, "время")
    msgs = []
    ctx.notifier.send = msgs.append
    pipeline.review(ctx)
    assert any("сдала экзамен" in m for m in msgs), msgs
    # автоотключение живой
    cfg["strategies"]["listing_short"]["mode"] = "live"
    cfg["pipeline"]["demote_last_n"] = 3
    for pnl in (-3.0, -2.0, -4.0):
        pid = st.add_position(Position(0, "listing_short", "live", "Y", "short", 1, 20, 2, 1, clock.t))
        st.close_position(pid, 1, clock.t, pnl / 100 * 20, pnl, "время")
    pipeline.review(ctx)
    assert ctx.mode("listing_short") == "paper", ctx.mode("listing_short")
    pipeline.daily_report(ctx)
    print("конвейер: экзамен, автоотключение, отчёт — ок")


def test_listing_50m_variant():
    cfg, clock, fm, st, ctx = make()
    fm.vol = {"NEWUSDT": 2e7}                                    # 20 млн $ — ниже порога варианта 50m
    listing_short.step(ctx, "listing_short")
    listing_short.step(ctx, "listing_short_50m")
    assert len(st.open_positions("listing_short")) == 1 and len(st.open_positions("listing_short_50m")) == 0
    print("вариант listing_short_50m: фильтр оборота работает, экземпляры независимы — ок")


def test_hour_drift():
    from datetime import datetime, timezone
    cfg, clock, fm, st, ctx = make()
    for i in range(8):
        fm.prices[f"A{i}USDT"] = 1.0; fm.onboard[f"A{i}USDT"] = 1
    fm.vol = {f"A{i}USDT": (i + 1) * 1e8 for i in range(8)}
    fm.vol["NEWUSDT"] = fm.vol["OLDUSDT"] = 1e7
    clock.t = datetime(2026, 11, 9, 16, 1, tzinfo=timezone.utc).timestamp()
    hour_drift.step(ctx)
    op = st.open_positions("hour_drift")
    assert len(op) == 5 and {p.symbol for p in op} == {f"A{i}USDT" for i in range(3, 8)}, [p.symbol for p in op]
    ctx.new_pass(); hour_drift.step(ctx)
    assert len(st.open_positions("hour_drift")) == 5, "повторный вход в тот же час"
    for i in range(8):
        fm.prices[f"A{i}USDT"] = 1.003
    clock.t += 3600; ctx.new_pass(); hour_drift.step(ctx)
    cl = st.closed_positions("hour_drift")
    assert len(cl) == 5 and not st.open_positions("hour_drift")
    print(f"hour_drift: корзина 5 самых оборотных в 16:00 UTC, выход через час — ок ({cl[0].pnl_pct:+.2f}% на +0,3% хода)")


def test_promote_and_reset():
    cfg, clock, fm, st, ctx = make()
    st.set("_pipeline", "mode:listing_short", "live")
    assert ctx.mode("listing_short") == "live"
    st.set("_pipeline", "mode:hour_drift", "live")
    assert ctx.mode("hour_drift") == "paper", "бумажная стратегия не должна уйти в деньги"
    print("перевод в деньги через конвейер и защита paper-only стратегий — ок")


if __name__ == "__main__":
    os.chdir(tempfile.mkdtemp())
    test_listing_paper()
    test_listing_window_missed()
    test_listing_live_and_risk()
    test_funding_paper()
    test_pipeline()
    test_listing_50m_variant()
    test_hour_drift()
    test_promote_and_reset()
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
