"""Арбитраж фандинга — ТОЛЬКО бумага: лонг спот + шорт перп (прошла контроль: 14 сделок, +1,7%/сделку по рынку).

После каждого начисления: если ставка (в пересчёте на 8 ч) ≥ entry_rate_8h и есть спот — открываем пару;
держим, пока очередная ставка ≥ exit_rate_8h; полученный фандинг копим. Итог = фандинг + базис − издержки 4 сделок.
"""
import time

NAME = "funding_arb"


def step(ctx):
    cfg = ctx.cfg["strategies"][NAME]
    if ctx.mode(NAME) == "off":
        return
    mode = "paper"                                       # реальный режим для этой стратегии не реализован
    pi = ctx.premium_index()
    last_next = ctx.state.get(NAME, "next_ms", {})
    intervals = ctx.funding_intervals()
    spot = None
    settled = []
    for sid, r in pi.items():                            # у каких монет только что прошло начисление
        prev = last_next.get(sid)
        if prev and r["next_ms"] > prev and int(time.time() * 1000) >= prev:
            settled.append(sid)
        last_next[sid] = r["next_ms"]
    ctx.state.set(NAME, "next_ms", last_next)
    if not settled:
        return
    # 4 рыночные сделки: перп (комиссия+проскальзывание) ×2 + спот (0,1% комиссия + проскальзывание) ×2 ≈ 0,5%
    fee_round = 2 * (ctx.cfg["costs"]["fee"] + ctx.cfg["costs"]["slippage"]) + 2 * (0.001 + ctx.cfg["costs"]["slippage"])

    open_by_sym = {p.symbol: p for p in ctx.state.open_positions(NAME)}
    for sid in settled:
        rate = pi[sid]["rate"]
        r8 = rate * 8 / intervals.get(sid, 8)
        p = open_by_sym.get(sid)
        if p:                                            # начисление по открытой паре
            p.extra["funding"] = p.extra.get("funding", 0.0) + rate
            ctx.state.update_extra(p.id, p.extra)
            if r8 < cfg["exit_rate_8h"]:
                spot = spot or ctx.spot_prices()
                sp = spot.get(sid)
                if not sp:
                    continue
                spot_leg = sp / p.extra["spot_entry"] - 1
                perp_leg = 1 - pi[sid]["mark"] / p.extra["perp_entry"]
                pnl = p.extra["funding"] + spot_leg + perp_leg - fee_round
                ctx.broker.close_with_pnl(p, pnl, f"фандинг < {cfg['exit_rate_8h']*100:.2f}%", extra=p.extra)
            continue
        if r8 < cfg["entry_rate_8h"]:
            continue
        t = ctx.tickers().get(sid)
        if not t or t["quote_volume"] < cfg.get("min_quote_volume_24h", 0):
            continue
        spot = spot or ctx.spot_prices()
        if sid not in spot:
            continue
        ctx.broker.open(NAME, mode, sid, sid, "short", cfg["notional_usd"], 1, pi[sid]["mark"],
                        extra={"spot_entry": spot[sid], "perp_entry": pi[sid]["mark"], "funding": 0.0})
