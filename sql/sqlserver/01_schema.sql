/* Mainframe Capacity Review portal: application tables (SQL Server 2016 or later).
   Run once per environment as a DBA, then 02_reporting_views.sql and 03_permissions.sql.
   Only the app's service account writes to these tables; reporting reads the rpt views. */

-- One row per team per cycle: workflow state and the baseline the team's % answers are measured against.
CREATE TABLE dbo.submissions (
    submission_id    INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_submissions PRIMARY KEY,
    team_id          VARCHAR(20)    NOT NULL,
    cycle_id         VARCHAR(10)    NOT NULL,          -- '2026-H2' (cycles live in cycles.py)
    status           VARCHAR(20)    NOT NULL,
    baseline_msu     DECIMAL(10,1)  NULL,              -- locked when the team submits
    baseline_method  VARCHAR(20)    NULL,              -- last_month | avg_3m | avg_6m | peak_12m | manual
    baseline_basis   NVARCHAR(100)  NULL,              -- 'Last month (Sep 26)'
    updated_by       NVARCHAR(256)  NULL,
    updated_at       DATETIME2(0)   NULL,              -- all timestamps are UTC
    submitted_by     NVARCHAR(256)  NULL,
    submitted_at     DATETIME2(0)   NULL,
    reviewed_by      NVARCHAR(256)  NULL,
    reviewed_name    NVARCHAR(200)  NULL,
    reviewed_at      DATETIME2(0)   NULL,
    review_note      NVARCHAR(1000) NULL,
    signed_off_by    NVARCHAR(256)  NULL,
    signed_off_name  NVARCHAR(200)  NULL,
    signed_off_at    DATETIME2(0)   NULL,
    CONSTRAINT uq_submissions_team_cycle UNIQUE (team_id, cycle_id),
    CONSTRAINT ck_submissions_status CHECK (status IN
        ('draft', 'submitted', 'changes_requested', 'approved', 'signed_off'))
);

-- Every question except growth: one row per answered question. Checkbox picks are '; '-separated.
CREATE TABLE dbo.answers (
    submission_id  INT            NOT NULL CONSTRAINT fk_answers_submission REFERENCES dbo.submissions (submission_id),
    question_id    VARCHAR(50)    NOT NULL,            -- id from questions.py
    value_text     NVARCHAR(4000) NULL,
    other_text     NVARCHAR(4000) NULL,                -- the "Other: ..." box
    CONSTRAINT pk_answers PRIMARY KEY (submission_id, question_id)
);

-- The growth question: one row per projected quarter.
CREATE TABLE dbo.growth_projections (
    submission_id  INT            NOT NULL CONSTRAINT fk_growth_submission REFERENCES dbo.submissions (submission_id),
    quarter_no     TINYINT        NOT NULL CONSTRAINT ck_growth_quarter CHECK (quarter_no BETWEEN 1 AND 4),
    quarter_label  VARCHAR(10)    NOT NULL,            -- 'Q4 2026'
    quarter_start  DATE           NOT NULL,            -- first month of the quarter; join key to Db2 usage
    team_pct       DECIMAL(7,2)   NULL,                -- % change vs baseline_msu, as the team entered it
    team_msu       DECIMAL(10,1)  NULL,                -- baseline_msu * (1 + team_pct / 100)
    CONSTRAINT pk_growth_projections PRIMARY KEY (submission_id, quarter_no)
);

-- Capacity Planning flags. The team's answer is never edited: original_value is the answer when
-- flagged, adjusted_value the value Capacity Planning plans with. Rows are closed, never deleted.
CREATE TABLE dbo.answer_flags (
    flag_id          INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_answer_flags PRIMARY KEY,
    submission_id    INT            NOT NULL CONSTRAINT fk_flags_submission REFERENCES dbo.submissions (submission_id),
    question_id      VARCHAR(50)    NOT NULL,
    quarter_no       TINYINT        NULL,              -- set for growth flags
    original_value   NVARCHAR(4000) NULL,
    adjusted_value   NVARCHAR(1000) NULL,
    adjusted_pct     DECIMAL(7,2)   NULL,              -- numeric copy for growth flags
    comment          NVARCHAR(1000) NOT NULL,
    visible_to_team  BIT            NOT NULL,
    created_by       NVARCHAR(256)  NOT NULL,
    created_name     NVARCHAR(200)  NULL,
    created_at       DATETIME2(0)   NOT NULL,
    resolved_by      NVARCHAR(256)  NULL,
    resolved_name    NVARCHAR(200)  NULL,
    resolved_at      DATETIME2(0)   NULL,
    resolution       VARCHAR(20)    NULL CONSTRAINT ck_flags_resolution CHECK (resolution IN ('cleared', 'superseded')),
    resolution_note  NVARCHAR(1000) NULL
);
CREATE INDEX ix_answer_flags_submission ON dbo.answer_flags (submission_id);
-- At most one open flag per answer (SQL Server treats NULL quarter_no values as equal here).
CREATE UNIQUE INDEX uq_answer_flags_open ON dbo.answer_flags (submission_id, question_id, quarter_no)
    WHERE resolved_at IS NULL;

-- Capacity Planning's starting point. team_id NULL = the cycle-wide default.
CREATE TABLE dbo.plan_settings (
    setting_id       INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_plan_settings PRIMARY KEY,
    cycle_id         VARCHAR(10)    NOT NULL,
    team_id          VARCHAR(20)    NULL,
    baseline_method  VARCHAR(20)    NOT NULL,
    manual_msu       DECIMAL(10,1)  NULL,              -- only when baseline_method = 'manual'
    note             NVARCHAR(1000) NULL,
    updated_by       NVARCHAR(256)  NOT NULL,
    updated_at       DATETIME2(0)   NOT NULL
);
CREATE UNIQUE INDEX uq_plan_settings ON dbo.plan_settings (cycle_id, team_id);  -- one NULL (default) per cycle

-- Workflow history: every save, submit, review, sign-off, reopen and import, with a JSON copy of the
-- answers at that moment (powers the History tab's change log).
CREATE TABLE dbo.submission_events (
    event_id       INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_submission_events PRIMARY KEY,
    submission_id  INT            NOT NULL CONSTRAINT fk_events_submission REFERENCES dbo.submissions (submission_id),
    version        INT            NOT NULL,
    event          VARCHAR(20)    NOT NULL,
    status_after   VARCHAR(20)    NOT NULL,
    baseline_msu   DECIMAL(10,1)  NULL,
    by_user        NVARCHAR(256)  NULL,
    by_name        NVARCHAR(200)  NULL,
    event_at       DATETIME2(0)   NOT NULL,
    note           NVARCHAR(1000) NULL,
    answers_json   NVARCHAR(MAX)  NOT NULL CONSTRAINT ck_events_json CHECK (ISJSON(answers_json) = 1),
    CONSTRAINT uq_submission_events_version UNIQUE (submission_id, version)
);

-- Sign-ins, actions, denied attempts, exports, setting changes. App-only.
CREATE TABLE dbo.audit_log (
    log_id     INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_audit_log PRIMARY KEY,
    logged_at  DATETIME2(0)   NOT NULL,
    user_upn   NVARCHAR(256)  NULL,
    action     VARCHAR(50)    NOT NULL,
    team_id    VARCHAR(20)    NULL,
    cycle_id   VARCHAR(10)    NULL,
    detail     NVARCHAR(2000) NULL
);
CREATE INDEX ix_audit_log_logged_at ON dbo.audit_log (logged_at);
