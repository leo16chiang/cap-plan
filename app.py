"""Mainframe Capacity Review portal (sample).

Run:  python app.py           -> http://127.0.0.1:5057
Mode: AUTH_MODE=mock (default) simulates Entra ID from sample_data/.
      AUTH_MODE=entra uses real Entra ID; see README.md.
"""
import csv
import io
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

import analytics
import auth
import cycles
import workflow
from questions import (ALL_QUESTIONS, QUARTERS, SECTIONS, diff_answers, display_answers, display_value,
                       parse_answers, validate_complete)

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "instance" / "portal.db"
CYCLE = cycles.CURRENT["id"]
CYCLE_LABEL = cycles.CURRENT["label"]

_config = json.loads((BASE / "sample_data" / "teams.json").read_text(encoding="utf-8"))
TEAMS = {t["id"]: t for t in _config["teams"]}
ADMIN_GROUP_ID = _config["admin_group"]["id"]

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("PORTAL_SECRET_KEY", "dev-only-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PORTAL_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

AUTH_MODE = os.environ.get("AUTH_MODE", "mock").lower()
if AUTH_MODE == "entra":
    provider = auth.EntraAuth(
        os.environ["ENTRA_TENANT_ID"], os.environ["ENTRA_CLIENT_ID"], os.environ["ENTRA_CLIENT_SECRET"]
    )
else:
    provider = auth.MockEntra(BASE / "sample_data" / "mock_entra_directory.json")


# ---------------------------------------------------------------- database

SCHEMA = """
-- Current state of each team's review, one row per team per cycle.
CREATE TABLE IF NOT EXISTS submissions (
    team_id         TEXT NOT NULL,
    cycle           TEXT NOT NULL,
    answers         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'draft',   -- see workflow.py
    updated_by      TEXT,
    updated_at      TEXT,
    submitted_by    TEXT,
    submitted_at    TEXT,
    reviewed_by     TEXT,
    reviewed_name   TEXT,
    reviewed_at     TEXT,
    review_note     TEXT,
    signed_off_by   TEXT,
    signed_off_name TEXT,
    signed_off_at   TEXT,
    PRIMARY KEY (team_id, cycle)
);
-- Every workflow event keeps a full copy of the answers at that moment.
CREATE TABLE IF NOT EXISTS submission_versions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
    cycle   TEXT NOT NULL,
    version INTEGER NOT NULL,
    event   TEXT NOT NULL,                  -- save | submit | request_changes | approve | signoff | reopen
    status  TEXT NOT NULL,                  -- status after the event
    answers TEXT NOT NULL,
    by_user TEXT,
    by_name TEXT,
    at      TEXT NOT NULL,
    note    TEXT,
    UNIQUE (team_id, cycle, version)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    user    TEXT,
    action  TEXT NOT NULL,
    team_id TEXT,
    cycle   TEXT,
    detail  TEXT
);
"""


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
        if AUTH_MODE == "mock" and empty:
            seed_sample_history(conn)
    finally:
        conn.close()


def seed_sample_history(conn):
    """Replays sample_data/past_submissions.json through the same workflow the live app uses."""
    data = json.loads((BASE / "sample_data" / "past_submissions.json").read_text(encoding="utf-8"))
    for sub in data["submissions"]:
        answers = {}
        for v in sub["versions"]:
            if workflow.EVENTS[v["event"]].get("answers"):
                answers = v.get("answers", answers)
            ok = apply_event(conn, sub["team_id"], sub["cycle"], v["event"], v["by"], v["name"], v["at"],
                             answers=answers if workflow.EVENTS[v["event"]].get("answers") else None,
                             note=v.get("note"))
            if not ok:
                raise ValueError(f"Bad sample history for {sub['team_id']} {sub['cycle']}: {v['event']}")
    conn.commit()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(action, team_id=None, detail=""):
    user = session.get("user", {}).get("upn")
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (at, user, action, team_id, cycle, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (now(), user, action, team_id, CYCLE if team_id else None, detail),
    )
    db.commit()


def load_submission(team_id, cycle=CYCLE):
    row = get_db().execute(
        "SELECT * FROM submissions WHERE team_id = ? AND cycle = ?", (team_id, cycle)
    ).fetchone()
    if row is None:
        return {"status": "not_started", "answers": {}}
    sub = dict(row)
    sub["answers"] = json.loads(sub["answers"])
    return sub


def load_versions(team_id, cycle):
    rows = get_db().execute(
        "SELECT * FROM submission_versions WHERE team_id = ? AND cycle = ? ORDER BY version", (team_id, cycle)
    ).fetchall()
    return [{**dict(r), "answers": json.loads(r["answers"])} for r in rows]


def apply_event(db, team_id, cycle, event, by_user, by_name, at, answers=None, note=None):
    """Moves a submission through the workflow and records a version snapshot.

    Returns False (and changes nothing) if the submission's status doesn't allow the event,
    including when someone else changed it a moment ago. The caller commits or rolls back.
    """
    spec = workflow.EVENTS[event]
    row = db.execute(
        "SELECT status, answers FROM submissions WHERE team_id = ? AND cycle = ?", (team_id, cycle)
    ).fetchone()
    current = row["status"] if row else "not_started"
    if current not in spec["from"]:
        return False

    new_status = workflow.next_status(event, current)
    fields = {"status": new_status}
    if answers is not None:
        fields.update(answers=json.dumps(answers), updated_by=by_user, updated_at=at)
    if event == "submit":
        fields.update(submitted_by=by_user, submitted_at=at)
    if event in ("approve", "request_changes"):
        fields.update(reviewed_by=by_user, reviewed_name=by_name, reviewed_at=at, review_note=note)
    if event == "signoff":
        fields.update(signed_off_by=by_user, signed_off_name=by_name, signed_off_at=at)
    if event == "reopen":
        fields.update(signed_off_by=None, signed_off_name=None, signed_off_at=None,
                      reviewed_by=None, reviewed_name=None, reviewed_at=None, review_note=None)

    if row is None:
        columns = ["team_id", "cycle", *fields]
        try:
            db.execute(f"INSERT INTO submissions ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                       [team_id, cycle, *fields.values()])
        except sqlite3.IntegrityError:  # another request created it first
            return False
    else:
        # "AND status = ?" makes this a compare-and-set: it fails if the status moved underneath us.
        cur = db.execute(
            f"UPDATE submissions SET {', '.join(f'{k} = ?' for k in fields)} "
            "WHERE team_id = ? AND cycle = ? AND status = ?",
            [*fields.values(), team_id, cycle, current],
        )
        if cur.rowcount != 1:
            return False

    snapshot = answers if answers is not None else (json.loads(row["answers"]) if row else {})
    db.execute(
        """
        INSERT INTO submission_versions (team_id, cycle, version, event, status, answers, by_user, by_name, at, note)
        VALUES (?, ?, (SELECT COALESCE(MAX(version), 0) + 1 FROM submission_versions WHERE team_id = ? AND cycle = ?),
                ?, ?, ?, ?, ?, ?, ?)
        """,
        (team_id, cycle, team_id, cycle, event, new_status, json.dumps(snapshot), by_user, by_name, at, note),
    )
    return True


# ---------------------------------------------------------------- auth helpers

def roles_from_groups(group_ids):
    """Entra group membership -> {team_id: 'signer' | 'viewer'}. Signer wins over viewer."""
    groups = set(group_ids)
    roles = {}
    for team in TEAMS.values():
        if team["signer_group"]["id"] in groups:
            roles[team["id"]] = "signer"
        elif team["viewer_group"]["id"] in groups:
            roles[team["id"]] = "viewer"
    return roles


def start_session(claims):
    session.clear()
    session.permanent = True
    roles = roles_from_groups(claims["groups"])
    session["user"] = {
        "oid": claims["oid"],
        "upn": claims["preferred_username"].lower(),
        "name": claims["name"],
        "is_admin": ADMIN_GROUP_ID in claims["groups"],
        "roles": roles,
        "verified_at": now(),
    }
    detail = ", ".join(f"{t}:{r}" for t, r in roles.items())
    if session["user"]["is_admin"]:
        detail = ", ".join(filter(None, ["admin", detail]))
    audit("login", detail=detail or "no team groups")


def current_user():
    return session.get("user")


def is_admin():
    return bool(current_user() and current_user()["is_admin"])


def session_role(team_id):
    return current_user()["roles"].get(team_id)


def live_role(team_id):
    """Re-check group membership right before a write, in case it changed since sign-in."""
    groups = provider.current_groups(current_user()["oid"])
    if groups is None:  # provider can't re-query; fall back to the token's groups
        return session_role(team_id)
    return roles_from_groups(groups).get(team_id)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not is_admin():
            audit("admin_denied", detail=request.path)
            abort(403, "Capacity Planning admins only.")
        return view(*args, **kwargs)
    return wrapper


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Your form expired. Go back, refresh the page and try again.")


@app.context_processor
def template_globals():
    return {
        "csrf_token": csrf_token,
        "cycle_label": CYCLE_LABEL,
        "auth_mode": AUTH_MODE,
        "user": current_user(),
    }


@app.template_filter("when")
def fmt_when(iso):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%b %d, %Y %H:%M UTC").replace(" 0", " ")


# ---------------------------------------------------------------- routes: auth

@app.get("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["is_admin"]:
        return redirect(url_for("admin"))
    if not user["roles"]:
        return render_template("no_access.html", teams=TEAMS.values())
    if len(user["roles"]) == 1:
        return redirect(url_for("team_page", team_id=next(iter(user["roles"]))))
    return redirect(url_for("my_teams"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if provider.interactive:  # real Entra: hand off to Microsoft
        flow = provider.begin(url_for("auth_callback", _external=True))
        session["auth_flow"] = flow
        return redirect(flow["auth_uri"])

    if request.method == "POST":
        try:
            claims = provider.authenticate(request.form.get("upn"), request.form.get("password"))
        except auth.AuthError as exc:
            flash(str(exc), "error")
            return render_template("login.html", accounts=provider.demo_accounts(),
                                   demo_password=provider.demo_password), 401
        start_session(claims)
        return redirect(url_for("index"))
    return render_template("login.html", accounts=provider.demo_accounts(), demo_password=provider.demo_password)


@app.get("/auth/callback")
def auth_callback():
    flow = session.pop("auth_flow", None)
    if not flow:
        return redirect(url_for("login"))
    try:
        claims = provider.complete(flow, request.args)
    except auth.AuthError as exc:
        abort(401, f"Sign-in failed: {exc}")
    start_session(claims)
    return redirect(url_for("index"))


@app.post("/logout")
def logout():
    if current_user():
        audit("logout")
    session.clear()
    if provider.interactive:
        return redirect(provider.logout_url(url_for("login", _external=True)))
    return redirect(url_for("login"))


# ---------------------------------------------------------------- routes: teams

@app.get("/teams")
@login_required
def my_teams():
    roles = current_user()["roles"]
    cards = [(TEAMS[t], r, load_submission(t)) for t, r in roles.items() if t in TEAMS]
    return render_template("my_teams.html", cards=cards)


def viewable_team(team_id):
    """(team, role) if the current user may view this team, otherwise 403/404."""
    team = TEAMS.get(team_id) or abort(404)
    role = session_role(team_id)
    if role is None and not is_admin():
        audit("view_denied", team_id, request.path)
        abort(403, "You're not in an Entra group for this team.")
    return team, role


def team_header_context(team, role):
    group = team["signer_group"] if role == "signer" else team["viewer_group"]
    return {
        "team": team, "role": role,
        "group_name": group["displayName"] if role else None,
        "many_teams": len(current_user()["roles"]) > 1,
    }


def render_team(team, role, sub, answers, errors, status=200):
    prev_cycle = cycles.previous(CYCLE)
    prev_sub = load_submission(team["id"], prev_cycle["id"]) if prev_cycle else None
    prev_answers = prev_sub["answers"] if prev_sub and prev_sub["status"] == "signed_off" else None
    return render_template(
        "team.html",
        **team_header_context(team, role),
        sub=sub, answers=answers, errors=errors,
        editable=role == "signer" and sub["status"] in workflow.EDITABLE,
        can_sign=role == "signer" and sub["status"] == "approved",
        sections=SECTIONS, quarters=QUARTERS,
        usage=analytics.team_overview(team["id"], prev_cycle, prev_answers),
        active_tab="current",
    ), status


@app.get("/team/<team_id>")
@login_required
def team_page(team_id):
    team, role = viewable_team(team_id)
    sub = load_submission(team_id)
    return render_team(team, role, sub, sub["answers"], {})


TEAM_FLASH = {
    "save": "Draft saved.",
    "submit": "Submitted. Capacity Planning will review it and get back to you before you sign off.",
    "signoff": "Signed off. Thank you!",
}


@app.post("/team/<team_id>")
@login_required
def team_submit(team_id):
    team = TEAMS.get(team_id) or abort(404)
    action = request.form.get("action")
    if action not in workflow.TEAM_EVENTS:
        abort(400)

    # Authorization happens here on the server, whatever the browser showed.
    role = live_role(team_id)
    if role != "signer":
        audit(f"{action}_denied", team_id, f"role={role}")
        abort(403, f"Only members of {team['signer_group']['displayName']} can edit, submit or sign off for {team['name']}.")

    sub = load_submission(team_id)
    if sub["status"] not in workflow.EVENTS[action]["from"]:
        abort(409, workflow.blocked_message(action, sub["status"]))

    answers = parse_answers(request.form) if workflow.EVENTS[action].get("answers") else None
    errors = {}
    if action == "submit":
        errors = validate_complete(answers)
    if action == "signoff" and not request.form.get("attest"):
        errors["attest"] = "Please confirm before signing off."
    if errors:
        flash(f"{len(errors)} item(s) need attention. Nothing was {'submitted' if action == 'submit' else 'signed'}.", "error")
        return render_team(team, role, sub, answers if answers is not None else sub["answers"], errors, status=400)

    user = current_user()
    if not apply_event(get_db(), team_id, CYCLE, action, user["upn"], user["name"], now(), answers=answers):
        get_db().rollback()
        abort(409, "This review changed while you were working on it. Refresh the page to see its current status.")
    get_db().commit()
    audit(action, team_id)
    flash(TEAM_FLASH[action], "ok")
    return redirect(url_for("team_page", team_id=team_id))


REVIEW_FLASH = {
    "approve": "Approved. The team can now sign off.",
    "request_changes": "Sent back to the team with your note.",
    "reopen": "Review reopened. The team can edit and resubmit.",
}


@app.post("/team/<team_id>/review")
@admin_required
def team_review(team_id):
    TEAMS.get(team_id) or abort(404)
    action = request.form.get("action")
    if action not in workflow.ADMIN_EVENTS:
        abort(400)
    note = request.form.get("note", "").strip()[:1000]
    if workflow.EVENTS[action].get("note") and not note:
        flash("Please add a note so the team knows why.", "error")
        return redirect(url_for("team_page", team_id=team_id))

    sub = load_submission(team_id)
    if sub["status"] not in workflow.EVENTS[action]["from"]:
        abort(409, workflow.blocked_message(action, sub["status"]))

    user = current_user()
    if not apply_event(get_db(), team_id, CYCLE, action, user["upn"], user["name"], now(), note=note or None):
        get_db().rollback()
        abort(409, "This review changed while you were looking at it. Refresh the page to see its current status.")
    get_db().commit()
    audit(action, team_id, note)
    flash(REVIEW_FLASH[action], "ok")
    return redirect(url_for("team_page", team_id=team_id))


@app.get("/team/<team_id>/history")
@login_required
def team_history(team_id):
    team, role = viewable_team(team_id)
    entries = []
    for cycle in reversed(cycles.CYCLES):
        versions = load_versions(team_id, cycle["id"])
        sub = load_submission(team_id, cycle["id"])
        if not versions and sub["status"] == "not_started":
            continue
        labels = cycles.quarter_labels(cycle["id"])
        changelog = []
        last_answers = {}
        for v in versions:
            changes = None
            if workflow.EVENTS[v["event"]].get("answers"):
                changes = diff_answers(last_answers, v["answers"], labels)
                last_answers = v["answers"]
            changelog.append({**v, "label": workflow.EVENTS[v["event"]]["label"], "changes": changes})
        entries.append({
            "cycle": cycle,
            "sub": sub,
            "is_current": cycle["id"] == CYCLE,
            "answers": display_answers(sub["answers"], labels),
            "outcomes": analytics.outcomes(team_id, cycle, sub["answers"]) if sub["answers"].get("growth") else None,
            "changelog": list(reversed(changelog)),
        })
    return render_template("history.html", **team_header_context(team, role), entries=entries, active_tab="history")


# ---------------------------------------------------------------- routes: admin

ADMIN_ORDER = ["submitted", "changes_requested", "approved", "draft", "not_started", "signed_off"]


@app.get("/admin")
@admin_required
def admin():
    rows = [(team, load_submission(team["id"])) for team in TEAMS.values()]
    rows.sort(key=lambda r: (ADMIN_ORDER.index(r[1]["status"]), r[0]["name"]))
    counts = {status: 0 for status in workflow.STATUS_LABELS}
    for _, sub in rows:
        counts[sub["status"]] += 1
    log = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50").fetchall()
    return render_template("admin.html", rows=rows, counts=counts, log=log)


@app.get("/admin/export.csv")
@admin_required
def admin_export():
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["team_id", "team", "status", "submitted_at", "reviewed_by", "reviewed_at", "review_note",
              "signed_off_by", "signed_off_at", "baseline_msu"]
    for q in ALL_QUESTIONS:
        header += [f"{q['id']}_{qt}" for qt in QUARTERS] if q["type"] == "quarters" else [q["id"]]
    writer.writerow(header)
    for team in TEAMS.values():
        sub = load_submission(team["id"])
        a = sub["answers"]
        row = [team["id"], team["name"], sub["status"]]
        row += [sub.get(k) or "" for k in ("submitted_at", "reviewed_by", "reviewed_at", "review_note",
                                           "signed_off_by", "signed_off_at")]
        row.append(analytics.baseline(analytics.monthly_msu(team["id"]), cycles.CURRENT))
        for q in ALL_QUESTIONS:
            if q["type"] == "quarters":
                row += a.get(q["id"]) or [""] * len(QUARTERS)
            else:
                row.append(display_value(q, a, QUARTERS))  # spells out "Other: …"
        writer.writerow(row)
    audit("export_csv")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=capacity-review-{CYCLE}.csv"})


# ---------------------------------------------------------------- errors

@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
def http_error(err):
    return render_template("error.html", code=err.code, name=err.name, message=err.description), err.code


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5057)), debug=True)
