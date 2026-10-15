/* Read-only reporting surface. Reporting users get SELECT on schema rpt only (03_permissions.sql);
   because rpt and dbo have the same owner, the views can read dbo tables without granting them. */
CREATE SCHEMA rpt AUTHORIZATION dbo;
GO

-- One row per team, cycle and projected quarter: the team's projection and the value Capacity
-- Planning plans with (the team's, unless an open flag adjusted it). Join to Db2 actuals on
-- team_id + quarter_start. Filter on status = 'signed_off' for final numbers.
CREATE VIEW rpt.v_projections AS
SELECT s.team_id, s.cycle_id, s.status, s.signed_off_at,
       g.quarter_no, g.quarter_label, g.quarter_start,
       s.baseline_msu, s.baseline_basis,
       g.team_pct, g.team_msu,
       COALESCE(f.adjusted_pct, g.team_pct) AS planning_pct,
       CASE WHEN f.adjusted_pct IS NULL THEN g.team_msu
            ELSE CAST(ROUND(s.baseline_msu * (1 + f.adjusted_pct / 100), 1) AS DECIMAL(10,1)) END AS planning_msu,
       CASE WHEN f.adjusted_pct IS NULL THEN 0 ELSE 1 END AS is_adjusted
FROM dbo.submissions s
JOIN dbo.growth_projections g ON g.submission_id = s.submission_id
LEFT JOIN dbo.answer_flags f
       ON f.submission_id = s.submission_id AND f.question_id = 'growth' AND f.quarter_no = g.quarter_no
      AND f.resolved_at IS NULL AND f.adjusted_pct IS NOT NULL;
GO

-- Every other answer, one row per team, cycle and question.
CREATE VIEW rpt.v_answers AS
SELECT s.team_id, s.cycle_id, s.status, s.signed_off_at, a.question_id, a.value_text, a.other_text
FROM dbo.submissions s
JOIN dbo.answers a ON a.submission_id = s.submission_id;
GO
