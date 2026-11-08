"""Хранилище бота: позиции, состояние стратегий, журнал событий (SQLite)."""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT, mode TEXT, symbol TEXT, side TEXT,
    qty REAL, notional REAL, leverage REAL,
    entry_price REAL, entry_time REAL,
    exit_price REAL, exit_time REAL,
    pnl_usd REAL, pnl_pct REAL, reason TEXT,
    extra TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (strategy TEXT, key TEXT, value TEXT, PRIMARY KEY (strategy, key));
CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, text TEXT);
"""


@dataclass
class Position:
    id: int
    strategy: str
    mode: str
    symbol: str
    side: str            # "long" | "short"
    qty: float
    notional: float
    leverage: float
    entry_price: float
    entry_time: float
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    reason: Optional[str] = None
    extra: dict = field(default_factory=dict)


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- позиции
    def add_position(self, p: Position) -> int:
        cur = self.db.execute(
            "INSERT INTO positions (strategy, mode, symbol, side, qty, notional, leverage, entry_price, entry_time, extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (p.strategy, p.mode, p.symbol, p.side, p.qty, p.notional, p.leverage, p.entry_price, p.entry_time,
             json.dumps(p.extra)))
        self.db.commit()
        return cur.lastrowid

    def update_extra(self, pid: int, extra: dict):
        self.db.execute("UPDATE positions SET extra=? WHERE id=?", (json.dumps(extra), pid))
        self.db.commit()

    def close_position(self, pid, exit_price, exit_time, pnl_usd, pnl_pct, reason, extra=None):
        self.db.execute("UPDATE positions SET exit_price=?, exit_time=?, pnl_usd=?, pnl_pct=?, reason=?"
                        + (", extra=?" if extra is not None else "") + " WHERE id=?",
                        (exit_price, exit_time, pnl_usd, pnl_pct, reason)
                        + ((json.dumps(extra),) if extra is not None else ()) + (pid,))
        self.db.commit()

    def _rows(self, where, args=()):
        cols = [c[1] for c in self.db.execute("PRAGMA table_info(positions)")]
        out = []
        for r in self.db.execute(f"SELECT * FROM positions WHERE {where} ORDER BY id", args):
            d = dict(zip(cols, r))
            d["extra"] = json.loads(d["extra"] or "{}")
            out.append(Position(**d))
        return out

    def open_positions(self, strategy=None, mode=None):
        w, a = "exit_time IS NULL", []
        if strategy:
            w += " AND strategy=?"; a.append(strategy)
        if mode:
            w += " AND mode=?"; a.append(mode)
        return self._rows(w, a)

    def closed_positions(self, strategy=None, mode=None, since=None):
        w, a = "exit_time IS NOT NULL", []
        if strategy:
            w += " AND strategy=?"; a.append(strategy)
        if mode:
            w += " AND mode=?"; a.append(mode)
        if since:
            w += " AND exit_time>=?"; a.append(since)
        return self._rows(w, a)

    # ---- состояние стратегий
    def get(self, strategy, key, default=None):
        r = self.db.execute("SELECT value FROM kv WHERE strategy=? AND key=?", (strategy, key)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, strategy, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (strategy, key, json.dumps(value)))
        self.db.commit()

    # ---- журнал
    def log(self, level, text):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), level, text))
        self.db.commit()
