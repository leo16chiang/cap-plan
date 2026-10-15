import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Point the app at a throwaway database before it's imported (app.py creates and seeds it on import).
os.environ["PORTAL_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ.setdefault("AUTH_MODE", "mock")

import app as portal  # noqa: E402

PASSWORD = portal.provider.demo_password


@pytest.fixture()
def fresh_db():
    """Empty the current cycle's tables between tests; keep the seeded history."""
    conn = portal.sqlite3.connect(portal.DB_PATH)
    for sql in ("DELETE FROM submissions WHERE cycle = ?", "DELETE FROM submission_versions WHERE cycle = ?",
                "DELETE FROM plan_settings WHERE cycle = ?", "DELETE FROM answer_flags WHERE cycle = ?"):
        conn.execute(sql, (portal.CYCLE,))
    conn.commit()
    conn.close()


@pytest.fixture()
def client(fresh_db):
    portal.app.config["TESTING"] = True
    return portal.app.test_client()


def login(client, upn):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["_csrf"]
    client.post("/login", data={"upn": upn, "password": PASSWORD, "_csrf": token})
    with client.session_transaction() as s:  # sign-in clears the session, so mint a new token
        assert "user" in s, f"login failed for {upn}"
        s["_csrf"] = "test-token"
    return "test-token"
