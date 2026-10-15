"""Database connection for the portal: SQL Server in production, SQLite for the local demo and tests.

The app writes one dialect of SQL that both accept (? placeholders, no LIMIT/TOP, no upserts);
the few differences live here. Choose the backend with environment variables:

    PORTAL_SQLSERVER="Driver={ODBC Driver 18 for SQL Server};Server=...;Database=CapReview;..."
        -> SQL Server via pyodbc. Create the schema first with sql/sqlserver/*.sql.
    otherwise
        -> SQLite at PORTAL_DB (default instance/portal.db), created automatically.
"""
import os
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent
SQLSERVER_DSN = os.environ.get("PORTAL_SQLSERVER")
DIALECT = "sqlserver" if SQLSERVER_DSN else "sqlite"
SQLITE_PATH = Path(os.environ.get("PORTAL_DB", BASE / "instance" / "portal.db"))

if DIALECT == "sqlserver":
    import pyodbc  # only needed for SQL Server
    IntegrityError = pyodbc.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError

TABLES = ("submissions", "answers", "growth_projections", "answer_flags", "plan_settings",
          "submission_events", "audit_log")


def _plain(value):
    """Driver values -> what the app works with: ISO strings for timestamps, floats for decimals."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


class Database:
    """A thin wrapper over one DB-API connection that returns rows as dicts."""

    IntegrityError = IntegrityError

    def __init__(self):
        if DIALECT == "sqlserver":
            self.conn = pyodbc.connect(SQLSERVER_DSN, autocommit=False)
            # The workflow's compare-and-set updates rely on row counts, which NOCOUNT ON would hide.
            self.conn.execute("SET NOCOUNT OFF")
        else:
            SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(SQLITE_PATH)
            self.conn.execute("PRAGMA foreign_keys = ON")

    def query(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [{c: _plain(v) for c, v in zip(cols, row)} for row in cur.fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return _plain(row[0]) if row else None

    def execute(self, sql, params=()):
        """Runs an INSERT/UPDATE/DELETE and returns the number of rows it touched."""
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.rowcount

    def insert(self, table, values, id_column=None):
        """INSERT one row; returns the new identity value when id_column is given."""
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        cur = self.conn.cursor()
        if id_column and DIALECT == "sqlserver":
            cur.execute(f"INSERT INTO {table} ({cols}) OUTPUT INSERTED.{id_column} VALUES ({marks})",
                        list(values.values()))
            return cur.fetchone()[0]
        cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
        return cur.lastrowid if id_column else None

    def latest(self, table, order_by, n):
        """Newest n rows of a table (SQL Server has TOP, SQLite has LIMIT)."""
        if DIALECT == "sqlserver":
            return self.query(f"SELECT TOP {int(n)} * FROM {table} ORDER BY {order_by} DESC")
        return self.query(f"SELECT * FROM {table} ORDER BY {order_by} DESC LIMIT {int(n)}")

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def init_schema():
    """SQLite: create tables and views if missing. SQL Server: check the DBA-run scripts were applied."""
    db = Database()
    try:
        if DIALECT == "sqlite":
            db.conn.executescript((BASE / "sql" / "sqlite_schema.sql").read_text(encoding="utf-8"))
        else:
            missing = [t for t in TABLES if not db.scalar(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ?", (t,))]
            if missing:
                raise RuntimeError(f"SQL Server database is missing tables {missing}. "
                                   "Run sql/sqlserver/01_schema.sql (see README).")
        db.commit()
    finally:
        db.close()
