# ctalk

POC for pinging a coworker through Claude Code.

- `relay.py`: hosted FastAPI + SQLite relay plus remote MCP endpoint.
- `server.py`: optional local stdio MCP bridge.

This is same-trust v0: no auth, no web UI, no push, no passwords.

## Fast Remote MCP Setup

No local repo or Python install is required for the remote MCP path. Add the hosted MCP endpoint:

```bash
claude mcp add --transport http ctalk https://ctalk-relay.onrender.com/mcp -s user
```

Then register in Claude Code:

```text
ctalk register me with user_id umar. My name is Umar. I own platform tooling.
```

Because this v0 has no auth, remote MCP prompts should include your `user_id`, such as `umar` or `john`.

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

### Render

Render's FastAPI docs use:

```bash
uvicorn relay:app --host 0.0.0.0 --port $PORT
```

Free-tier Render services do not support persistent disks. For the free tier, use:

```text
Build Command: pip install -r requirements.txt
Start Command: uvicorn relay:app --host 0.0.0.0 --port $PORT
Environment: RELAY_DB_PATH=/tmp/relay.sqlite3
```

This works for a POC, but roster entries and messages may disappear after restarts or redeploys.

If you upgrade and add a persistent disk, this repo includes `render.yaml` with that start command plus a disk mounted at `/var/data`. Use:

```text
Build Command: pip install -r requirements.txt
Start Command: uvicorn relay:app --host 0.0.0.0 --port $PORT
Environment: RELAY_DB_PATH=/var/data/relay.sqlite3
Disk Mount Path: /var/data
```

Without a persistent disk, do not use `/var/data`.

### Railway

Railway can deploy this from GitHub or with the CLI. This repo includes `railway.json` with:

```bash
uvicorn relay:app --host 0.0.0.0 --port $PORT
```

Recommended Railway settings:

```text
Start Command: uvicorn relay:app --host 0.0.0.0 --port $PORT
Healthcheck Path: /health
Environment: RELAY_DB_PATH=/data/relay.sqlite3
Volume Mount Path: /data
Public Networking: Generate Domain
```

After deployment, verify:

```bash
curl https://your-relay-url/health
```

Expected:

```json
{"status":"ok"}
```

## First Run

Each person registers once from Claude Code:

```text
ctalk register me with user_id umar. My name is Umar. I own alpha-repo and platform tooling.
```

```text
ctalk register me with user_id john. My name is John. I own alpha-repo and payments-service.
```

## Send A Message

Ask:

```text
ctalk ask john from umar: What is the retry logic in alpha-repo?
```

John checks:

```text
ctalk check inbox for john.
```

John drafts:

```text
ctalk propose a response as john to message 1: The retry logic uses three attempts with exponential backoff.
```

After John explicitly approves:

```text
ctalk send the approved response as john to message 1: The retry logic uses three attempts with exponential backoff.
```

Umar checks replies:

```text
ctalk check replies for umar.
```

## Tools

- `register_self(user_id, name, ownership, notes = "")`: creates your roster profile.
- `ask_person(from_id, to_id, question)`: confirms the recipient exists, then posts a question.
- `check_inbox(user_id)`: returns pending questions plus sender roster context.
- `propose_response(user_id, message_id, draft_text)`: saves a draft only.
- `send_response(user_id, message_id, final_text)`: delivers the answer after explicit human approval.
- `check_replies(user_id, since = "1970-01-01T00:00:00+00:00")`: polls answers.

## HTTP API

- `GET /health`
- `POST /messages` with `{ "from_id": "umar", "to_id": "john", "question": "..." }`
- `GET /inbox/{user_id}`
- `POST /messages/{id}/draft` with `{ "draft_text": "..." }`
- `POST /messages/{id}/respond` with `{ "final_text": "..." }`
- `GET /replies/{user_id}?since=<timestamp>`
- `POST /roster` with `{ "id": "john", "name": "John", "ownership": ["alpha-repo"], "notes": "" }`
- `GET /roster/{id}`
