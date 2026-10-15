import csv
import io

import analytics
import cycles
from conftest import login, portal

ALICE = "alice.nguyen@example.com"
ADMIN = "cap.admin@example.com"


def full_answers(growth=("5", "4", "3", "2")):
    data = {f"growth__{i}": g for i, g in enumerate(growth)}
    data.update({
        "driver": "Business / transaction volume growth",
        "confidence": "Low: rough estimate",
        "changes": "None planned",
        "biggest_change": "Nothing big",
        "batch_window": "No change",
        "meeting": "No, this submission is enough",
    })
    return data


def submit_pay(client, growth=("5", "4", "3", "2")):
    token = login(client, ALICE)
    r = client.post("/team/PAY", data={**full_answers(growth), "action": "submit", "_csrf": token})
    assert r.status_code == 302, r.data
    client.post("/logout", data={"_csrf": token})


def test_default_baseline_is_last_month(client):
    msu = analytics.monthly_msu("PAY")
    with portal.app.test_request_context():
        base = portal.plan_baseline("PAY", cycles.CURRENT)
    assert base["method"] == "last_month"
    assert base["value"] == msu[(2026, 9)]
    assert base["basis"] == "Last month (Sep 26)"


def test_baseline_methods():
    msu = analytics.monthly_msu("PAY")
    cand = analytics.baseline_candidates(msu, cycles.CURRENT)
    assert cand["avg_3m"] == round(sum(msu[(2026, m)] for m in (7, 8, 9)) / 3, 1)
    assert cand["peak_12m"] == max(v for k, v in msu.items() if k >= (2025, 10))


def test_admin_sets_manual_baseline_and_it_locks_on_submit(client):
    token = login(client, ADMIN)
    r = client.post("/admin/plan", data={"scope": "PAY", "method": "manual", "value": "450", "_csrf": token})
    assert r.status_code == 302  # missing note -> flash + redirect, nothing saved
    with portal.app.test_request_context():
        assert portal.plan_baseline("PAY", cycles.CURRENT)["method"] == "last_month"
    client.post("/admin/plan", data={"scope": "PAY", "method": "manual", "value": "450",
                                     "note": "Sep had a one-off outage", "_csrf": token})
    with portal.app.test_request_context():
        assert portal.plan_baseline("PAY", cycles.CURRENT)["value"] == 450
    client.post("/logout", data={"_csrf": token})

    submit_pay(client)
    with portal.app.test_request_context():
        sub = portal.load_submission("PAY")
        assert sub["baseline_msu"] == 450 and sub["baseline_method"] == "manual"

    token = login(client, ADMIN)
    r = client.post("/admin/plan", data={"scope": "PAY", "method": "avg_3m", "_csrf": token})
    assert r.status_code == 409  # locked once submitted
    # A cycle-wide change doesn't re-scale a submitted team either.
    client.post("/admin/plan", data={"scope": "*", "method": "avg_6m", "_csrf": token})
    with portal.app.test_request_context():
        assert portal.team_baseline("PAY", cycles.CURRENT, portal.load_submission("PAY"))["value"] == 450
        assert portal.plan_baseline("CLM", cycles.CURRENT)["method"] == "avg_6m"
    assert client.get("/admin/plan").status_code == 200


