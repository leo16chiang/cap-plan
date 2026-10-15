"""Monthly MSU per application: from your existing Db2 table, or generated sample data for the demo.

    PORTAL_USAGE=db2
    PORTAL_DB2="Driver={IBM DB2 ODBC DRIVER};Database=...;Hostname=...;Port=...;Protocol=TCPIP;Uid=...;Pwd=..."

The query is in sql/db2_usage_query.sql; edit it to match your table. It's run once per application with
that application's usage_key from teams.json (defaults to the team id) and must return two columns:
the month (a DATE/TIMESTAMP, 'YYYY-MM...' text, or a YYYYMM number) and the average MSU for that month.
Results are cached for PORTAL_USAGE_CACHE_SECONDS (default an hour); monthly data doesn't change often.
"""
import json
import os
import threading
import time
from datetime import date
from pathlib import Path

import sample_usage

BASE = Path(__file__).resolve().parent
SOURCE = os.environ.get("PORTAL_USAGE", "sample").lower()
CACHE_SECONDS = int(os.environ.get("PORTAL_USAGE_CACHE_SECONDS", "3600"))

_teams = json.loads((BASE / "sample_data" / "teams.json").read_text(encoding="utf-8"))["teams"]
USAGE_KEYS = {t["id"]: t.get("usage_key", t["id"]) for t in _teams}

_cache = {}
_lock = threading.Lock()


def _month(value):
    """Db2 month value -> (year, month)."""
    if isinstance(value, date):  # date and datetime
        return (value.year, value.month)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = int(value)
        return (n // 100, n % 100)
    text = str(value).strip()
    return (int(text[:4]), int(text[5:7]))


def _from_db2(team_id):
    import pyodbc  # only needed when PORTAL_USAGE=db2

    sql = (BASE / "sql" / "db2_usage_query.sql").read_text(encoding="utf-8")
    conn = pyodbc.connect(os.environ["PORTAL_DB2"], autocommit=True, readonly=True)
    try:
        cur = conn.cursor()
        cur.execute(sql, (USAGE_KEYS[team_id],))
        rows = cur.fetchall()
    finally:
        conn.close()
    out = {}
    for month, msu in rows:
        if month is not None and msu is not None:
            out[_month(month)] = round(float(msu), 1)
    return dict(sorted(out.items()))


def monthly_msu(team_id):
    """{(year, month): average MSU}, oldest first."""
    if SOURCE != "db2":
        return sample_usage.monthly_msu(team_id)
    with _lock:
        hit = _cache.get(team_id)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    data = _from_db2(team_id)
    with _lock:
        _cache[team_id] = (time.monotonic(), data)
    return data
