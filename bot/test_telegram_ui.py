"""Проверка кнопок Telegram без сети:  python test_telegram_ui.py"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(__file__))
import yaml

import telegram_ui as T
from state import Position, State


class FakeTG:
    def __init__(self):
        self.sent, self.updates = [], []

    def __call__(self, method, **p):
        if method == "sendMessage":
            self.sent.append((p["chat_id"], p["text"], "reply_markup" in p))
            return {"ok": True}
        if method == "getUpdates":
            off = p.get("offset")
            res = [u for u in self.updates if off is None or off == -1 or u["update_id"] >= off]
            if off == -1:
                res = res[-1:]
            return {"ok": True, "result": res}


class FakeMarket:
    def tickers(self):
        return {"NEWUSDT": {"last": 0.9, "quote_volume": 1e8}}


def main():
    os.chdir(tempfile.mkdtemp())
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(T.__file__)), "config.example.yaml"),
                              encoding="utf-8"))
    cfg["telegram"] = {"token": "x", "chat_id": "111"}
    db = "s.sqlite"
    st = State(db)
    st.add_position(Position(0, "listing_short", "paper", "NEWUSDT", "short", 20, 20, 2, 1.0, time.time() - 3600))
    st.set("_health", "last_pass", time.time())
    os.makedirs("ob_data/2026-11-09")
    open("ob_data/2026-11-09/AAAUSDT_trades.csv.gz", "wb").write(b"x" * 1000)
    tg = FakeTG()
    ui = T.TelegramUI(cfg, api=tg, state_factory=lambda: State(db), market_factory=FakeMarket)

    tg.updates = [{"update_id": 5, "message": {"chat": {"id": 111}, "text": "привет"}}]
    ui.skip_old()
    ui.poll_once(0)
    assert tg.sent == [], "старые сообщения не должны обрабатываться"

    def press(text, uid, chat=111):
        tg.updates.append({"update_id": uid, "message": {"chat": {"id": chat}, "text": text}})
        n = len(tg.sent)
        ui.poll_once(0)
        return tg.sent[n:]

    r = press(T.B_POS, 6)
    assert len(r) == 1 and "NEWUSDT" in r[0][1] and "+" in r[0][1] and r[0][2], r     # шорт с 1.0 до 0.9 — плюс
    r = press(T.B_REPORT, 7)
    assert "Отчёт" in r[0][1] and "listing_short" in r[0][1], r
    r = press(T.B_REC, 8)
    assert "монет со сделками 1" in r[0][1], r
    r = press(T.B_HEALTH, 9)
    assert "работает" in r[0][1], r
    r = press(T.B_PAUSE, 10)
    assert os.path.exists("STOP") and "Пауза" in r[0][1]
    r = press(T.B_HEALTH, 11)
    assert "пауза" in r[0][1], r
    r = press(T.B_RESUME, 12)
    assert not os.path.exists("STOP")
    r = press(T.B_PAUSE, 13, chat=999)
    assert r == [] and not os.path.exists("STOP"), "чужой chat_id должен игнорироваться"
    print("кнопки: позиции, отчёт, регистратор, состояние, пауза/продолжить, чужие игнорируются — ок")
    print("ВСЕ ПРОВЕРКИ КНОПОК ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
