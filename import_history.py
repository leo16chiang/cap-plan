"""One-time import of past capacity reviews (e.g. the cycles run over email) from a CSV file.

    python import_history.py --template 2026-H1 > past.csv   # header row for a cycle, to fill in
    python import_history.py past.csv --dry-run              # check the file, change nothing
    python import_history.py past.csv                        # import

One row per team per cycle. Each row becomes a signed-off submission in that cycle, with a single
"Imported from past records" entry in its change log, so the History tab, the look-back callout
and the "your previous answers" reference on the form all pick it up.

Columns (only team_id, cycle and the growth columns are required):
  team_id          an id from sample_data/teams.json, e.g. PAY
  cycle            an id from cycles.py, e.g. 2026-H1 (add older cycles to cycles.CYCLES first)
  growth_<quarter> % change for each of that cycle's quarters, e.g. growth_Q2 2026
                   (growth_1 … growth_4 also work)
  baseline_msu     the baseline MSU the team's % were made against. Leave blank to calculate it
                   from usage data with the cycle's default_baseline method.
  signed_off_by    email of whoever signed off (default: imported@capacity-review)
  signed_off_name  their name
  signed_off_at    date or ISO timestamp (default: the cycle's first month)
  <question id>    any other question from questions.py: driver, confidence, changes, ...
                   Checkbox answers are separated by ";". A choice that isn't one of the options
                   is stored as "Other" with the text in the Other box.

Existing submissions are left alone (use --replace to overwrite one that was itself imported).
Set PORTAL_DB to import into a database other than instance/portal.db.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone

import analytics
import cycles
import workflow
from app import TEAMS, apply_event, init_db
from questions import ALL_QUESTIONS, OTHER, other_key

DEFAULT_USER = "imported@capacity-review"


def template_header(cycle_id):
    cycle = cycles.BY_ID[cycle_id]
    cols = ["team_id", "cycle", *[f"growth_{q['label']}" for q in cycle["quarters"]], "baseline_msu",
            "signed_off_by", "signed_off_name", "signed_off_at"]
    return cols + [q["id"] for q in ALL_QUESTIONS if q["id"] != "growth"]


def parse_when(raw, cycle):
    raw = (raw or "").strip()
    if not raw:
        y, m = cycle["start_month"]
        return datetime(y, m, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def row_answers(row, cycle):
    """CSV row -> answers dict in the same shape the form produces, plus a list of problems."""
    problems, answers = [], {}
    growth = []
    for i, quarter in enumerate(cycle["quarters"]):
        raw = (row.get(f"growth_{quarter['label']}") or row.get(f"growth_{i + 1}") or "").strip().rstrip("%")
        if raw:
            try:
                float(raw)
            except ValueError:
                problems.append(f"growth for {quarter['label']} is not a number: {raw!r}")
        growth.append(raw)
    if not any(growth):
        problems.append("no growth percentages")
    answers["growth"] = growth

    for q in ALL_QUESTIONS:
        if q["type"] == "quarters":
            continue
        raw = (row.get(q["id"]) or "").strip()
        if q["type"] == "checkboxes":
            picked, others = [], []
            for v in filter(None, (p.strip() for p in raw.split(";"))):
                (picked if v in q["options"] else others).append(v)
            if others and q.get("has_other"):
                picked.append(OTHER)
                answers[other_key(q["id"])] = "; ".join(others)
            elif others:
                problems.append(f"{q['id']}: not an option: {others}")
            answers[q["id"]] = picked
        elif q["type"] in ("select", "radio"):
            if raw and raw not in q["options"]:
                if q.get("has_other"):
                    answers[other_key(q["id"])] = raw
                    raw = OTHER
                else:
                    problems.append(f"{q['id']}: not an option: {raw!r}")
                    raw = ""
            answers[q["id"]] = raw
        else:
            answers[q["id"]] = raw
        if q.get("has_other"):
            answers.setdefault(other_key(q["id"]), "")
    return answers, problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", nargs="?", help="CSV file to import")
    parser.add_argument("--template", metavar="CYCLE", help="print the header row for a cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="validate only; write nothing")
    parser.add_argument("--replace", action="store_true", help="overwrite submissions that were imported earlier")
    args = parser.parse_args(argv)

    if args.template:
        if args.template not in cycles.BY_ID:
            parser.error(f"unknown cycle {args.template}; known: {', '.join(cycles.BY_ID)}")
        csv.writer(sys.stdout).writerow(template_header(args.template))
        return 0
    if not args.csv:
        parser.error("give a CSV file, or --template CYCLE")

    import sqlite3
    from app import DB_PATH
    init_db()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    imported = skipped = failed = 0
    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        for line, row in enumerate(csv.DictReader(fh), start=2):
            row = {(k or "").strip(): v for k, v in row.items()}
            team_id, cycle_id = (row.get("team_id") or "").strip(), (row.get("cycle") or "").strip()
            where = f"line {line} ({team_id or '?'} {cycle_id or '?'})"
            if team_id not in TEAMS:
                print(f"{where}: unknown team_id"); failed += 1; continue
            if cycle_id not in cycles.BY_ID:
                print(f"{where}: unknown cycle; add it to cycles.CYCLES first"); failed += 1; continue
            if cycle_id == cycles.CURRENT["id"]:
                print(f"{where}: that's the open cycle; teams fill it in through the portal"); failed += 1; continue
            cycle = cycles.BY_ID[cycle_id]

            answers, problems = row_answers(row, cycle)
            base_raw = (row.get("baseline_msu") or "").strip()
            if base_raw:
                try:
                    base = (round(float(base_raw), 1), "Imported baseline")
                except ValueError:
                    problems.append(f"baseline_msu is not a number: {base_raw!r}")
                    base = None
            else:
                method = cycle["default_baseline"]
                value = analytics.computed_baseline(analytics.monthly_msu(team_id), cycle, method)
                if value is None:
                    problems.append("no baseline_msu and no usage data to calculate one")
                base = (value, analytics.baseline_basis(cycle, method))
            try:
                at = parse_when(row.get("signed_off_at"), cycle)
            except ValueError:
                problems.append(f"signed_off_at is not a date: {row.get('signed_off_at')!r}")
            if problems:
                print(f"{where}: " + "; ".join(problems)); failed += 1; continue

            existing = db.execute("SELECT status FROM submissions WHERE team_id = ? AND cycle = ?",
                                  (team_id, cycle_id)).fetchone()
            if existing:
                was_imported = db.execute(
                    "SELECT 1 FROM submission_versions WHERE team_id = ? AND cycle = ? AND event = 'import'",
                    (team_id, cycle_id)).fetchone()
                if not (args.replace and was_imported):
                    print(f"{where}: already has a submission ({existing['status']}); skipped"
                          + ("" if was_imported else " (not an import, so --replace won't touch it)"))
                    skipped += 1
                    continue
                if not args.dry_run:
                    for table in ("submissions", "submission_versions"):
                        db.execute(f"DELETE FROM {table} WHERE team_id = ? AND cycle = ?", (team_id, cycle_id))

            by = (row.get("signed_off_by") or "").strip().lower() or DEFAULT_USER
            name = (row.get("signed_off_name") or "").strip() or by
            if args.dry_run:
                print(f"{where}: OK (baseline {base[0]} MSU, growth {answers['growth']})")
            elif not apply_event(db, team_id, cycle_id, "import", by, name, at, answers=answers,
                                 note=f"Imported from {args.csv}", baseline=base):
                print(f"{where}: could not import"); failed += 1; db.rollback(); continue
            else:
                db.execute("INSERT INTO audit_log (at, user, action, team_id, cycle, detail) VALUES (?, ?, ?, ?, ?, ?)",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"), "import_history.py",
                            "import", team_id, cycle_id, json.dumps({"file": args.csv, "line": line})))
                db.commit()
            imported += 1

    verb = "would import" if args.dry_run else "imported"
    print(f"\n{verb} {imported}, skipped {skipped}, failed {failed}")
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
