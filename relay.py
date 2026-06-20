import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


DB_PATH = os.environ.get("RELAY_DB_PATH", "relay.sqlite3")


app = FastAPI(title="Claude Code Coworker Ping Relay")


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


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.get("/roster/{user_id}")
def get_roster_entry(user_id: str) -> dict[str, Any]:
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
            raise HTTPException(status_code=404, detail="roster entry not found")
        return row_to_dict(row)
