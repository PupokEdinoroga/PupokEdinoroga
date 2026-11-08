"""Доступ к бирже через ccxt: рыночные данные и (в живом режиме) ордера Binance USDT-M."""
import time

import ccxt


class Market:
    def __init__(self, cfg):
        ex = cfg["exchange"]
        self.fut = getattr(ccxt, ex.get("name", "binanceusdm"))({
            "apiKey": ex.get("api_key") or None, "secret": ex.get("api_secret") or None,
            "enableRateLimit": True, "options": {"defaultType": "future"}})
        self.spot = ccxt.binance({"enableRateLimit": True})
        self._markets_ts = 0
        self._spot_symbols = None

    # ---------------------------------------------------------------- данные
    def perpetuals(self):
        """USDT-перпы: {symbol_id: {"symbol": unified, "onboard_ms": int, "status": str}}. Кэш 10 мин."""
        if time.time() - self._markets_ts > 600:
            self.fut.load_markets(reload=True)
            self._markets_ts = time.time()
        out = {}
        for m in self.fut.markets.values():
            info = m.get("info", {})
            if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" \
                    and info.get("contractType") == "PERPETUAL":
                out[m["id"]] = {"symbol": m["symbol"], "onboard_ms": int(info.get("onboardDate") or 0),
                                "status": info.get("status")}
        return out

    def tickers(self):
        """{symbol_id: {"last": float, "quote_volume": float}} по всем перпам."""
        raw = self.fut.fapiPublicGetTicker24hr()
        return {r["symbol"]: {"last": float(r["lastPrice"]), "quote_volume": float(r["quoteVolume"])} for r in raw}

    def premium_index(self):
        """{symbol_id: {"rate": последняя ставка, "next_ms": время следующего начисления, "mark": цена}}."""
        raw = self.fut.fapiPublicGetPremiumIndex()
        return {r["symbol"]: {"rate": float(r["lastFundingRate"] or 0), "next_ms": int(r["nextFundingTime"] or 0),
                              "mark": float(r["markPrice"])} for r in raw}

    def funding_intervals(self):
        """{symbol_id: часы между начислениями}; у кого нет записи — 8."""
        try:
            return {r["symbol"]: int(r["fundingIntervalHours"]) for r in self.fut.fapiPublicGetFundingInfo()}
        except Exception:
            return {}

    def spot_prices(self):
        raw = self.spot.publicGetTickerPrice()
        return {r["symbol"]: float(r["price"]) for r in raw}

    # ---------------------------------------------------------------- ордера (только live)
    def prepare(self, symbol, leverage):
        try:
            self.fut.set_margin_mode("isolated", symbol)
        except Exception as e:                      # «уже isolated» — не ошибка
            if "No need to change" not in str(e):
                raise
        self.fut.set_leverage(int(leverage), symbol)

    def amount_for(self, symbol, notional, price):
        return float(self.fut.amount_to_precision(symbol, notional / price))

    def market_order(self, symbol, side, amount, reduce_only=False):
        params = {"reduceOnly": True} if reduce_only else {}
        o = self.fut.create_order(symbol, "market", side, amount, None, params)
        price = o.get("average") or o.get("price")
        if not price:
            o = self.fut.fetch_order(o["id"], symbol)
            price = o.get("average") or o.get("price")
        return float(price), float(o.get("filled") or amount)

    def position_size(self, symbol):
        """Текущий размер позиции на бирже (0 — позиции нет, например её ликвидировали)."""
        for pos in self.fut.fetch_positions([symbol]):
            if pos.get("symbol") == symbol:
                return abs(float(pos.get("contracts") or 0))
        return 0.0
