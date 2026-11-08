"""Исполнение: бумажное (по текущей цене с издержками как в бэктесте) или реальное (рыночные ордера)."""
import time

from state import Position


class Broker:
    def __init__(self, market, state, notifier, cfg):
        self.m, self.s, self.n, self.cfg = market, state, notifier, cfg
        self.fee = cfg["costs"]["fee"]
        self.slip = cfg["costs"]["slippage"]

    def open(self, strategy, mode, symbol_id, symbol, side, notional, leverage, price, extra=None):
        """side: long | short. price — текущая цена (для бумаги)."""
        d = 1 if side == "long" else -1
        if mode == "live":
            self.m.prepare(symbol, leverage)
            amount = self.m.amount_for(symbol, notional, price)
            fill, qty = self.m.market_order(symbol, "buy" if d > 0 else "sell", amount)
        else:
            fill = price * (1 + d * self.slip)
            qty = notional / fill
        p = Position(0, strategy, mode, symbol_id, side, qty, qty * fill, leverage, fill, time.time(),
                     extra=dict(extra or {}, ccxt_symbol=symbol))
        p.id = self.s.add_position(p)
        self.n.send(f"{'🟢' if mode == 'live' else '📄'} {strategy}: {side.upper()} {symbol_id} "
                    f"на {p.notional:.2f}$ по {fill:.6g} ({mode})")
        return p

    def close(self, p: Position, price, reason, extra_pnl_pct=0.0, extra=None):
        """extra_pnl_pct — добавка к результату в долях (например, фандинг, нога спота)."""
        d = 1 if p.side == "long" else -1
        if p.mode == "live":
            if self.m.position_size(p.extra["ccxt_symbol"]) == 0:
                # позиции на бирже уже нет — её ликвидировали: потеряна изолированная маржа
                loss_pct = -1.0 / max(p.leverage, 1)
                self.s.close_position(p.id, None, time.time(), loss_pct * p.notional, loss_pct * 100, "ликвидация")
                self.n.send(f"💥 {p.strategy}: {p.symbol} ликвидирована биржей, потеря ≈{loss_pct * p.notional:+.2f}$")
                return loss_pct * p.notional
            fill, _ = self.m.market_order(p.extra["ccxt_symbol"], "sell" if d > 0 else "buy", p.qty,
                                          reduce_only=True)
        else:
            fill = price * (1 - d * self.slip)
        pnl_pct = d * (fill / p.entry_price - 1) - 2 * self.fee + extra_pnl_pct
        pnl_usd = pnl_pct * p.notional
        self.s.close_position(p.id, fill, time.time(), pnl_usd, pnl_pct * 100, reason, extra)
        icon = "✅" if pnl_usd > 0 else "🔻"
        self.n.send(f"{icon} {p.strategy}: закрыл {p.side.upper()} {p.symbol} ({reason}) "
                    f"{pnl_pct * 100:+.2f}% = {pnl_usd:+.2f}$ ({p.mode})")
        return pnl_usd

    def close_with_pnl(self, p: Position, pnl_pct, reason, extra=None):
        """Бумажное закрытие с заранее посчитанным итогом (для парных стратегий: спот + фьючерс)."""
        pnl_usd = pnl_pct * p.notional
        self.s.close_position(p.id, None, time.time(), pnl_usd, pnl_pct * 100, reason, extra)
        icon = "✅" if pnl_usd > 0 else "🔻"
        self.n.send(f"{icon} {p.strategy}: закрыл пару {p.symbol} ({reason}) {pnl_pct * 100:+.2f}% = {pnl_usd:+.2f}$ (paper)")
        return pnl_usd
