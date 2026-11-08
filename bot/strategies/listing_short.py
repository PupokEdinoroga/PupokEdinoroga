"""Шорт новых листингов (прошла контроль 2026-11-07: 44 листинга, +2,25%/сделку, t≈1,6).

Правила как в бэктесте: через enter_after_hours после листинга перпа — шорт, держим hold_hours, выход по рынку.
Стопа нет (в бэктесте его не было); риск одной сделки ограничен изолированной маржой notional/leverage.
"""
import time

NAME = "listing_short"


def step(ctx, NAME=NAME):
    cfg = ctx.cfg["strategies"][NAME]
    mode = ctx.mode(NAME)
    if mode == "off":
        return
    now = time.time()

    # 1) выходы по времени
    for p in ctx.state.open_positions(NAME):
        if now - p.entry_time >= cfg["hold_hours"] * 3600:
            price = ctx.tickers().get(p.symbol, {}).get("last")
            if price:
                ctx.broker.close(p, price, "время")
            elif p.mode == "paper":                    # монету делистнули — закрываем по последней известной
                ctx.broker.close(p, p.extra.get("last_seen", p.entry_price), "нет цены")

    # 2) входы
    lo = cfg["enter_after_hours"] * 3600 * 1000
    hi = lo + cfg["entry_window_hours"] * 3600 * 1000
    now_ms = int(now * 1000)
    for sid, m in ctx.perpetuals().items():
        if m["status"] != "TRADING" or not m["onboard_ms"]:
            continue
        age = now_ms - m["onboard_ms"]
        if not (lo <= age < hi) or ctx.state.get(NAME, f"done:{sid}"):
            continue
        ctx.state.set(NAME, f"done:{sid}", True)       # одна попытка на листинг
        t = ctx.tickers().get(sid)
        if not t or t["quote_volume"] < cfg.get("min_quote_volume_24h", 0):
            ctx.notify(f"{NAME}: пропуск {sid} (мало оборота или нет цены)")
            continue
        if not ctx.can_open(NAME, mode):
            ctx.notify(f"{NAME}: пропуск {sid} — лимит риска/позиций")
            continue
        ctx.broker.open(NAME, mode, sid, m["symbol"], "short", cfg["notional_usd"], cfg["leverage"], t["last"])

    # запоминаем последние цены открытых (на случай делистинга)
    tk = ctx.tickers()
    for p in ctx.state.open_positions(NAME):
        if p.symbol in tk:
            p.extra["last_seen"] = tk[p.symbol]["last"]
            ctx.state.update_extra(p.id, p.extra)
