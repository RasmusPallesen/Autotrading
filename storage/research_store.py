"""
Research signal store.
Persists research agent findings so the trading agent can consume them.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "logs" / "trades.db"
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _is_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")


class ResearchStore:
    """
    Stores and retrieves research signals.
    Uses the same backend (PostgreSQL or SQLite) as TradeStore.
    """

    def __init__(self):
        if _is_postgres():
            self._setup_postgres()
        else:
            self._setup_sqlite()
        self._create_table()

    def _setup_postgres(self):
        import psycopg2
        from urllib.parse import urlparse, unquote
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
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
        logger.info("ResearchStore connected to PostgreSQL")

    def _setup_sqlite(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._backend = "sqlite"
        logger.info("ResearchStore opened SQLite at %s", DB_PATH)

    def _create_table(self):
        sql = """
            CREATE TABLE IF NOT EXISTS research_signals (
                id {serial} PRIMARY KEY,
                ts {tz} NOT NULL,
                symbol TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                conviction REAL NOT NULL,
                recommended_action TEXT NOT NULL,
                summary TEXT,
                key_points TEXT,
                risk_factors TEXT,
                sources_used INTEGER,
                expires_at {tz} NOT NULL
            )
        """.format(
            serial="SERIAL" if self._backend == "postgres" else "INTEGER AUTOINCREMENT",
            tz="TIMESTAMPTZ" if self._backend == "postgres" else "TEXT",
        )
        # SQLite fix
        if self._backend == "sqlite":
            sql = sql.replace("INTEGER AUTOINCREMENT", "INTEGER")
            self.conn.execute(sql)
            self.conn.commit()
        else:
            with self.conn.cursor() as cur:
                cur.execute(sql)

    def write_signal(
        self,
        symbol: str,
        sentiment: str,
        conviction: float,
        recommended_action: str,
        summary: str,
        key_points: List[str],
        risk_factors: List[str],
        sources_used: int,
        ttl_hours: int = 4,
    ):
        """Write a research signal, replacing any existing signal for this symbol."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=ttl_hours)

        # Delete existing signal for this symbol first (upsert pattern)
        self._execute("DELETE FROM research_signals WHERE symbol = %s", (symbol,))

        params = (
            now.isoformat(), symbol, sentiment, conviction,
            recommended_action, summary,
            json.dumps(key_points), json.dumps(risk_factors),
            sources_used, expires.isoformat(),
        )
        sql = """
            INSERT INTO research_signals
            (ts, symbol, sentiment, conviction, recommended_action,
             summary, key_points, risk_factors, sources_used, expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        self._execute(sql, params)
        logger.info("Research signal written for %s: %s %.0f%%", symbol, sentiment, conviction * 100)

    def get_signal(self, symbol: str) -> Optional[dict]:
        """Get the most recent non-expired signal for a symbol."""
        now = datetime.now(timezone.utc).isoformat()
        sql = """
            SELECT * FROM research_signals
            WHERE symbol = %s AND expires_at > %s
            ORDER BY ts DESC LIMIT 1
        """
        rows = self._fetchall(sql, (symbol, now))
        if not rows:
            return None
        row = rows[0]
        row["key_points"] = json.loads(row.get("key_points") or "[]")
        row["risk_factors"] = json.loads(row.get("risk_factors") or "[]")
        return row

    def get_all_active(self) -> List[dict]:
        """Get all non-expired signals."""
        now = datetime.now(timezone.utc).isoformat()
        sql = "SELECT * FROM research_signals WHERE expires_at > %s ORDER BY conviction DESC"
        rows = self._fetchall(sql, (now,))
        for row in rows:
            row["key_points"] = json.loads(row.get("key_points") or "[]")
            row["risk_factors"] = json.loads(row.get("risk_factors") or "[]")
        return rows

    def _execute(self, sql: str, params: tuple):
        try:
            if self._backend == "postgres":
                with self.conn.cursor() as cur:
                    cur.execute(sql, params)
            else:
                self.conn.execute(sql.replace("%s", "?"), params)
                self.conn.commit()
        except Exception as e:
            logger.error("ResearchStore write error: %s", e)

    def _fetchall(self, sql: str, params: tuple) -> List[dict]:
        try:
            if self._backend == "postgres":
                import psycopg2.extras
                with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return [dict(r) for r in cur.fetchall()]
            else:
                cur = self.conn.execute(sql.replace("%s", "?"), params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error("ResearchStore read error: %s", e)
            return []

    def close(self):
        self.conn.close()