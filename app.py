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
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for

import analytics
import auth
import cycles
import db as dbmod
import store
import workflow
from questions import (ALL_QUESTIONS, FLAG_MAX_TEXT, QUARTERS, SECTIONS, diff_answers, display_answers,
                       display_value, flag_targets, format_target_value, parse_adjusted, parse_answers,
                       parse_target, target_key, target_value, validate_complete, validate_numbers)

BASE = Path(__file__).resolve().parent
CYCLE = cycles.CURRENT["id"]
CYCLE_LABEL = cycles.CURRENT["label"]

_config = json.loads((BASE / "sample_data" / "teams.json").read_text(encoding="utf-8"))
TEAMS = {t["id"]: t for t in _config["teams"]}
ADMIN_GROUP_ID = _config["admin_group"]["id"]

AUTH_MODE = os.environ.get("AUTH_MODE", "mock").lower()
SECRET_KEY = os.environ.get("PORTAL_SECRET_KEY")
if not SECRET_KEY:
    # The session cookie carries the user's roles and admin flag, so a known key lets anyone forge them.
    if AUTH_MODE != "mock":
        raise RuntimeError("Set PORTAL_SECRET_KEY to a long random value before running outside demo mode.")
    SECRET_KEY = "dev-only-change-me"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PORTAL_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

if AUTH_MODE == "entra":
    provider = auth.EntraAuth(
        os.environ["ENTRA_TENANT_ID"], os.environ["ENTRA_CLIENT_ID"], os.environ["ENTRA_CLIENT_SECRET"]
    )
else:
    provider = auth.MockEntra(BASE / "sample_data" / "mock_entra_directory.json")


# ---------------------------------------------------------------- database

def init_db():
    dbmod.init_schema()
    db = dbmod.Database()
    try:
        if AUTH_MODE == "mock" and not db.scalar("SELECT COUNT(*) FROM submissions"):
            seed_sample_history(db)
    finally:
        db.close()


