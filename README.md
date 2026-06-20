# ctalk

POC for pinging a coworker through Claude Code.

- `relay.py`: hosted FastAPI + SQLite relay.
- `server.py`: local stdio MCP server that each person points at the relay.

This is same-trust v0: no auth, no web UI, no push, no passwords.

## Local Install

Each person who wants to use the MCP tools needs a local checkout of this repo:

```bash
cd /path/to/ctalk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Deploy The Relay

The relay is the only hosted piece. Everyone's local MCP server points to the same relay URL.

### Render

Render's FastAPI docs use:

```bash
uvicorn relay:app --host 0.0.0.0 --port $PORT
```

This repo includes `render.yaml` with that start command plus a persistent disk mounted at `/var/data`. Use:

```text
Build Command: pip install -r requirements.txt
Start Command: uvicorn relay:app --host 0.0.0.0 --port $PORT
Environment: RELAY_DB_PATH=/var/data/relay.sqlite3
Disk Mount Path: /var/data
```

Without a persistent disk, SQLite data can disappear when the service restarts or redeploys.

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

## Add ctalk To Claude Code

Each person adds one MCP server named `ctalk`, using their own `CURRENT_USER_ID` and the shared deployed relay URL.

```bash
claude mcp add-json ctalk '{
  "type": "stdio",
  "command": "/absolute/path/to/ctalk/.venv/bin/python",
  "args": ["/absolute/path/to/ctalk/server.py"],
  "env": {
    "RELAY_URL": "https://your-relay-url",
    "CURRENT_USER_ID": "umar"
  }
}'
```

John uses the same `RELAY_URL`, but sets:

```json
"CURRENT_USER_ID": "john"
```

Check the MCP server:

```bash
claude mcp list
```

You should see:

```text
ctalk ... ✓ Connected
```

## First Run

Each person registers once from Claude Code:

```text
ctalk register me as Umar. I own alpha-repo and platform tooling.
```

```text
ctalk register me as John. I own alpha-repo and payments-service.
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

- `register_self(name, ownership, notes = "")`: creates your roster profile.
- `ask_person(to_id, question)`: confirms the recipient exists, then posts a question.
- `check_inbox()`: returns pending questions plus sender roster context.
- `propose_response(message_id, draft_text)`: saves a draft only.
- `send_response(message_id, final_text)`: delivers the answer after explicit human approval.
- `check_replies()`: polls answers since `~/.ctalk/last_checked.<CURRENT_USER_ID>`.

## HTTP API

- `GET /health`
- `POST /messages` with `{ "from_id": "umar", "to_id": "john", "question": "..." }`
- `GET /inbox/{user_id}`
- `POST /messages/{id}/draft` with `{ "draft_text": "..." }`
- `POST /messages/{id}/respond` with `{ "final_text": "..." }`
- `GET /replies/{user_id}?since=<timestamp>`
- `POST /roster` with `{ "id": "john", "name": "John", "ownership": ["alpha-repo"], "notes": "" }`
- `GET /roster/{id}`