def test_flag_keeps_original_and_adjusted(client):
    submit_pay(client)
    token = login(client, ADMIN)
    r = client.post("/team/PAY/flags", data={"target": "growth:1", "adjusted": "8", "comment": "Jan peak",
                                             "visible": "1", "_csrf": token})
    assert r.status_code == 302
    r = client.post("/team/PAY/flags", data={"target": "growth:1", "adjusted": "abc", "comment": "x", "_csrf": token})
    assert r.status_code == 400
    client.post("/team/PAY/flags", data={"target": "driver", "comment": "internal only", "_csrf": token})
    # Re-flagging supersedes but keeps the old row.
    client.post("/team/PAY/flags", data={"target": "growth:1", "adjusted": "9", "comment": "Actually 9",
                                         "visible": "1", "_csrf": token})
    with portal.app.test_request_context():
        flags = portal.load_flags("PAY", portal.CYCLE)
        assert len(flags) == 3
        growth = [f for f in flags if f["question_id"] == "growth"]
        assert [(f["original"], f["adjusted"], f["active"]) for f in growth] == [("4", "9", True), ("4", "8", False)]
        assert portal.adjusted_growth(flags) == [None, 9.0, None, None]
        assert portal.load_submission("PAY")["answers"]["growth"][1] == "4"  # team answer untouched

    export = client.get("/admin/export.csv").data.decode()
    row = next(r for r in csv.DictReader(io.StringIO(export)) if r["team_id"] == "PAY")
    q1 = cycles.CURRENT["quarters"][1]["label"]
    assert row[f"growth_{q1}"] == "4" and row[f"planning_growth_{q1}"] == "9.0"
    flags_csv = list(csv.DictReader(io.StringIO(client.get("/admin/flags.csv").data.decode())))
    assert len(flags_csv) == 3
    page = client.get("/team/PAY").data.decode()
    assert "internal only" in page
    client.post("/logout", data={"_csrf": token})

    token = login(client, ALICE)
    page = client.get("/team/PAY").data.decode()
    assert "Actually 9" in page and "internal only" not in page


def test_flag_clear(client):
    submit_pay(client)
    token = login(client, ADMIN)
    client.post("/team/PAY/flags", data={"target": "growth:0", "comment": "check", "_csrf": token})
    with portal.app.test_request_context():
        fid = portal.load_flags("PAY", portal.CYCLE)[0]["id"]
    assert client.post(f"/team/PAY/flags/{fid}/clear", data={"note": "ok", "_csrf": token}).status_code == 302
    assert client.post(f"/team/PAY/flags/{fid}/clear", data={"_csrf": token}).status_code == 409


def test_team_cannot_flag(client):
    submit_pay(client)
    token = login(client, ALICE)
    r = client.post("/team/PAY/flags", data={"target": "growth:0", "comment": "x", "_csrf": token})
    assert r.status_code == 403


def test_team_page_shows_reference(client):
    token = login(client, ALICE)
    page = client.get("/team/PAY").data.decode()
    assert "Statistical projection" in page
    assert "Your Spring 2026 answer" in page


def test_statistical_projection_follows_trend():
    msu = analytics.monthly_msu("PAY")
    stats = analytics.statistical_projection(msu, cycles.CURRENT, 100)
    assert len(stats["quarters"]) == 4
    # PAY grows ~1%/month with a December peak, so Q4 (Oct–Dec) should be well above Sep's level.
    assert stats["quarters"][0]["msu"] > msu[(2026, 9)]


def test_csv_safe():
    assert portal.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert portal.csv_safe("-5") == "-5"
    assert portal.csv_safe("ok") == "ok"


def test_history_pages_render(client):
    login(client, ADMIN)
    for team in portal.TEAMS:
        assert client.get(f"/team/{team}/history").status_code == 200
        assert client.get(f"/team/{team}").status_code == 200
    assert client.get("/admin").status_code == 200


def test_import_history(tmp_path, fresh_db):
    import import_history
    path = tmp_path / "past.csv"
    header = import_history.template_header("2026-H1")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        row = dict.fromkeys(header, "")
        row.update({"team_id": "PRL", "cycle": "2026-H1", "growth_Q2 2026": "1", "growth_Q3 2026": "2",
                    "growth_Q4 2026": "3", "growth_Q1 2027": "4", "baseline_msu": "95",
                    "driver": "Something new", "signed_off_at": "2026-05-01"})
        w.writerow([row[h] for h in header])
        w.writerow([{"team_id": "PAY", "cycle": "2026-H1"}.get(h, "") for h in header])  # exists + no growth
    assert import_history.main([str(path), "--dry-run"]) == 1
    assert import_history.main([str(path)]) == 1  # PAY row fails, PRL imports
    with portal.app.test_request_context():
        sub = portal.load_submission("PRL", "2026-H1")
        assert sub["status"] == "signed_off" and sub["baseline_msu"] == 95
        assert sub["answers"]["driver"] == "Other" and sub["answers"]["driver__other"] == "Something new"
        assert portal.store.load_events(portal.get_db(), "PRL", "2026-H1")[0]["event"] == "import"
    assert import_history.main([str(path), "--replace"]) == 1


