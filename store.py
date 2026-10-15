"""Reads and writes for the portal's tables (see sql/sqlserver/01_schema.sql).

The rest of the app works with a submission's answers as one dict, the shape questions.py parses
from the form: {"growth": ["5", "4", "", "2"], "driver": "...", "changes": ["...", "..."], ...}.
This module maps that dict to the answers and growth_projections tables and back.
"""
import json
from datetime import datetime, timezone

import cycles
import workflow
from questions import ALL_QUESTIONS, other_key

CHOICE_SEP = "; "  # checkbox picks in answers.value_text


def now():
    """UTC, ISO 8601 without offset: what DATETIME2 and SQLite text both store and sort correctly."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value):
    return "" if value is None else f"{value:g}"


def audit(db, user_upn, action, team_id=None, cycle_id=None, detail=""):
    db.insert("audit_log", {"logged_at": now(), "user_upn": user_upn, "action": action,
                            "team_id": team_id, "cycle_id": cycle_id, "detail": (detail or "")[:2000]})
    db.commit()


def recent_audit(db, n=50):
    return db.latest("audit_log", "log_id", n)


# ---------------------------------------------------------------- answers

def read_answers(db, submission_id):
    rows = {r["question_id"]: r for r in db.query(
        "SELECT question_id, value_text, other_text FROM answers WHERE submission_id = ?", (submission_id,))}
    growth = {r["quarter_no"]: r["team_pct"] for r in db.query(
        "SELECT quarter_no, team_pct FROM growth_projections WHERE submission_id = ?", (submission_id,))}
    answers = {}
    for q in ALL_QUESTIONS:
        qid = q["id"]
        if q["type"] == "quarters":
            answers[qid] = [_fmt_pct(growth.get(i + 1)) for i in range(4)]
            continue
        row = rows.pop(qid, None) or {}
        value = row.get("value_text") or ""
        answers[qid] = value.split(CHOICE_SEP) if q["type"] == "checkboxes" and value else (
            [] if q["type"] == "checkboxes" else value)
        if q.get("has_other"):
            answers[other_key(qid)] = row.get("other_text") or ""
    for qid, row in rows.items():  # answers to questions no longer in questions.py
        answers[qid] = row["value_text"] or ""
    return answers


def write_answers(db, submission_id, cycle_id, answers, baseline_msu):
    """Replaces a submission's stored answers with the given dict."""
    db.execute("DELETE FROM answers WHERE submission_id = ?", (submission_id,))
    db.execute("DELETE FROM growth_projections WHERE submission_id = ?", (submission_id,))
    cycle = cycles.BY_ID[cycle_id]
    for q in ALL_QUESTIONS:
        qid, value = q["id"], answers.get(q["id"])
        if q["type"] == "quarters":
            for i, quarter in enumerate(cycle["quarters"]):
                pct = _num((value or [])[i] if i < len(value or []) else None)
                msu = round(baseline_msu * (1 + pct / 100), 1) if pct is not None and baseline_msu is not None else None
                y, m = quarter["months"][0]
                db.insert("growth_projections", {
                    "submission_id": submission_id, "quarter_no": i + 1, "quarter_label": quarter["label"],
                    "quarter_start": f"{y:04d}-{m:02d}-01", "team_pct": pct, "team_msu": msu})
            continue
        text = CHOICE_SEP.join(value) if isinstance(value, list) else (value or "")
        other = (answers.get(other_key(qid)) or "") if q.get("has_other") else ""
        if text or other:
            db.insert("answers", {"submission_id": submission_id, "question_id": qid,
                                  "value_text": text or None, "other_text": other or None})


# ---------------------------------------------------------------- submissions

def _row(db, team_id, cycle_id):
    return db.one("SELECT * FROM submissions WHERE team_id = ? AND cycle_id = ?", (team_id, cycle_id))


def load_submission(db, team_id, cycle_id):
    row = _row(db, team_id, cycle_id)
    if row is None:
        return {"status": "not_started", "answers": {}, "submission_id": None, "baseline_msu": None}
    row["answers"] = read_answers(db, row["submission_id"])
    return row


def load_events(db, team_id, cycle_id):
    rows = db.query(
        """SELECT e.* FROM submission_events e JOIN submissions s ON s.submission_id = e.submission_id
           WHERE s.team_id = ? AND s.cycle_id = ? ORDER BY e.version""", (team_id, cycle_id))
    for r in rows:
        r["answers"] = json.loads(r.pop("answers_json"))
    return rows