def _utc(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def seed_sample_history(db):
    """Replays sample_data/past_submissions.json through the same workflow the live app uses."""
    data = json.loads((BASE / "sample_data" / "past_submissions.json").read_text(encoding="utf-8"))
    for sub in data["submissions"]:
        cycle = cycles.BY_ID[sub["cycle"]]
        method = cycle["default_baseline"]
        base = (analytics.computed_baseline(analytics.monthly_msu(sub["team_id"]), cycle, method),
                method, analytics.baseline_basis(cycle, method))
        answers = {}
        for v in sub["versions"]:
            carries = workflow.EVENTS[v["event"]].get("answers")
            if carries:
                answers = v.get("answers", answers)
            ok = store.apply_event(db, sub["team_id"], sub["cycle"], v["event"], v["by"], v["name"], _utc(v["at"]),
                                   answers=answers if carries else None, note=v.get("note"),
                                   baseline=base if carries else None)
            if not ok:
                raise ValueError(f"Bad sample history for {sub['team_id']} {sub['cycle']}: {v['event']}")
    db.commit()


def get_db():
    if "db" not in g:
        g.db = dbmod.Database()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


now = store.now


def audit(action, team_id=None, detail=""):
    store.audit(get_db(), session.get("user", {}).get("upn"), action, team_id, CYCLE if team_id else None, detail)


def load_submission(team_id, cycle=CYCLE):
    return store.load_submission(get_db(), team_id, cycle)


# ---------------------------------------------------------------- plan settings & flags

CYCLE_DEFAULT = None  # plan_settings.team_id for the cycle-wide default
LOCKED = ("submitted", "approved", "signed_off")  # answers are locked, so is the baseline they were made against


def load_setting(team_id, cycle_id):
    return store.load_setting(get_db(), cycle_id, team_id)


def cycle_default_method(cycle):
    row = load_setting(CYCLE_DEFAULT, cycle["id"])
    return row["baseline_method"] if row else cycle["default_baseline"]


def plan_baseline(team_id, cycle, msu=None):
    """The starting point Capacity Planning has set for a team right now.

    Team setting if there is one, else the cycle-wide default from Plan settings, else cycles.py.
    """
    team_row = load_setting(team_id, cycle["id"])
    method = team_row["baseline_method"] if team_row else cycle_default_method(cycle)
    if method == "manual":
        value = team_row["manual_msu"]
    else:
        value = analytics.computed_baseline(msu or analytics.monthly_msu(team_id), cycle, method)
    return {
        "value": value,
        "method": method,
        "basis": analytics.baseline_basis(cycle, method),
        "source": "team" if team_row else "cycle",
        "setting": team_row,
        "locked": False,
    }


def team_baseline(team_id, cycle, sub, msu=None):
    """The baseline a submission's answers are measured against.

    While the team can still edit, that's the current plan setting. Once the answers are locked
    (or the cycle is over), it's the baseline recorded with them, so a later change to Plan
    settings never re-scales numbers the team already submitted or signed.
    """
    editable = cycle["id"] == CYCLE and sub["status"] in workflow.EDITABLE
    if not editable and sub.get("baseline_msu") is not None:
        return {"value": sub["baseline_msu"], "method": sub.get("baseline_method"),
                "basis": sub.get("baseline_basis") or "",
                "source": "submission", "setting": None, "locked": True}
    return plan_baseline(team_id, cycle, msu)


def baseline_snapshot(base):
    return (base["value"], base["method"], base["basis"])


def load_flags(team_id, cycle_id, team_view=False):
    """All flags for a submission, newest first. team_view hides internal ones."""
    flags = store.load_flags(get_db(), team_id, cycle_id, team_view)
    for f in flags:
        target = parse_target(target_key(f["question_id"], f["quarter"]))
        q = target[0] if target else {"id": f["question_id"], "label": f["question_id"], "type": "text"}
        f["target"] = target_key(f["question_id"], f["quarter"])
        f["target_label"] = dict(flag_targets()).get(f["target"], f["question_id"])
        f["original_display"] = format_target_value(q, f["quarter"], f["original"])
        f["adjusted_display"] = format_target_value(q, f["quarter"], f["adjusted"])
        f["active"] = f["resolved_at"] is None
    return flags


def active_flags(flags, answers):
    """{target: flag} for unresolved flags, marking any whose answer the team has changed since."""
    out = {}
    for f in flags:
        if not f["active"]:
            continue
        target = parse_target(f["target"])
        if target:
            q, quarter = target
            current = target_value(q, quarter, answers)
            f["stale"] = current != (f["original"] or "")
            f["current_display"] = format_target_value(q, quarter, current)
        out[f["target"]] = f
    return out


def adjusted_growth(flags):
    """Capacity Planning's planning % per quarter (None where the team's number stands)."""
    out = [None] * len(QUARTERS)
    for f in flags:
        if f["active"] and f["question_id"] == "growth" and f["adjusted_pct"] is not None:
            if f["quarter"] is not None and f["quarter"] < len(out):
                out[f["quarter"]] = f["adjusted_pct"]
    return out


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


def previous_answers(team_id, current_base, limit=2):
    """The team's growth answers from earlier cycles, lined up against this cycle's quarters.

    Earlier answers were % changes against that cycle's own baseline, so each one is converted to
    MSU and then to % change against today's baseline to make it comparable.
    """
    out = []
    current = [q["label"] for q in cycles.CURRENT["quarters"]]
    for cycle in reversed(cycles.CYCLES[:-1]):
        sub = load_submission(team_id, cycle["id"])
        growth = sub["answers"].get("growth")
        if not growth or not any(growth):
            continue
        base = team_baseline(team_id, cycle, sub)["value"]
        answered = dict(zip(cycles.quarter_labels(cycle["id"]), growth))
        cells = []
        for label in current:
            pct = analytics._num(answered.get(label))
            msu = analytics.msu_for(base, pct)
            cells.append(None if msu is None else {
                "msu": msu, "pct": round((msu / current_base - 1) * 100, 1) if current_base else None})
        out.append({
            "label": cycle["label"],
            "status": workflow.STATUS_LABELS[sub["status"]],
            "signed_off": sub["status"] == "signed_off",
            "baseline": base,
            "raw": ", ".join(f"{q}: {v}%" for q, v in answered.items() if v not in ("", None)),
            "cells": cells,
        })
        if len(out) == limit:
            break
    return out


def render_team(team, role, sub, answers, errors, status=200, flag_form=None):
    prev_cycle = cycles.previous(CYCLE)
    prev_sub = load_submission(team["id"], prev_cycle["id"]) if prev_cycle else None
    signed_prev = prev_sub and prev_sub["status"] == "signed_off"
    prev_answers = prev_sub["answers"] if signed_prev else None
    prev_base = team_baseline(team["id"], prev_cycle, prev_sub)["value"] if signed_prev else None

    base = team_baseline(team["id"], cycles.CURRENT, sub)
    # Tell the team if Capacity Planning moved the starting point since they last saved.
    baseline_moved = (sub["status"] in workflow.EDITABLE and sub.get("baseline_msu") is not None
                      and base["value"] is not None and abs(sub["baseline_msu"] - base["value"]) >= 0.05)

    flags = load_flags(team["id"], CYCLE, team_view=not is_admin())
    active = active_flags(flags, answers)
    usage = analytics.team_overview(team["id"], base, prev_cycle, prev_answers, prev_base,
                                    adjusted=adjusted_growth(flags))
    usage["previous"] = previous_answers(team["id"], base["value"])
    return render_template(
        "team.html",
        **team_header_context(team, role),
        sub=sub, answers=answers, errors=errors,
        editable=role == "signer" and sub["status"] in workflow.EDITABLE,
        can_sign=role == "signer" and sub["status"] == "approved",
        sections=SECTIONS, quarters=QUARTERS,
        usage=usage,
        base=base, baseline_moved=baseline_moved,
        flags=flags, active_flags=active, flag_targets=flag_targets(), flag_form=flag_form or {},
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
    elif action == "save":
        errors = validate_numbers(answers)
    if action == "signoff" and not request.form.get("attest"):
        errors["attest"] = "Please confirm before signing off."
    if errors:
        verb = {"submit": "submitted", "save": "saved"}.get(action, "signed")
        flash(f"{len(errors)} item(s) need attention. Nothing was {verb}.", "error")
        return render_team(team, role, sub, answers if answers is not None else sub["answers"], errors, status=400)

    user = current_user()
    base = baseline_snapshot(plan_baseline(team_id, cycles.CURRENT)) if answers is not None else None
    if not store.apply_event(get_db(), team_id, CYCLE, action, user["upn"], user["name"], now(),
                             answers=answers, baseline=base):
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
    if not store.apply_event(get_db(), team_id, CYCLE, action, user["upn"], user["name"], now(), note=note or None):
        get_db().rollback()
        abort(409, "This review changed while you were looking at it. Refresh the page to see its current status.")
    get_db().commit()
    audit(action, team_id, note)
    flash(REVIEW_FLASH[action], "ok")
    return redirect(url_for("team_page", team_id=team_id))


@app.post("/team/<team_id>/flags")
@admin_required
def flag_add(team_id):
    """Flag one answer with a comment and, optionally, the value Capacity Planning plans with instead."""
    team = TEAMS.get(team_id) or abort(404)
    sub = load_submission(team_id)
    if sub["status"] == "not_started":
        abort(409, "There's nothing to flag until the team has saved some answers.")

    form = {k: request.form.get(k, "") for k in ("target", "adjusted", "comment")}
    form["visible"] = bool(request.form.get("visible"))
    errors = {}
    target = parse_target(form["target"])
    if target is None:
        errors["target"] = "Pick the answer you want to flag."
    comment = form["comment"].strip()[:FLAG_MAX_TEXT]
    if not comment:
        errors["comment"] = "Add a comment explaining the flag."
    adjusted = None
    if target:
        adjusted, err = parse_adjusted(*target, form["adjusted"])
        if err:
            errors["adjusted"] = err
    if errors:
        flash("The flag wasn't saved. " + " ".join(errors.values()), "error")
        return render_team(team, None, sub, sub["answers"], {}, status=400, flag_form={**form, "errors": errors})

    q, quarter = target
    user, db = current_user(), get_db()
    original = target_value(q, quarter, sub["answers"])
    # Flagging the same answer again closes the open flag as superseded; it stays in the table.
    try:
        store.add_flag(db, sub["submission_id"], q["id"], quarter, original, adjusted, comment, form["visible"],
                       user["upn"], user["name"])
    except db.IntegrityError:  # someone flagged the same answer at the same moment
        db.rollback()
        abort(409, "Someone else just flagged that answer. Refresh the page and try again.")
    db.commit()
    detail = f"{form['target']}: {original!r}" + (f" -> {adjusted!r}" if adjusted is not None else "")
    audit("flag", team_id, f"{detail} ({'shared' if form['visible'] else 'internal'}): {comment}")
    flash("Flag saved. The team's original answer is kept alongside it.", "ok")
    return redirect(url_for("team_page", team_id=team_id) + "#flags")


@app.post("/team/<team_id>/flags/<int:flag_id>/clear")
@admin_required
def flag_clear(team_id, flag_id):
    TEAMS.get(team_id) or abort(404)
    note = request.form.get("note", "").strip()[:FLAG_MAX_TEXT] or None
    user = current_user()
    if not store.clear_flag(get_db(), flag_id, team_id, CYCLE, note, user["upn"], user["name"]):
        get_db().rollback()
        abort(409, "That flag was already cleared or doesn't belong to this review.")
    get_db().commit()
    audit("flag_clear", team_id, f"flag {flag_id}" + (f": {note}" if note else ""))
    flash("Flag cleared. It stays in the history.", "ok")
    return redirect(url_for("team_page", team_id=team_id) + "#flags")


@app.get("/team/<team_id>/history")
@login_required
def team_history(team_id):
    team, role = viewable_team(team_id)
    entries = []
    for cycle in reversed(cycles.CYCLES):
        versions = store.load_events(get_db(), team_id, cycle["id"])
        sub = load_submission(team_id, cycle["id"])
        if not versions and sub["status"] == "not_started":
            continue
        labels = cycles.quarter_labels(cycle["id"])
        flags = load_flags(team_id, cycle["id"], team_view=not is_admin())
        base = team_baseline(team_id, cycle, sub)
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
            "base": base,
            "flags": flags,
            "outcomes": analytics.outcomes(team_id, cycle, sub["answers"], base["value"], adjusted_growth(flags))
            if sub["answers"].get("growth") else None,
            "changelog": list(reversed(changelog)),
        })
    return render_template("history.html", **team_header_context(team, role), entries=entries, active_tab="history")


