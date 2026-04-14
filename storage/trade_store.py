"""
Storage layer: SQLite-backed trade log and state persistence.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "logs" / "trades.db"


class TradeStore:
    """Persists trade decisions, executions, and daily summaries to SQLite."""

    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._create_tables()
        logger.info("TradeStore opened at %s", db_path)

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence REAL,
                rationale TEXT,
                urgency TEXT,
                approved INTEGER,
                approval_reason TEXT,
                notional REAL
            );

            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                order_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                notional REAL,
                qty REAL,
                stop_loss REAL,
                take_profit REAL,
                extra_json TEXT
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                snapshot_json TEXT NOT NULL
            );
        """)
        self.conn.commit()

    def log_decision(
        self,
        symbol: str,
        action: str,
        confidence: float,
        rationale: str,
        urgency: str,
        approved: bool,
        approval_reason: str,
        notional: Optional[float],
    ):
        ts = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO decisions
               (ts, symbol, action, confidence, rationale, urgency, approved, approval_reason, notional)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, symbol, action, confidence, rationale, urgency, int(approved), approval_reason, notional),
        )
        self.conn.commit()

    def log_execution(
        self,
        order_id: str,
        symbol: str,
        side: str,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        extra: Optional[dict] = None,
    ):
        ts = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO executions
               (ts, order_id, symbol, side, notional, qty, stop_loss, take_profit, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, order_id, symbol, side, notional, qty, stop_loss, take_profit,
             json.dumps(extra) if extra else None),
        )
        self.conn.commit()

    def log_snapshot(self, symbol: str, snapshot_dict: dict):
        ts = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO snapshots (ts, symbol, snapshot_json) VALUES (?,?,?)",
            (ts, symbol, json.dumps(snapshot_dict)),
        )
        self.conn.commit()

    def recent_decisions(self, limit: int = 50) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def recent_executions(self, limit: int = 20) -> List[dict]:
        cur = self.conn.execute(
            "SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self):
        self.conn.close()
