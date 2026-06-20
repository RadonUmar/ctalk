import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("RELAY_DB_PATH", "relay.sqlite3")
SETUP_PAGE = Path(__file__).with_name("setup.html")


remote_mcp = FastMCP(
    "ctalk",
    instructions=(
        "ctalk lets Claude Code users register themselves, ask coworkers questions, "
        "check inboxes, draft replies, and send approved replies. Because this hosted "
        "v0 has no auth, tools require an explicit user_id/from_id argument."
    ),
    stateless_http=True,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    async with remote_mcp.session_manager.run():
        yield


app = FastAPI(title="ctalk relay", lifespan=lifespan)


class MessageCreate(BaseModel):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


class DraftCreate(BaseModel):
    draft_text: str = Field(min_length=1)


class ResponseCreate(BaseModel):
    final_text: str = Field(min_length=1)


class RosterCreate(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ownership: list[str] = Field(default_factory=list)
    notes: str = ""


@contextmanager
def db() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "ownership" in data and isinstance(data["ownership"], str):
        data["ownership"] = json.loads(data["ownership"])
    return data


def get_roster_profile(user_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM roster
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return row_to_dict(row)


def require_roster_profile(user_id: str) -> dict[str, Any]:
    profile = get_roster_profile(user_id)
    if profile is None:
        raise ValueError(f"'{user_id}' is not registered yet. Ask them to register with ctalk first.")
    return profile


def init_db() -> None:
    global DB_PATH

    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except PermissionError:
            fallback_path = "/tmp/relay.sqlite3"
            logger.warning("Cannot write to %s; falling back to %s", db_dir, fallback_path)
            DB_PATH = fallback_path

    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                query_text TEXT NOT NULL,
                draft_response TEXT,
                final_response TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending', 'answered')),
                created_at TEXT NOT NULL,
                answered_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS roster (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                ownership TEXT NOT NULL,
                notes TEXT NOT NULL
            )
            """
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def setup_page() -> FileResponse:
    return FileResponse(SETUP_PAGE)


@app.post("/messages")
def create_message(message: MessageCreate) -> dict[str, Any]:
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (
                from_id, to_id, query_text, status, created_at
            ) VALUES (?, ?, ?, 'pending', ?)
            """,
            (message.from_id, message.to_id, message.question, utc_now()),
        )
        return {"id": cursor.lastrowid, "status": "pending"}


def create_message_row(from_id: str, to_id: str, question: str) -> dict[str, Any]:
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (
                from_id, to_id, query_text, status, created_at
            ) VALUES (?, ?, ?, 'pending', ?)
            """,
            (from_id, to_id, question, utc_now()),
        )
        return {"id": cursor.lastrowid, "status": "pending"}


@app.get("/inbox/{user_id}")
def get_inbox(user_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE to_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (user_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_inbox_rows(user_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE to_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (user_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


@app.post("/messages/{message_id}/draft")
def save_draft(message_id: int, draft: DraftCreate) -> dict[str, Any]:
    with db() as conn:
        cursor = conn.execute(
            """
            UPDATE messages
            SET draft_response = ?
            WHERE id = ?
            """,
            (draft.draft_text, message_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="message not found")
        return {"id": message_id, "status": "draft_saved"}


def save_draft_row(message_id: int, draft_text: str) -> dict[str, Any]:
    with db() as conn:
        cursor = conn.execute(
            """
            UPDATE messages
            SET draft_response = ?
            WHERE id = ?
            """,
            (draft_text, message_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("message not found")
        return {"id": message_id, "status": "draft_saved"}


@app.post("/messages/{message_id}/respond")
def respond(message_id: int, response: ResponseCreate) -> dict[str, Any]:
    answered_at = utc_now()
    with db() as conn:
        message = conn.execute(
            """
            SELECT draft_response
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        if message is None:
            raise HTTPException(status_code=404, detail="message not found")
        if message["draft_response"] is None:
            raise HTTPException(status_code=409, detail="draft response required before sending")

        cursor = conn.execute(
            """
            UPDATE messages
            SET final_response = ?, status = 'answered', answered_at = ?
            WHERE id = ?
            """,
            (response.final_text, answered_at, message_id),
        )
        return {"id": message_id, "status": "answered", "answered_at": answered_at}


def respond_row(message_id: int, final_text: str) -> dict[str, Any]:
    answered_at = utc_now()
    with db() as conn:
        message = conn.execute(
            """
            SELECT draft_response
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        if message is None:
            raise ValueError("message not found")
        if message["draft_response"] is None:
            raise ValueError("draft response required before sending")

        conn.execute(
            """
            UPDATE messages
            SET final_response = ?, status = 'answered', answered_at = ?
            WHERE id = ?
            """,
            (final_text, answered_at, message_id),
        )
        return {"id": message_id, "status": "answered", "answered_at": answered_at}


@app.get("/replies/{user_id}")
def get_replies(user_id: str, since: str = "1970-01-01T00:00:00+00:00") -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE from_id = ?
              AND status = 'answered'
              AND answered_at > ?
            ORDER BY answered_at ASC
            """,
            (user_id, since),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


def get_reply_rows(user_id: str, since: str = "1970-01-01T00:00:00+00:00") -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM messages
            WHERE from_id = ?
              AND status = 'answered'
              AND answered_at > ?
            ORDER BY answered_at ASC
            """,
            (user_id, since),
        ).fetchall()
        return [row_to_dict(row) for row in rows]


@app.post("/roster")
def create_roster_entry(entry: RosterCreate) -> dict[str, Any]:
    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO roster (id, name, ownership, notes)
                VALUES (?, ?, ?, ?)
                """,
                (entry.id, entry.name, json.dumps(entry.ownership), entry.notes),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="roster id already exists") from exc
        return {"id": entry.id, "name": entry.name, "ownership": entry.ownership, "notes": entry.notes}


def create_roster_row(user_id: str, name: str, ownership: list[str], notes: str = "") -> dict[str, Any]:
    with db() as conn:
        try:
            conn.execute(
                """
                INSERT INTO roster (id, name, ownership, notes)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, name, json.dumps(ownership), notes),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("roster id already exists") from exc
        return {"id": user_id, "name": name, "ownership": ownership, "notes": notes}


@app.get("/roster/{user_id}")
def get_roster_entry(user_id: str) -> dict[str, Any]:
    profile = get_roster_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="roster entry not found")
    return profile


@remote_mcp.tool()
def register_self(user_id: str, name: str, ownership: list[str], notes: str = "") -> dict[str, Any]:
    """Register yourself in the shared ctalk roster. user_id should be a short id like 'umar' or 'john'."""
    profile = create_roster_row(user_id=user_id, name=name, ownership=ownership, notes=notes)
    return {"registered": True, "profile": profile}


@remote_mcp.tool()
def ask_person(from_id: str, to_id: str, question: str) -> dict[str, Any]:
    """Ask a registered coworker a question. from_id is your ctalk user id."""
    require_roster_profile(from_id)
    recipient = require_roster_profile(to_id)
    result = create_message_row(from_id=from_id, to_id=to_id, question=question)
    return {"sent": True, "message_id": result["id"], "to": recipient, "question": question}


@remote_mcp.tool()
def check_inbox(user_id: str) -> dict[str, Any]:
    """Check pending questions for user_id, including sender roster context."""
    require_roster_profile(user_id)
    messages = get_inbox_rows(user_id)
    enriched = []
    for message in messages:
        sender = get_roster_profile(message["from_id"])
        enriched.append(
            {
                "id": message["id"],
                "from_id": message["from_id"],
                "query_text": message["query_text"],
                "created_at": message["created_at"],
                "draft_response": message.get("draft_response"),
                "from_profile": sender or {"id": message["from_id"], "registered": False},
            }
        )
    return {"user_id": user_id, "pending": enriched}


@remote_mcp.tool()
def propose_response(user_id: str, message_id: int, draft_text: str) -> dict[str, Any]:
    """Save a draft response for human review without delivering it."""
    require_roster_profile(user_id)
    result = save_draft_row(message_id=message_id, draft_text=draft_text)
    return {
        "draft_saved": True,
        "message_id": result["id"],
        "draft_text": draft_text,
        "approval_required": "Review this draft with the human before calling send_response.",
    }


@remote_mcp.tool()
def send_response(user_id: str, message_id: int, final_text: str) -> dict[str, Any]:
    """Deliver a final response after explicit human approval. Use propose_response first."""
    require_roster_profile(user_id)
    result = respond_row(message_id=message_id, final_text=final_text)
    return {
        "sent": True,
        "message_id": result["id"],
        "answered_at": result["answered_at"],
        "final_text": final_text,
    }


@remote_mcp.tool()
def check_replies(user_id: str, since: str = "1970-01-01T00:00:00+00:00") -> dict[str, Any]:
    """Check answers to questions user_id previously asked."""
    require_roster_profile(user_id)
    return {"user_id": user_id, "since": since, "replies": get_reply_rows(user_id=user_id, since=since)}


mcp_app = remote_mcp.streamable_http_app()
app.mount("/", mcp_app)
