"""Уведомления в Telegram (если токен не задан — только в консоль/журнал)."""
import logging

import requests

log = logging.getLogger("bot")


class Notifier:
    def __init__(self, cfg, state=None):
        tg = cfg.get("telegram", {}) or {}
        self.token, self.chat = tg.get("token"), tg.get("chat_id")
        self.state = state

    def send(self, text):
        log.info(text)
        if self.state:
            self.state.log("info", text)
        if not (self.token and self.chat):
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat, "text": text}, timeout=15)
        except Exception as e:
            log.warning(f"Telegram недоступен: {e}")