# ---------------------------------------------------------------- routes: admin

ADMIN_ORDER = ["submitted", "changes_requested", "approved", "draft", "not_started", "signed_off"]


@app.get("/admin")
@admin_required
def admin():
    rows = []
    for team in TEAMS.values():
        sub = load_submission(team["id"])
        flags = [f for f in load_flags(team["id"], CYCLE) if f["active"]]
        rows.append((team, sub, team_baseline(team["id"], cycles.CURRENT, sub), len(flags)))
    rows.sort(key=lambda r: (ADMIN_ORDER.index(r[1]["status"]), r[0]["name"]))
    counts = {status: 0 for status in workflow.STATUS_LABELS}
    for _, sub, _, _ in rows:
        counts[sub["status"]] += 1
    log = store.recent_audit(get_db(), 50)
    return render_template("admin.html", rows=rows, counts=counts, log=log, active_admin="overview")


@app.get("/admin/plan")
@admin_required
def admin_plan():
    cycle = cycles.CURRENT
    rows = []
    for team in TEAMS.values():
        msu = analytics.monthly_msu(team["id"])
        sub = load_submission(team["id"])
        rows.append({
            "team": team, "sub": sub,
            "candidates": analytics.baseline_candidates(msu, cycle),
            "plan": plan_baseline(team["id"], cycle, msu),
            "effective": team_baseline(team["id"], cycle, sub, msu),
            "locked": sub["status"] in LOCKED,
        })
    methods = analytics.BASELINE_METHODS
    return render_template(
        "admin_plan.html", rows=rows, methods=methods, computed=analytics.COMPUTED_METHODS,
        bases={m: analytics.baseline_basis(cycle, m) for m in methods},
        cycle_default=cycle_default_method(cycle), cycle_setting=load_setting(CYCLE_DEFAULT, CYCLE),
        file_default=cycle["default_baseline"], active_admin="plan",
    )


