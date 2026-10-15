-- SQLite version of sql/sqlserver/*.sql for the local demo and tests. Keep the two in step.
CREATE TABLE IF NOT EXISTS submissions (
    submission_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id          TEXT NOT NULL,
    cycle_id         TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('draft', 'submitted', 'changes_requested', 'approved', 'signed_off')),
    baseline_msu     REAL,
    baseline_method  TEXT,
    baseline_basis   TEXT,
    updated_by TEXT, updated_at TEXT,
    submitted_by TEXT, submitted_at TEXT,
    reviewed_by TEXT, reviewed_name TEXT, reviewed_at TEXT, review_note TEXT,
    signed_off_by TEXT, signed_off_name TEXT, signed_off_at TEXT,
    UNIQUE (team_id, cycle_id)
);
CREATE TABLE IF NOT EXISTS answers (
    submission_id INTEGER NOT NULL REFERENCES submissions (submission_id),
    question_id   TEXT NOT NULL,
    value_text    TEXT,
    other_text    TEXT,
    PRIMARY KEY (submission_id, question_id)
);
CREATE TABLE IF NOT EXISTS growth_projections (
    submission_id INTEGER NOT NULL REFERENCES submissions (submission_id),
    quarter_no    INTEGER NOT NULL CHECK (quarter_no BETWEEN 1 AND 4),
    quarter_label TEXT NOT NULL,
    quarter_start TEXT NOT NULL,
    team_pct      REAL,
    team_msu      REAL,
    PRIMARY KEY (submission_id, quarter_no)
);
CREATE TABLE IF NOT EXISTS answer_flags (
    flag_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions (submission_id),
    question_id     TEXT NOT NULL,
    quarter_no      INTEGER,
    original_value  TEXT,
    adjusted_value  TEXT,
    adjusted_pct    REAL,
    comment         TEXT NOT NULL,
    visible_to_team INTEGER NOT NULL,
    created_by TEXT NOT NULL, created_name TEXT, created_at TEXT NOT NULL,
    resolved_by TEXT, resolved_name TEXT, resolved_at TEXT,
    resolution      TEXT CHECK (resolution IN ('cleared', 'superseded')),
    resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS ix_answer_flags_submission ON answer_flags (submission_id);
CREATE TABLE IF NOT EXISTS plan_settings (
    setting_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id        TEXT NOT NULL,
    team_id         TEXT,
    baseline_method TEXT NOT NULL,
    manual_msu      REAL,
    note            TEXT,
    updated_by TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions (submission_id),
    version       INTEGER NOT NULL,
    event         TEXT NOT NULL,
    status_after  TEXT NOT NULL,
    baseline_msu  REAL,
    by_user TEXT, by_name TEXT,
    event_at      TEXT NOT NULL,
    note          TEXT,
    answers_json  TEXT NOT NULL,
    UNIQUE (submission_id, version)
);
CREATE TABLE IF NOT EXISTS audit_log (
    log_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    user_upn  TEXT,
    action    TEXT NOT NULL,
    team_id   TEXT,
    cycle_id  TEXT,
    detail    TEXT
);
CREATE VIEW IF NOT EXISTS rpt_v_projections AS
SELECT s.team_id, s.cycle_id, s.status, s.signed_off_at,
       g.quarter_no, g.quarter_label, g.quarter_start,
       s.baseline_msu, s.baseline_basis,
       g.team_pct, g.team_msu,
       COALESCE(f.adjusted_pct, g.team_pct) AS planning_pct,
       CASE WHEN f.adjusted_pct IS NULL THEN g.team_msu
            ELSE ROUND(s.baseline_msu * (1 + f.adjusted_pct / 100), 1) END AS planning_msu,
       CASE WHEN f.adjusted_pct IS NULL THEN 0 ELSE 1 END AS is_adjusted
FROM submissions s
JOIN growth_projections g ON g.submission_id = s.submission_id
LEFT JOIN answer_flags f
       ON f.submission_id = s.submission_id AND f.question_id = 'growth' AND f.quarter_no = g.quarter_no
      AND f.resolved_at IS NULL AND f.adjusted_pct IS NOT NULL;
CREATE VIEW IF NOT EXISTS rpt_v_answers AS
SELECT s.team_id, s.cycle_id, s.status, s.signed_off_at, a.question_id, a.value_text, a.other_text
FROM submissions s
JOIN answers a ON a.submission_id = s.submission_id;
