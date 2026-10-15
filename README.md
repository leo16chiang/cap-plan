# Mainframe Capacity Review portal (sample)

A small Flask app that replaces the twice-a-year capacity-review emails. Each app team signs in,
sees **only its own application's** usage chart and questionnaire, and a member of the team's
signer group signs off. Capacity Planning gets a status overview, an audit log and a CSV export.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5057. Locally the app uses a SQLite file at `instance/portal.db` (delete it to start
over); production uses SQL Server, see [Data](#data).

> Windows note: the Microsoft Store build of Python can't read files under `AppData\Roaming` and
> has trouble with very long paths. Keep the project in a normal folder (e.g. `C:\dev\capacity_portal`).

## Demo accounts (AUTH_MODE=mock, the default)

The login page lists them. The shared password is in `sample_data/mock_entra_directory.json`.

| Account | What it shows |
|---|---|
| alice.nguyen | Signer for Payments Core, viewer on Card Authorization, no access to anything else |
| bob.martin | Signer for Claims Processing, viewer on Payments Core |
| carla.diaz | Signer for two teams: gets an app picker |
| dev.patel | Viewer only: form is read-only, sign-off is refused |
| erin.walsh | Valid login, no groups: "no application assigned" page |
| cap.admin | Capacity Planning: review queue, approve or request changes, reopen, plan settings, flags, audit log, CSV export |

## How access works

1. **Who you are:** Entra ID sign-in (simulated in demo mode). The app gets `oid`, UPN, name and
   the `groups` claim (group object IDs).
2. **Which team:** `sample_data/teams.json` maps each application to two Entra security groups:
   `SG-CAPREVIEW-<APP>-SIGNERS` (edit and sign off) and `SG-CAPREVIEW-<APP>-VIEWERS` (read-only).
   Users only see applications they're in a group for; any other team page returns 403.
3. **Everything is enforced on the server** (`team_submit` / `team_review` in `app.py`): group
   membership is re-checked on every write, identities come from the session (never the form),
   CSRF tokens are required, status changes are compare-and-set in SQL so two people can't race,
   and denied attempts are written to the audit log.

## Review workflow

Defined in `workflow.py`:

```
draft ──submit──▶ submitted ──approve──▶ approved ──signoff──▶ signed_off
  ▲                   │                     │                      │
  └── changes_requested ◀──request_changes──┘                      │
  └──────────────────────────────── reopen ◀───────────────────────┘
```

| Who | Can do |
|---|---|
| Team signer | Save draft, **Submit for review** (all required answers needed), **Sign off** (only after approval) |
| Capacity Planning admin | **Approve for sign-off** (optional note), **Request changes** (note required, shown to the team), **Reopen** a signed-off review (reason required) |
| Viewer | Read only |

Answers lock on submit and stay locked through approval, so the team signs exactly what was reviewed.

## Plan settings (admin)

**Overview → Plan settings** sets the baseline (starting point) each team's growth % is measured against:

- **Cycle default**: last month, 3-month average, 6-month average or 12-month peak of the months before
  the cycle's first quarter. Until you set one, `default_baseline` in `cycles.py` applies (last month for
  the current cycle; past cycles keep the 3-month average they were run with).
- **Per team**: any of those, or a **manual value** (with a required note), e.g. last month adjusted for a
  one-off. The table shows every calculation side by side.
- The baseline is recorded with the team's answers on every save/submit. Once a team has submitted, its
  baseline is locked; send the review back (or reopen it) to change it. If the baseline moves while a team
  is still editing, their page tells them.

## Flags (admin)

