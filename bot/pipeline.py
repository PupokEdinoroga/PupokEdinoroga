"""Конвейер «бумага → экзамен → деньги» и автоотключение живых стратегий."""
import math
import time


def stats(positions):
    v = [p.pnl_pct for p in positions if p.pnl_pct is not None]
    n = len(v)
    if n == 0:
        return {"n": 0, "avg": 0.0, "t": 0.0, "sum_usd": 0.0, "win": 0.0}
    avg = sum(v) / n
    sd = math.sqrt(sum((x - avg) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "avg": avg, "t": avg / sd * math.sqrt(n) if sd > 0 else 0.0,
            "sum_usd": sum(p.pnl_usd or 0 for p in positions), "win": 100 * sum(x > 0 for x in v) / n}


def review(ctx):
    """Раз в сутки: экзамен для бумажных стратегий и проверка живых."""
    pc = ctx.cfg["pipeline"]
    for name, scfg in ctx.cfg["strategies"].items():
        mode = ctx.mode(name)
        if mode == "paper":
            st = stats(ctx.state.closed_positions(name, "paper"))
            if st["n"] >= pc["min_paper_trades"] and st["t"] >= pc["min_t_stat"] and st["avg"] >= pc["min_avg_pnl_pct"]:
                if name == "funding_arb":
                    ctx.notify(f"🎓 {name} сдала экзамен на бумаге ({st['n']} сделок, {st['avg']:+.2f}%, t={st['t']:.1f}), "
                               f"но живой режим для неё не реализован")
                elif pc.get("auto_promote"):
                    ctx.state.set("_pipeline", f"mode:{name}", "live")
                    ctx.notify(f"🎓 {name} сдала экзамен ({st['n']} сделок, {st['avg']:+.2f}%, t={st['t']:.1f}) — ПЕРЕВЕДЕНА В ДЕНЬГИ")
                else:
                    ctx.notify(f"🎓 {name} сдала экзамен ({st['n']} сделок, {st['avg']:+.2f}%, t={st['t']:.1f}). "
                               f"Чтобы торговать деньгами: mode: live в config.yaml")
        elif mode == "live":
            last = ctx.state.closed_positions(name, "live")[-pc["demote_last_n"]:]
            st = stats(last)
            if st["n"] >= pc["demote_last_n"] and st["avg"] < pc["demote_avg_pnl_pct"]:
                ctx.state.set("_pipeline", f"mode:{name}", "paper")
                ctx.notify(f"⛔ {name} сломалась: последние {st['n']} живых сделок в среднем {st['avg']:+.2f}% — "
                           f"обратно на бумагу")


def daily_report(ctx):
    lines = ["📊 Отчёт за сутки"]
    day_ago = time.time() - 86400
    for name in ctx.cfg["strategies"]:
        mode = ctx.mode(name)
        allp = ctx.state.closed_positions(name)
        st_all, st_day = stats(allp), stats([p for p in allp if p.exit_time >= day_ago])
        lines.append(f"• {name} [{mode}]: открыто {len(ctx.state.open_positions(name))}, за сутки "
                     f"{st_day['n']} сделок {st_day['sum_usd']:+.2f}$; всего {st_all['n']} сделок, "
                     f"ср {st_all['avg']:+.2f}%, {st_all['sum_usd']:+.2f}$, в плюсе {st_all['win']:.0f}%")
    live = stats(ctx.state.closed_positions(mode="live"))
    lines.append(f"Реальные деньги всего: {live['sum_usd']:+.2f}$ ({live['n']} сделок)")
    ctx.notify("\n".join(lines))
