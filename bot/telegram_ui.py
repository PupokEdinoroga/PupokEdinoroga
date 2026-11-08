"""Кнопки управления ботом в Telegram. Работает в отдельном потоке, отвечает только владельцу (chat_id из config)."""
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("bot")

B_REPORT, B_POS, B_REC, B_HEALTH, B_PAUSE, B_RESUME = (
    "📊 Отчёт", "📂 Позиции", "📼 Регистратор", "🩺 Состояние", "⏸ Пауза", "▶️ Продолжить")
KEYBOARD = {"keyboard": [[{"text": B_REPORT}, {"text": B_POS}],
                         [{"text": B_REC}, {"text": B_HEALTH}],
                         [{"text": B_PAUSE}, {"text": B_RESUME}]],
            "resize_keyboard": True, "is_persistent": True}


def _ago(ts):
    if not ts:
        return "никогда"
    s = int(time.time() - ts)
    if s < 120:
        return f"{s} с назад"
    if s < 7200:
        return f"{s // 60} мин назад"
    if s < 172800:
        return f"{s // 3600} ч назад"
    return f"{s // 86400} дн назад"


class TelegramUI:
    def __init__(self, cfg, api=None, state_factory=None, market_factory=None):
        tg = cfg.get("telegram", {}) or {}
        self.cfg, self.token, self.chat = cfg, tg.get("token"), str(tg.get("chat_id") or "")
        self.api = api or self._http
        self._state_factory = state_factory
        self._market_factory = market_factory
        self._state = self._market = None
        self.offset = None

    # ---------------------------------------------------------------- транспорт
    def _http(self, method, **params):
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                          timeout=params.get("timeout", 0) + 15)
        return r.json()

    def send(self, text):
        self.api("sendMessage", chat_id=self.chat, text=text[:4000], reply_markup=KEYBOARD)

    def state(self):                               # sqlite: своё соединение в своём потоке
        if self._state is None:
            self._state = self._state_factory()
        return self._state

    def market(self):
        if self._market is None and self._market_factory:
            self._market = self._market_factory()
        return self._market

    def kill_file(self):
        return self.cfg.get("risk", {}).get("kill_switch_file", "STOP")

    # ---------------------------------------------------------------- кнопки
    def handle(self, text):
        text = (text or "").strip()
        if text in (B_REPORT, "/report"):
            return self.report()
        if text in (B_POS, "/positions"):
            return self.positions()
        if text in (B_REC, "/recorder"):
            return self.recorder()
        if text in (B_HEALTH, "/status"):
            return self.health()
        if text in (B_PAUSE, "/pause"):
            Path(self.kill_file()).touch()
            return "⏸ Пауза: новые сделки не открываются. Открытые закроются по своим правилам."
        if text in (B_RESUME, "/resume"):
            if os.path.exists(self.kill_file()):
                os.remove(self.kill_file())
            return "▶️ Продолжаем: новые сделки снова разрешены."
        return "Кнопки управления ниже 👇"

    def report(self):
        import pipeline
        out = []

        class Ctx:                                  # минимальный контекст для pipeline.daily_report
            cfg = self.cfg
            state = self.state()

            def mode(_, name):
                base = self.cfg["strategies"][name].get("mode", "paper")
                ov = self.state().get("_pipeline", f"mode:{name}")
                return "off" if base == "off" else (ov or base)

            def notify(_, text):
                out.append(text)

        pipeline.daily_report(Ctx())
        return "\n".join(out)

    def positions(self):
        ops = self.state().open_positions()
        if not ops:
            return "📂 Открытых позиций нет."
        prices = {}
        try:
            prices = {k: v["last"] for k, v in self.market().tickers().items()}
        except Exception as e:
            log.warning(f"цены для /positions: {e}")
        fee = self.cfg.get("costs", {}).get("fee", 0.0005)
        lines, total = [f"📂 Открыто: {len(ops)}"], 0.0
        for p in ops:
            d = 1 if p.side == "long" else -1
            last = prices.get(p.symbol)
            if last and p.strategy != "funding_arb":
                pnl = d * (last / p.entry_price - 1) - 2 * fee
                total += pnl * p.notional
                res = f"{pnl * 100:+.2f}% ({pnl * p.notional:+.2f}$)"
            else:
                res = "—"
            lines.append(f"• {p.strategy} [{p.mode}] {p.side.upper()} {p.symbol} {p.notional:.0f}$ "
                         f"вход {p.entry_price:.6g}, {_ago(p.entry_time)}: {res}")
        if prices:
            lines.append(f"Итого по текущим ценам: {total:+.2f}$")
        return "\n".join(lines)

    def recorder(self):
        root = Path((self.cfg.get("recorder") or {}).get("data_dir", "ob_data"))
        days = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
        if not days:
            return "📼 Регистратор: данных пока нет."
        size = sum(f.stat().st_size for d in days for f in d.iterdir())
        last = days[-1]
        trades = list(last.glob("*_trades.csv.gz"))
        newest = max((f.stat().st_mtime for f in last.iterdir()), default=0)
        anom = last / "anomalies.csv.gz"
        free = os.statvfs(str(root))
        free_gb = free.f_bavail * free.f_frsize / 1e9
        return (f"📼 Регистратор\n• дней записано: {len(days)}, объём {size / 1e9:.2f} ГБ, свободно на диске {free_gb:.0f} ГБ\n"
                f"• сегодня ({last.name}): монет со сделками {len(trades)}, "
                f"файл аномалий {'есть' if anom.exists() else 'нет'}\n"
                f"• последняя запись: {_ago(newest)}")

    def health(self):
        st = self.state()
        last_pass, started = st.get("_health", "last_pass"), st.get("_health", "started")
        err, err_ts = st.get("_health", "last_error"), st.get("_health", "last_error_ts")
        alive = last_pass and time.time() - last_pass < 5 * self.cfg.get("loop_seconds", 60)
        paused = os.path.exists(self.kill_file())
        return (f"🩺 Состояние: {'✅ работает' if alive else '❌ не отвечает'}"
                f"{' (⏸ пауза)' if paused else ''}\n"
                f"• последний проход: {_ago(last_pass)}\n• запущен: {_ago(started)}\n"
                f"• последняя ошибка: {(err + ', ' + _ago(err_ts)) if err else 'нет'}\n"
                f"• время UTC: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}")

    # ---------------------------------------------------------------- цикл
    def skip_old(self):
        r = self.api("getUpdates", offset=-1, timeout=0)
        res = r.get("result") or []
        self.offset = res[-1]["update_id"] + 1 if res else None

    def poll_once(self, timeout=2):
        # короткий опрос: на некоторых VPS долгие соединения к Telegram обрываются (Read timed out / reset)
        params = {"timeout": timeout}
        if self.offset is not None:
            params["offset"] = self.offset
        r = self.api("getUpdates", **params)
        for u in r.get("result") or []:
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat:
                continue                            # чужим не отвечаем
            try:
                self.send(self.handle(msg.get("text")))
            except Exception as e:
                log.warning(f"кнопка {msg.get('text')!r}: {e}")
                self.send(f"⚠️ Не получилось: {type(e).__name__}: {str(e)[:200]}")

    def run_forever(self):
        if not (self.token and self.chat):
            return
        try:
            self.skip_old()
            self.send("Кнопки управления ниже 👇")
        except Exception as e:
            log.warning(f"Telegram UI: {e}")
        fails = 0
        while True:
            try:
                self.poll_once()
                fails = 0
                time.sleep(1)
            except Exception as e:
                fails += 1
                if fails in (1, 10) or fails % 100 == 0:          # не засоряем журнал
                    log.warning(f"Telegram UI (ошибок подряд {fails}): {e}")
                time.sleep(min(30, 2 * fails))
