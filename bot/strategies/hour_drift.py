"""Часы после смены 4-часовой свечи — ТОЛЬКО бумага.

Бэктест (разработка): равновзвешенный индекс альтов растёт в часы 00 UTC (+0,16%, t=3,4) и 16 UTC (+0,12%, t=2,8);
это меньше издержек по рынку (~0,2%), поэтому только проверка на бумаге.
В начале часа из списка hours: лонг корзины из basket_size самых оборотных альтов (оборот ≥ min_quote_volume_24h,
без BTC/ETH), держим hold_minutes, выход по рынку.
"""
import time
from datetime import datetime, timezone

NAME = "hour_drift"
EXCLUDE = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "BTCDOMUSDT"}


def step(ctx, NAME=NAME):
    cfg = ctx.cfg["strategies"][NAME]
    if ctx.mode(NAME) == "off":
        return
    now = time.time()
    for p in ctx.state.open_positions(NAME):
        if now - p.entry_time >= cfg["hold_minutes"] * 60:
            price = ctx.tickers().get(p.symbol, {}).get("last")
            if price:
                ctx.broker.close(p, price, "время")
    dt = datetime.fromtimestamp(now, timezone.utc)
    if dt.hour not in cfg["hours"] or dt.minute >= cfg.get("entry_window_minutes", 5):
        return
    slot = dt.strftime("%Y-%m-%d %H")
    if ctx.state.get(NAME, "last_slot") == slot:
        return
    ctx.state.set(NAME, "last_slot", slot)
    if not ctx.can_open(NAME, "paper"):
        return
    perps = ctx.perpetuals()
    tk = ctx.tickers()
    cands = [(t["quote_volume"], sid) for sid, t in tk.items()
             if sid in perps and perps[sid]["status"] == "TRADING" and sid not in EXCLUDE
             and t["quote_volume"] >= cfg["min_quote_volume_24h"]]
    for _, sid in sorted(cands, reverse=True)[:cfg["basket_size"]]:
        ctx.broker.open(NAME, "paper", sid, perps[sid]["symbol"], "long", cfg["notional_usd"], 1, tk[sid]["last"])