def apply_event(db, team_id, cycle_id, event, by_user, by_name, at, answers=None, note=None, baseline=None):
    """Moves a submission through the workflow and records the event.

    baseline: (MSU, method, basis) the answers were made against; stored with them so later changes
    to Plan settings don't rewrite what the team saw. Returns False (and the caller rolls back) if
    the submission's status doesn't allow the event, including when someone changed it a moment ago.
    """
    spec = workflow.EVENTS[event]
    row = _row(db, team_id, cycle_id)
    current = row["status"] if row else "not_started"
    if current not in spec["from"]:
        return False

    new_status = workflow.next_status(event, current)
    fields = {"status": new_status}
    if answers is not None:
        fields.update(updated_by=by_user, updated_at=at)
    if baseline is not None:
        fields.update(baseline_msu=baseline[0], baseline_method=baseline[1], baseline_basis=baseline[2])
    if event == "submit":
        fields.update(submitted_by=by_user, submitted_at=at)
    if event in ("approve", "request_changes"):
        fields.update(reviewed_by=by_user, reviewed_name=by_name, reviewed_at=at, review_note=note)
    if event in ("signoff", "import"):
        fields.update(signed_off_by=by_user, signed_off_name=by_name, signed_off_at=at)
    if event == "reopen":
        fields.update(signed_off_by=None, signed_off_name=None, signed_off_at=None,
                      reviewed_by=None, reviewed_name=None, reviewed_at=None, review_note=None)

    try:
        if row is None:
            submission_id = db.insert("submissions", {"team_id": team_id, "cycle_id": cycle_id, **fields},
                                      id_column="submission_id")
        else:
            submission_id = row["submission_id"]
            # "AND status = ?" makes this a compare-and-set: it fails if the status moved underneath us.
            changed = db.execute(
                f"UPDATE submissions SET {', '.join(f'{k} = ?' for k in fields)} "
                "WHERE submission_id = ? AND status = ?",
                [*fields.values(), submission_id, current])
            if changed != 1:
                return False

        base_msu = baseline[0] if baseline is not None else (row or {}).get("baseline_msu")
        if answers is not None:
            write_answers(db, submission_id, cycle_id, answers, base_msu)
        snapshot = answers if answers is not None else read_answers(db, submission_id)
        db.execute(
            """INSERT INTO submission_events (submission_id, version, event, status_after, baseline_msu,
                                              by_user, by_name, event_at, note, answers_json)
               SELECT ?, COALESCE(MAX(version), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?
               FROM submission_events WHERE submission_id = ?""",
            (submission_id, event, new_status, base_msu, by_user, by_name, at, note, json.dumps(snapshot),
             submission_id))
    except db.IntegrityError:  # another request created the row or the version first
        return False
    return True


# ---------------------------------------------------------------- plan settings

def _team_clause(team_id):
    return ("team_id IS NULL", ()) if team_id is None else ("team_id = ?", (team_id,))


def load_setting(db, cycle_id, team_id):
    """team_id None = the cycle-wide default."""
    clause, params = _team_clause(team_id)
    return db.one(f"SELECT * FROM plan_settings WHERE cycle_id = ? AND {clause}", (cycle_id, *params))


def save_setting(db, cycle_id, team_id, method, manual_msu, note, by_user):
    clause, params = _team_clause(team_id)
    values = {"baseline_method": method, "manual_msu": manual_msu, "note": note,
              "updated_by": by_user, "updated_at": now()}
    changed = db.execute(
        f"UPDATE plan_settings SET {', '.join(f'{k} = ?' for k in values)} WHERE cycle_id = ? AND {clause}",
        (*values.values(), cycle_id, *params))
    if not changed:
        db.insert("plan_settings", {"cycle_id": cycle_id, "team_id": team_id, **values})


def delete_setting(db, cycle_id, team_id):
    clause, params = _team_clause(team_id)
    db.execute(f"DELETE FROM plan_settings WHERE cycle_id = ? AND {clause}", (cycle_id, *params))


# ---------------------------------------------------------------- flags
# The app indexes quarters from 0 ("growth:0"); the tables number them 1-4 like growth_projections.

def load_flags(db, team_id, cycle_id, team_view=False):
    sql = """SELECT f.* FROM answer_flags f JOIN submissions s ON s.submission_id = f.submission_id
             WHERE s.team_id = ? AND s.cycle_id = ?"""
    if team_view:
        sql += " AND f.visible_to_team = 1"
    out = []
    for r in db.query(sql + " ORDER BY f.flag_id DESC", (team_id, cycle_id)):
        out.append({
            "id": r["flag_id"], "question_id": r["question_id"],
            "quarter": None if r["quarter_no"] is None else r["quarter_no"] - 1,
            "original": r["original_value"], "adjusted": r["adjusted_value"], "adjusted_pct": r["adjusted_pct"],
            "comment": r["comment"], "visible_to_team": bool(r["visible_to_team"]),
            "created_by": r["created_by"], "created_name": r["created_name"], "created_at": r["created_at"],
            "resolved_by": r["resolved_by"], "resolved_name": r["resolved_name"], "resolved_at": r["resolved_at"],
            "resolution": r["resolution"], "resolution_note": r["resolution_note"],
        })
    return out


def add_flag(db, submission_id, question_id, quarter, original, adjusted, comment, visible, by_user, by_name):
    """Opens a flag; an open flag on the same answer is closed as superseded (kept, not deleted)."""
    at = now()
    quarter_no = None if quarter is None else quarter + 1
    q_clause, q_params = ("quarter_no IS NULL", ()) if quarter_no is None else ("quarter_no = ?", (quarter_no,))
    db.execute(
        f"""UPDATE answer_flags SET resolved_by = ?, resolved_name = ?, resolved_at = ?, resolution = 'superseded'
            WHERE submission_id = ? AND question_id = ? AND {q_clause} AND resolved_at IS NULL""",
        (by_user, by_name, at, submission_id, question_id, *q_params))
    db.insert("answer_flags", {
        "submission_id": submission_id, "question_id": question_id, "quarter_no": quarter_no,
        "original_value": original, "adjusted_value": adjusted,
        "adjusted_pct": _num(adjusted) if quarter_no is not None else None,
        "comment": comment, "visible_to_team": 1 if visible else 0,
        "created_by": by_user, "created_name": by_name, "created_at": at,
    })


def clear_flag(db, flag_id, team_id, cycle_id, note, by_user, by_name):
    """Returns False if the flag is already closed or isn't on this team's submission."""
    return db.execute(
        """UPDATE answer_flags SET resolved_by = ?, resolved_name = ?, resolved_at = ?, resolution = 'cleared',
                                   resolution_note = ?
           WHERE flag_id = ? AND resolved_at IS NULL
             AND submission_id IN (SELECT submission_id FROM submissions WHERE team_id = ? AND cycle_id = ?)""",
        (by_user, by_name, now(), note, flag_id, team_id, cycle_id)) == 1