def test_answers_land_in_tables(client):
    """Submitted answers are stored as rows, and round-trip back into the form unchanged."""
    token = login(client, ALICE)
    form = full_answers(("5", "-2.5", "", "10"))
    form.update({"changes": ["Code or SQL tuning initiatives", "Other"], "changes__other": "Db2 13 upgrade",
                 "biggest_change_month": "2027-02"})
    assert client.post("/team/PAY", data={**form, "action": "save", "_csrf": token}).status_code == 302
    with portal.app.test_request_context():
        db = portal.get_db()
        sub = portal.load_submission("PAY")
        sid = sub["submission_id"]
        growth = db.query("SELECT * FROM growth_projections WHERE submission_id = ? ORDER BY quarter_no", (sid,))
        assert [g["team_pct"] for g in growth] == [5, -2.5, None, 10]
        assert growth[0]["quarter_label"] == "Q4 2026" and growth[0]["quarter_start"] == "2026-10-01"
        assert growth[0]["team_msu"] == round(sub["baseline_msu"] * 1.05, 1)
        rows = {r["question_id"]: r for r in db.query("SELECT * FROM answers WHERE submission_id = ?", (sid,))}
        assert rows["changes"]["value_text"] == "Code or SQL tuning initiatives; Other"
        assert rows["changes"]["other_text"] == "Db2 13 upgrade"
        assert "peaks" not in rows  # blank answers aren't stored
        assert sub["answers"]["growth"] == ["5", "-2.5", "", "10"]
        assert sub["answers"]["changes"] == ["Code or SQL tuning initiatives", "Other"]
        assert sub["answers"]["biggest_change_month"] == "2027-02"


def test_save_rejects_text_in_growth(client):
    token = login(client, ALICE)
    r = client.post("/team/PAY", data={**full_answers(("abc", "1", "1", "1")), "action": "save", "_csrf": token})
    assert r.status_code == 400


def test_reporting_view_uses_planning_value(client):
    submit_pay(client)
    token = login(client, ADMIN)
    client.post("/team/PAY/flags", data={"target": "growth:1", "adjusted": "8", "comment": "Jan peak", "_csrf": token})
    client.post("/team/PAY/flags", data={"target": "growth:2", "comment": "comment only", "_csrf": token})
    with portal.app.test_request_context():
        rows = portal.get_db().query(
            "SELECT * FROM rpt_v_projections WHERE team_id = 'PAY' AND cycle_id = ? ORDER BY quarter_no", (portal.CYCLE,))
        assert [r["planning_pct"] for r in rows] == [5, 8, 3, 2]
        assert [r["is_adjusted"] for r in rows] == [0, 1, 0, 0]
        assert rows[1]["planning_msu"] == round(rows[1]["baseline_msu"] * 1.08, 1)
        assert rows[0]["planning_msu"] == rows[0]["team_msu"]


def test_history_events_store_snapshots(client):
    submit_pay(client)
    with portal.app.test_request_context():
        events = portal.store.load_events(portal.get_db(), "PAY", portal.CYCLE)
        assert [e["event"] for e in events] == ["submit"]
        assert events[0]["answers"]["growth"] == ["5", "4", "3", "2"]
        assert events[0]["baseline_msu"] is not None


def test_db2_month_formats():
    import datetime
    import usage
    assert usage._month(datetime.date(2026, 9, 1)) == (2026, 9)
    assert usage._month(datetime.datetime(2026, 9, 30, 12)) == (2026, 9)
    assert usage._month(202609) == (2026, 9)
    assert usage._month("202609") == (2026, 9)
    assert usage._month("2026-09-01") == (2026, 9)
