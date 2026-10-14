# Mainframe Capacity Review portal (sample)

A small Flask app that replaces the twice-a-year capacity-review emails. Each app team signs in,
sees **only its own application's** usage chart and questionnaire, and a member of the team's
signer group signs off. Capacity Planning gets a status overview, an audit log and a CSV export.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5057. The SQLite database is created at `instance/portal.db`; delete it to start over.

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
| cap.admin | Capacity Planning: review queue, approve or request changes, reopen, audit log, CSV export |

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

## Data

SQLite at `instance/portal.db`:

- `submissions`: current state per team per cycle (answers as JSON, status, who submitted/reviewed/signed and when)
- `submission_versions`: a full snapshot of the answers on every workflow event; powers the History tab's change log
- `audit_log`: logins, actions, denied attempts, exports

In demo mode the database is seeded with two past cycles from `sample_data/past_submissions.json`.
There are no migrations: if you change the schema, delete `instance/portal.db` and restart.

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
PORTAL_SECRET_KEY=<long random value>
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
| `sample_usage.monthly_msu()` (generated MSU numbers) | Query your SMF/RMF-derived capacity data per application |
| `cycles.py` | Add a new entry each spring/fall; the last one is the open cycle |
| No notifications | Email the team on approve / request changes, and Capacity Planning on submit (e.g. Graph `sendMail`) |
| `sample_data/teams.json` | Real app list + group IDs (or generate it from a CMDB export) |
| SQLite | Postgres / SQL Server if more than one app server |
| `python app.py` dev server | gunicorn/waitress behind your standard reverse proxy |
| `questions.py` | Your actual questionnaire: the form, validation and CSV export are generated from it. Every dropdown, radio and checkbox question gets an "Other" option with a text box; set `"other": False` on a question to turn it off |


