"""
Patch script — fixes TradeStore to read DATABASE_URL at runtime not import time.
Run from project root: python fix_trade_store.py
"""

NEW_TRADE_STORE = '''"""
Storage layer: PostgreSQL-backed trade log with SQLite fallback for local dev.
Set DATABASE_URL env var to use PostgreSQL, otherwise falls back to SQLite.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "logs" / "trades.db"


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql") or url.startswith("postgres")


class TradeStore:
    """
    Persists trade decisions and executions.
    Uses PostgreSQL if DATABASE_URL is set, otherwise SQLite.
    """

    def __init__(self):
        # Read at runtime not import time
        database_url = os.getenv("DATABASE_URL", "")
        if _is_postgres(database_url):
            self._setup_postgres(database_url)
        else:
            self._setup_sqlite()

    def _setup_postgres(self, url: str):
        try:
            import psycopg2
            from urllib.parse import urlparse, unquote
            url = url.replace("postgres://", "postgresql://", 1)
            parsed = urlparse(url)
            self.conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                dbname=parsed.path.lstrip("/"),
                user=parsed.username,
                password=unquote(parsed.password or ""),
                sslmode="require",
                connect_timeout=10,
            )
            self.conn.autocommit = True
            self._backend = "postgres"
            self._create_tables_postgres()
            logger.info("TradeStore connected to PostgreSQL")
        except ImportError:
            raise ImportError("Install psycopg2: pip install psycopg2-binary")

    def _setup_sqlite(self):
        import sqlite3
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._backend = "sqlite"
        self._create_tables_sqlite()
        logger.info("TradeStore opened at %s", DB_PATH)

    def _create_tables_postgres(self):
        with self.conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
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
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
                    order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    notional REAL,
                    qty REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    extra_json TEXT
                );
            """)

    def _create_tables_sqlite(self):
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
        """)
        self.conn.commit()

    def log_decision(self, symbol, action, confidence, rationale, urgency,
                     approved, approval_reason, notional):
        ts = datetime.now(timezone.utc).isoformat()
        params = (ts, symbol, action, confidence, rationale, urgency,
                  int(approved), approval_reason, notional)
        sql = """INSERT INTO decisions
                 (ts, symbol, action, confidence, rationale, urgency,
                  approved, approval_reason, notional)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        self._execute(sql, params)

    def log_execution(self, order_id, symbol, side, notional=None, qty=None,
                      stop_loss=None, take_profit=None, extra=None):
        ts = datetime.now(timezone.utc).isoformat()
        params = (ts, order_id, symbol, side, notional, qty, stop_loss,
                  take_profit, json.dumps(extra) if extra else None)
        sql = """INSERT INTO executions
                 (ts, order_id, symbol, side, notional, qty,
                  stop_loss, take_profit, extra_json)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        self._execute(sql, params)

    def _execute(self, sql: str, params: tuple):
        try:
            if self._backend == "postgres":
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
            else:
                self.conn.execute(sql.replace("%s", "?"), params)
                self.conn.commit()
        except Exception as e:
            logger.error("DB write error: %s", e)

    def recent_decisions(self, limit: int = 200) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT %s", (limit,))

    def recent_executions(self, limit: int = 100) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM executions ORDER BY id DESC LIMIT %s", (limit,))

    def _fetchall(self, sql: str, params: tuple) -> List[dict]:
        try:
            if self._backend == "postgres":
                import psycopg2.extras
                with self.conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor
                ) as cur:
                    cur.execute(sql, params)
                    return [dict(r) for r in cur.fetchall()]
            else:
                sql_lite = sql.replace("%s", "?")
                cur = self.conn.execute(sql_lite, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error("DB read error: %s", e)
            return []

    def close(self):
        self.conn.close()
'''

with open("storage\\trade_store.py", "w", encoding="utf-8") as f:
    f.write(NEW_TRADE_STORE)

print("SUCCESS: trade_store.py rewritten")
print("Restart start_agent.bat — TradeStore will now connect to PostgreSQL")