@app.post("/admin/plan")
@admin_required
def admin_plan_save():
    """Sets the cycle-wide default starting point, or one team's."""
    scope = request.form.get("scope", "")
    method = request.form.get("method", "")
    note = request.form.get("note", "").strip()[:FLAG_MAX_TEXT] or None
    db, user = get_db(), current_user()

    if scope == "*":
        if method not in analytics.COMPUTED_METHODS:
            abort(400, "Pick a calculation for the cycle default.")
        old = cycle_default_method(cycles.CURRENT)
        store.save_setting(db, CYCLE, CYCLE_DEFAULT, method, None, note, user["upn"])
        db.commit()
        audit("plan_default", detail=f"{old} -> {method}" + (f": {note}" if note else ""))
        locked = [t["name"] for t in TEAMS.values()
                  if not load_setting(t["id"], CYCLE) and load_submission(t["id"])["status"] in LOCKED]
        msg = f"Cycle default set to {analytics.BASELINE_METHODS[method]['label'].lower()}."
        if locked:
            msg += (f" {', '.join(locked)} already submitted and keep the baseline they submitted against"
                    " until you send them back.")
        flash(msg, "ok")
        return redirect(url_for("admin_plan"))

    team = TEAMS.get(scope) or abort(404)
    sub = load_submission(scope)
    if sub["status"] in LOCKED:
        abort(409, f"{team['name']} has already submitted against its current baseline. "
                   "Send the review back for changes (or reopen it) before changing the starting point.")
    old = plan_baseline(scope, cycles.CURRENT)
    if method == "":  # back to the cycle default
        store.delete_setting(db, CYCLE, scope)
    elif method in analytics.BASELINE_METHODS:
        value = None
        if method == "manual":
            try:
                value = round(float(request.form.get("value", "")), 1)
            except ValueError:
                value = None
            if value is None or not 0 < value < 1e7:
                flash(f"Enter the manual baseline for {team['name']} in MSU.", "error")
                return redirect(url_for("admin_plan") + f"#plan-{scope}")
        if method == "manual" and not note:
            flash(f"Add a note explaining the manual baseline for {team['name']}.", "error")
            return redirect(url_for("admin_plan") + f"#plan-{scope}")
        store.save_setting(db, CYCLE, scope, method, value, note, user["upn"])
    else:
        abort(400)
    db.commit()
    new = plan_baseline(scope, cycles.CURRENT)
    audit("plan_baseline", scope, f"{old['method']} {old['value']} -> {new['method']} {new['value']}"
                                  + (f": {note}" if note else ""))
    flash(f"{team['name']}: baseline is now {new['value']} MSU ({new['basis']}).", "ok")
    return redirect(url_for("admin_plan") + f"#plan-{scope}")


