# ctalk

POC for pinging a coworker through Claude Code.

- `relay.py`: hosted FastAPI relay plus remote MCP endpoint.
- `server.py`: optional local stdio MCP bridge.

This is a small hosted MCP app with organization-scoped users, passwords, and auth tokens. In production it uses Postgres via `DATABASE_URL`; without `DATABASE_URL`, it falls back to local SQLite for development. There is no web UI and no realtime push.

## Fast Remote MCP Setup

No local repo or Python install is required for the remote MCP path. Add the hosted MCP endpoint:

```bash
claude mcp add --transport http ctalk https://ctalk-relay.onrender.com/mcp -s user
```

Then create an organization:

```text
ctalk register organization with org_id ai-company, name "The AI Company", and admin password "choose-a-long-admin-password".
```

Store the `org_id` and admin password carefully. The admin password is required to register users and perform admin actions.

Register a user:

```text
ctalk register user umar in org ai-company with admin password "choose-a-long-admin-password", password "choose-a-user-password", name Umar, ownership platform tooling.
```

Login:

```text
ctalk login to org ai-company as umar with password "choose-a-user-password".
```

Claude should reuse the returned `auth_token` for later ctalk tool calls in the same conversation. Treat it like a password.

## Optional Local Stdio Install

If you want to run the MCP bridge locally instead of using hosted MCP:

```bash
cd /path/to/ctalk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Deploy

The hosted app serves both the REST relay and the remote MCP endpoint at `/mcp`.

### Recommended Production Stack

- App host: Railway Hobby or Render Starter.
- Database: Neon Postgres.
- Required app env var: `DATABASE_URL`, copied from Neon.
- Start command: `uvicorn relay:app --host 0.0.0.0 --port $PORT`
- Healthcheck path: `/health`

Neon free databases can scale to zero when idle. Railway app services on the paid Hobby plan are a better fit than Render free for this because Render free web services spin down after inactivity.

### Railway + Neon

1. Create a Neon project.
2. Copy the pooled Postgres connection string from Neon.
3. Deploy this GitHub repo on Railway.
4. Set Railway environment variable:

```text
DATABASE_URL=postgresql://...
```

5. Use:

```bash
uvicorn relay:app --host 0.0.0.0 --port $PORT
```

This repo includes `railway.json` with that start command.

After deployment, verify:

```bash
curl https://your-relay-url/health
```

Expected:

```json
{"status":"ok"}
```

Then add the hosted MCP endpoint:

```bash
claude mcp add --transport http ctalk https://your-relay-url/mcp -s user
```

### Render + Neon

Render's FastAPI docs use:

```bash
uvicorn relay:app --host 0.0.0.0 --port $PORT
```

Use:

```text
Build Command: pip install -r requirements.txt
Start Command: uvicorn relay:app --host 0.0.0.0 --port $PORT
Environment: DATABASE_URL=postgresql://...
```

Do not rely on Render free filesystem storage for real users. If `DATABASE_URL` is not set, the app uses SQLite and data may disappear after restarts/redeploys on free hosts.

### Local Development

SQLite fallback:

```bash
uvicorn relay:app --host 0.0.0.0 --port 8000
```

Postgres local/Neon test:

```bash
DATABASE_URL='postgresql://...' uvicorn relay:app --host 0.0.0.0 --port 8000
```

Local MCP check:

```bash
claude mcp add --transport http ctalk-local http://127.0.0.1:8000/mcp -s local
```

### SQLite Escape Hatch

If you really want file-backed SQLite on a host with persistent disks:

```text
RELAY_DB_PATH=/data/relay.sqlite3
```

## First Run

Each person registers once from Claude Code:

```text
ctalk register organization with org_id ai-company, name "The AI Company", and admin password "choose-a-long-admin-password".
```

```text
ctalk register user john in org ai-company with admin password "choose-a-long-admin-password", password "choose-a-user-password", name John, ownership alpha-repo and payments-service.
```

## Send A Message

Ask:

```text
ctalk ask john: What is the retry logic in alpha-repo?
```

John checks:

```text
ctalk check my inbox.
```

John drafts:

```text
ctalk propose a response to message 1: The retry logic uses three attempts with exponential backoff.
```

After John explicitly approves:

```text
ctalk send the approved response to message 1: The retry logic uses three attempts with exponential backoff.
```

Umar checks replies:

```text
ctalk check my replies.
```

## Tools

- `register_organization(org_id, name, admin_password)`: creates an organization.
- `register_user(org_id, admin_password, user_id, name, password, ownership, notes = "")`: creates a user.
- `login(org_id, user_id, password)`: returns an auth token.
- `whoami(auth_token)`: checks the logged-in user.
- `list_users(auth_token)`: lists users in your organization.
- `get_user_profile(auth_token, user_id)`: gets profile and interaction metadata.
- `update_my_notes(auth_token, notes)`: updates your own notes.
- `update_user_profile_notes(org_id, admin_password, user_id, profile_notes)`: admin-only profile notes.
- `ask_person(auth_token, to_user_id, question)`: posts a question inside your organization.
- `check_inbox(auth_token)`: returns pending questions plus sender context.
- `propose_response(auth_token, message_id, draft_text)`: saves a draft only.
- `send_response(auth_token, message_id, final_text)`: delivers the answer after explicit human approval.
- `check_replies(auth_token, since = "1970-01-01T00:00:00+00:00")`: polls answers.

## HTTP API

- `GET /health`
- `GET /`
- `POST /mcp`

The old unauthenticated REST roster/message API is disabled. Use the MCP tools.