On a team's page, **Flag an answer** adds a comment to any answer (growth is flagged per quarter) and,
optionally, the **planning value** Capacity Planning will use instead. The team's answer is never edited:
each flag stores the original answer and the adjusted value. Flags can be shared with the team (shown next
to the answer) or kept internal. Re-flagging an answer supersedes the old flag; clearing one keeps it, marked
cleared. The main CSV adds `planning_growth_*` / `planning_msu_*` columns (adjusted value if flagged,
otherwise the team's); **Export flags** downloads every flag with original and adjusted values.

## Reference numbers on the form

Under the growth question, teams see their answers from the last two cycles, converted to % against
today's baseline (and the raw answers as entered). They also see a **statistical projection**: a linear
trend over all months of actual usage times a per-calendar-month seasonal index. "Fill in the statistical
projection" copies it into the inputs as a starting point. It's also drawn on the chart.

## Importing past cycles (one-time)

`import_history.py` loads past reviews (e.g. the email era) from a CSV as signed-off submissions:

```bash
python import_history.py --template 2026-H1 > past.csv   # header row to fill in
python import_history.py past.csv --dry-run              # validate only
python import_history.py past.csv                        # import
```

Include `baseline_msu` (what the team's % were against) when you have it. Older cycles need adding to
`cycles.CYCLES` first. Existing submissions are skipped; `--replace` only overwrites earlier imports. See the
docstring for all columns.

## Data

Production runs on **SQL Server**; the local demo and the tests use SQLite with the same tables and SQL
(`db.py` holds the few dialect differences). All reads and writes are in `store.py`.

| Table | What's in it |
|---|---|
| `submissions` | One row per team per cycle: status, who submitted/reviewed/signed off, and the baseline (MSU, method, description) the team's % are measured against |
| `answers` | Every question except growth, one row per answered question. Checkbox picks are `; `-separated; "Other" text is in `other_text` |
| `growth_projections` | The growth question, one row per quarter: `quarter_label`, `quarter_start` (join key to Db2 usage), `team_pct`, `team_msu` |
| `answer_flags` | Capacity Planning flags: the team's original value, the adjusted value (`adjusted_pct` for growth), comment, visibility. Closed, never deleted |
| `plan_settings` | Baseline method / manual MSU per cycle (`team_id` NULL = cycle default) and per team |
| `submission_events` | Every workflow step with a JSON copy of the answers at that moment (the History tab's change log) |
| `audit_log` | Sign-ins, actions, denied attempts, exports, setting changes |

Teams (`sample_data/teams.json`), cycles (`cycles.py`) and the questionnaire (`questions.py`) stay in code.
Actual usage is read from Db2, not copied.

**Reporting** reads two views in schema `rpt`, never the tables:

- `rpt.v_projections`: one row per team, cycle and quarter with `team_pct`/`team_msu` and
  `planning_pct`/`planning_msu` (the team's value unless an open flag adjusted it). Join to the Db2 usage
  table on team + `quarter_start` for projected vs actual (in Power BI, or via a linked server).
- `rpt.v_answers`: every other answer, one row per team, cycle and question.

### Setting up SQL Server

1. Have a DBA create a database and run, in order:
   `sql/sqlserver/01_schema.sql`, `02_reporting_views.sql`, `03_permissions.sql` (then add the app's
   identity to role `cap_review_app` and reporting users to `cap_review_reporting`). SQL Server 2016+.
2. Install Microsoft's *ODBC Driver 18 for SQL Server* and `pip install pyodbc`.
3. Set the connection string (from a vault, not a file). Examples:

```
# Entra managed identity (app hosted in Azure):
PORTAL_SQLSERVER="Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>,1433;Database=CapReview;Encrypt=yes;Authentication=ActiveDirectoryMsi"
# Windows integrated auth (on-prem host running under a service account):
PORTAL_SQLSERVER="Driver={ODBC Driver 18 for SQL Server};Server=<server>;Database=CapReview;Encrypt=yes;Trusted_Connection=yes"
```

The app checks the tables exist on start; it doesn't create or alter them in SQL Server. The demo seed data
is only loaded into SQLite. Schema changes go into new numbered scripts in `sql/sqlserver/`; keep
`sql/sqlite_schema.sql` in step.

### Usage data from Db2

```
PORTAL_USAGE=db2
PORTAL_DB2="Driver={IBM DB2 ODBC DRIVER};Database=...;Hostname=...;Port=...;Protocol=TCPIP;..."
```

Edit `sql/db2_usage_query.sql` to match your table: it receives one application key and returns
`(month, avg MSU)` rows. Set `usage_key` on a team in `teams.json` if its Db2 key differs from its id. Results
are cached for an hour (`PORTAL_USAGE_CACHE_SECONDS`). Without `PORTAL_USAGE=db2` the app uses the generated
sample data in `sample_usage.py`.

### Local demo database

SQLite at `instance/portal.db` (or `PORTAL_DB`), created and seeded with two past cycles from
`sample_data/past_submissions.json` on first run. Delete the file to start over.

## Tests

```bash
pip install pytest
python -m pytest tests
```

## Switching to real Entra ID

1. Register an app in Entra ID (Web platform), redirect URI `https://<host>/auth/callback`.
2. Token configuration → **Add groups claim** → *Security groups*, emitted as **Group ID**, and limit
   it to **Groups assigned to the application**. Assign the `SG-CAPREVIEW-*` groups to the
   enterprise application. (This avoids the 200-group "overage" case.)
3. Put the real group object IDs into `sample_data/teams.json` (rename it to e.g. `config/teams.json`).
4. Set environment variables and run behind HTTPS:

```
AUTH_MODE=entra
ENTRA_TENANT_ID=...
ENTRA_CLIENT_ID=...
ENTRA_CLIENT_SECRET=...        # from a vault, not a file
PORTAL_SECRET_KEY=<long random value>   # required outside demo mode; the app refuses to start without it
PORTAL_HTTPS=1
```

In Entra mode group membership comes from the ID token, so a removal takes effect at the next
sign-in (sessions last 8 hours). For immediate re-checks at sign-off, grant `GroupMember.Read.All`
and implement `EntraAuth.current_groups` with Graph's `/me/checkMemberGroups`.

The Entra path is written against MSAL's documented auth-code flow but has not been run against a
real tenant. Test it in a dev tenant first.

## What to replace for production

| Sample piece | Replace with |
|---|---|
| `sample_usage.py` (generated MSU numbers) | Your Db2 usage table: set `PORTAL_USAGE=db2` and edit `sql/db2_usage_query.sql` |
| `cycles.py` | Add a new entry each spring/fall; the last one is the open cycle |
| No notifications | Email the team on approve / request changes, and Capacity Planning on submit (e.g. Graph `sendMail`) |
| `sample_data/teams.json` | Real app list + group IDs (or generate it from a CMDB export) |
| SQLite | SQL Server: set `PORTAL_SQLSERVER` after running `sql/sqlserver/*.sql` |
| `python app.py` dev server | gunicorn/waitress behind your standard reverse proxy |
| `questions.py` | Your actual questionnaire: the form, validation and CSV export are generated from it. Every dropdown, radio and checkbox question gets an "Other" option with a text box; set `"other": False` on a question to turn it off |


