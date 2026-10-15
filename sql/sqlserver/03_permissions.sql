/* Roles. Add the real principals at the bottom: for example the app's Entra managed identity or
   service principal (CREATE USER [name] FROM EXTERNAL PROVIDER) or a Windows/SQL login, per your DBA standards. */
CREATE ROLE cap_review_app;           -- the portal itself
GRANT SELECT, INSERT, UPDATE, DELETE ON SCHEMA::dbo TO cap_review_app;
GRANT SELECT ON SCHEMA::rpt TO cap_review_app;

CREATE ROLE cap_review_reporting;     -- Power BI, analysts: the views only, no tables
GRANT SELECT ON SCHEMA::rpt TO cap_review_reporting;

-- ALTER ROLE cap_review_app ADD MEMBER [<app identity>];
-- ALTER ROLE cap_review_reporting ADD MEMBER [<reporting group>];