def csv_safe(value):
    """Stops spreadsheet apps treating free-text answers as formulas (e.g. '=HYPERLINK(...)')."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(value)
            return value  # a plain negative number like "-5"
        except ValueError:
            return "'" + value
    return value


def csv_response(rows, filename):
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow([csv_safe(v) for v in row])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/admin/export.csv")
@admin_required
def admin_export():
    header = ["team_id", "team", "status", "submitted_at", "reviewed_by", "reviewed_at", "review_note",
              "signed_off_by", "signed_off_at", "baseline_msu", "baseline_basis", "open_flags"]
    for q in ALL_QUESTIONS:
        header += [f"{q['id']}_{qt}" for qt in QUARTERS] if q["type"] == "quarters" else [q["id"]]
    # What Capacity Planning plans with: the team's % unless a flag adjusted it, and the MSU that implies.
    header += [f"planning_growth_{qt}" for qt in QUARTERS] + [f"planning_msu_{qt}" for qt in QUARTERS]
    rows = [header]
    for team in TEAMS.values():
        sub = load_submission(team["id"])
        a = sub["answers"]
        base = team_baseline(team["id"], cycles.CURRENT, sub)
        flags = load_flags(team["id"], CYCLE)
        row = [team["id"], team["name"], sub["status"]]
        row += [sub.get(k) or "" for k in ("submitted_at", "reviewed_by", "reviewed_at", "review_note",
                                           "signed_off_by", "signed_off_at")]
        row += [base["value"], base["basis"], sum(f["active"] for f in flags)]
        for q in ALL_QUESTIONS:
            if q["type"] == "quarters":
                row += a.get(q["id"]) or [""] * len(QUARTERS)
            else:
                row.append(display_value(q, a, QUARTERS))  # spells out "Other: …"
        team_growth = (a.get("growth") or [""] * len(QUARTERS))
        planning = [adj if adj is not None else analytics._num(raw)
                    for adj, raw in zip(adjusted_growth(flags), team_growth)]
        row += ["" if p is None else p for p in planning]
        row += ["" if p is None else analytics.msu_for(base["value"], p) for p in planning]
        rows.append(row)
    audit("export_csv")
    return csv_response(rows, f"capacity-review-{CYCLE}.csv")


@app.get("/admin/flags.csv")
@admin_required
def admin_flags_export():
    """Every flag this cycle, cleared ones included, with the team's original and the adjusted value."""
    rows = [["team_id", "team", "answer", "team_original", "adjusted", "comment", "shared_with_team",
             "flagged_by", "flagged_at", "status", "cleared_by", "cleared_at", "clear_note"]]
    for team in TEAMS.values():
        for f in reversed(load_flags(team["id"], CYCLE)):
            rows.append([team["id"], team["name"], f["target_label"], f["original"] or "", f["adjusted"] or "",
                         f["comment"], "yes" if f["visible_to_team"] else "no", f["created_name"], f["created_at"],
                         "open" if f["active"] else "cleared", f["resolved_name"] or "", f["resolved_at"] or "",
                         f["resolution_note"] or ""])
    audit("export_flags_csv")
    return csv_response(rows, f"capacity-review-flags-{CYCLE}.csv")


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
