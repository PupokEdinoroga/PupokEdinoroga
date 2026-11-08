#!/usr/bin/env python3
"""
Торговый бот: конвейер стратегий «бумага → экзамен → деньги».

  python main.py                 # основной цикл (на VPS — через systemd, см. README_BOT.md)
  python main.py --once          # один проход (проверка настроек)
  python main.py --report        # прислать отчёт сейчас
"""
import argparse
import logging
import os
import time
import traceback
from datetime import datetime, timezone

import yaml

import pipeline
from broker import Broker
from market import Market
from notifier import Notifier
from state import State
from strategies import funding_arb, listing_short

STRATEGIES = {"listing_short": listing_short, "funding_arb": funding_arb}
log = logging.getLogger("bot")


class Ctx:
    """Всё, что нужно стратегиям: данные (с кэшем на один проход), брокер, риск-лимиты."""

    def __init__(self, cfg, market, state, broker, notifier):
        self.cfg, self.market, self.state, self.broker, self.notifier = cfg, market, state, broker, notifier
        self._cache = {}

    def _cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def new_pass(self):
        self._cache = {}

    def perpetuals(self):
        return self._cached("perps", self.market.perpetuals)

    def tickers(self):
        return self._cached("tickers", self.market.tickers)

    def premium_index(self):
        return self._cached("pi", self.market.premium_index)

    def funding_intervals(self):
        return self._cached("fi", self.market.funding_intervals)

    def spot_prices(self):
        return self._cached("spot", self.market.spot_prices)

    def notify(self, text):
        self.notifier.send(text)

    def mode(self, name):
        """Режим стратегии: решение конвейера (автоотключение) важнее config, но 'off' в config — всегда off."""
        base = self.cfg["strategies"][name].get("mode", "paper")
        if base == "off":
            return "off"
        override = self.state.get("_pipeline", f"mode:{name}")
        if override == "paper":
            return "paper"
        return base

    def can_open(self, name, mode):
        if mode != "live":
            return len(self.state.open_positions(mode="paper")) < 100
        r = self.cfg["risk"]
        if os.path.exists(r.get("kill_switch_file", "STOP")):
            return False
        day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        day_pnl = sum(p.pnl_usd or 0 for p in self.state.closed_positions(mode="live", since=day0))
        if day_pnl <= -abs(r["daily_loss_limit_usd"]):
            if not self.state.get("_risk", f"paused:{int(day0)}"):
                self.state.set("_risk", f"paused:{int(day0)}", True)
                self.notify(f"⏸ Дневной лимит убытка: {day_pnl:+.2f}$. Новые живые сделки — с завтрашнего дня (UTC).")
            return False
        live_open = self.state.open_positions(mode="live")
        if len(live_open) >= r["max_open_positions"]:
            return False
        used = sum(p.notional / max(p.leverage, 1) for p in live_open)
        scfg = self.cfg["strategies"][name]
        need = scfg["notional_usd"] / max(scfg.get("leverage", 1), 1)
        return used + need <= r["capital_usd"]


def run(cfg, once=False, report=False):
    state = State(cfg.get("db_path", "bot_state.sqlite"))
    notifier = Notifier(cfg, state)
    market = Market(cfg)
    ctx = Ctx(cfg, market, state, Broker(market, state, notifier, cfg), notifier)
    if report:
        pipeline.daily_report(ctx)
        return
    notifier.send("🤖 Бот запущен. Режимы: " + ", ".join(f"{n}={ctx.mode(n)}" for n in STRATEGIES))
    last_err = 0
    while True:
        ctx.new_pass()
        try:
            for name, mod in STRATEGIES.items():
                if name in cfg["strategies"]:
                    mod.step(ctx)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if datetime.now(timezone.utc).hour == 0 and state.get("_daily", "last") != today:
                state.set("_daily", "last", today)
                pipeline.review(ctx)
                pipeline.daily_report(ctx)
        except Exception as e:
            log.error(traceback.format_exc())
            if time.time() - last_err > 1800:                 # не спамим: не чаще раза в 30 мин
                notifier.send(f"⚠️ Ошибка: {type(e).__name__}: {str(e)[:300]}")
                last_err = time.time()
        if once:
            break
        time.sleep(cfg.get("loop_seconds", 60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")])
    with open(a.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    run(cfg, once=a.once, report=a.report)


if __name__ == "__main__":
    main()
