import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from mcp.server.fastmcp import FastMCP

import graph

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("RELAY_DB_PATH", "relay.sqlite3")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = bool(DATABASE_URL)
SETUP_PAGE = Path(__file__).with_name("setup.html")
GRAPH_PAGE = Path(__file__).with_name("graph.html")
HASH_ITERATIONS = 200_000


remote_mcp = FastMCP(
    "ctalk",
    instructions=(
        "ctalk lets Claude Code users create organizations, register users, log in, "
        "ask coworkers questions, check inboxes, draft replies, and send approved replies. "
        "Use register_organization first with org_id, name, and admin_password; "
        "then register_user with that admin password, then login. Authenticated "
        "message tools require the auth_token returned by login. After login succeeds, "
        "reuse that auth_token for later ctalk tool calls in the same conversation; "
        "do not ask the human to paste it again unless it is missing or invalid."
    ),
    host="0.0.0.0",
    stateless_http=True,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    try:
        graph.init_graph()
    except Exception as exc:
        logger.warning("Neo4j graph init skipped: %s", exc)
    async with remote_mcp.session_manager.run():
        yield
    graph.close_driver()


app = FastAPI(title="ctalk relay", lifespan=lifespan)


@contextmanager
def db() -> Any:
    if USE_POSTGRES:
        if psycopg is None or dict_row is None:
            raise RuntimeError("DATABASE_URL is set, but psycopg is not installed.")
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sql(sqlite_sql: str) -> str:
    if USE_POSTGRES:
        return sqlite_sql.replace("?", "%s")
    return sqlite_sql


def execute(conn: Any, sqlite_sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(sql(sqlite_sql), params)


def is_integrity_error(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return bool(psycopg is not None and isinstance(exc, psycopg.IntegrityError))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_list(values: list[str]) -> str:
    return json.dumps(values)


def parse_json_list(value: str) -> list[str]:
    parsed = json.loads(value)
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def make_auth_token() -> str:
    return f"ctalk_auth_{secrets.token_urlsafe(32)}"


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, HASH_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        HASH_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_secret(secret: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_id(value: str, label: str) -> str:
    normalized = value.strip().lower()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not normalized:
        raise ValueError(f"{label} cannot be empty.")
    if len(normalized) > 64:
        raise ValueError(f"{label} must be 64 characters or fewer.")
    if any(char not in allowed for char in normalized):
        raise ValueError(f"{label} can only contain letters, numbers, hyphens, and underscores.")
    return normalized


def user_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "org_id": row["org_id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "ownership": parse_json_list(row["ownership"]),
        "notes": row["notes"],
        "profile_notes": row["profile_notes"],
        "created_at": row["created_at"],
    }


def message_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "org_id": row["org_id"],
        "from_user_id": row["from_user_id"],
        "to_user_id": row["to_user_id"],
        "query_text": row["query_text"],
        "draft_response": row["draft_response"],
        "final_response": row["final_response"],
        "status": row["status"],
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
    }


def ensure_column(conn: Any, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in execute(conn, f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        execute(conn, f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    global DB_PATH

    if not USE_POSTGRES:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
            except PermissionError:
                fallback_path = "/tmp/relay.sqlite3"
                logger.warning("Cannot write to %s; falling back to %s", db_dir, fallback_path)
                DB_PATH = fallback_path

    with db() as conn:
        if USE_POSTGRES:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    admin_password_hash TEXT,
                    created_at TEXT NOT NULL
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS users (
                    org_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    ownership TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    profile_notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, user_id),
                    FOREIGN KEY (org_id) REFERENCES organizations(id)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, user_id)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS org_messages (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    from_user_id TEXT NOT NULL,
                    to_user_id TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    draft_response TEXT,
                    final_response TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'answered')),
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    FOREIGN KEY (org_id) REFERENCES organizations(id),
                    FOREIGN KEY (org_id, from_user_id) REFERENCES users(org_id, user_id),
                    FOREIGN KEY (org_id, to_user_id) REFERENCES users(org_id, user_id)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS user_interactions (
                    org_id TEXT NOT NULL,
                    user_a_id TEXT NOT NULL,
                    user_b_id TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_interaction_at TEXT,
                    summary TEXT NOT NULL DEFAULT '',
                    private_notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (org_id, user_a_id, user_b_id),
                    FOREIGN KEY (org_id) REFERENCES organizations(id)
                )
                """,
            )
            execute(conn, "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS admin_password_hash TEXT")
            execute(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_notes TEXT NOT NULL DEFAULT ''")
            execute(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT ''")
            execute(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS level TEXT NOT NULL DEFAULT ''")
            execute(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS commit_history_opt_in INTEGER NOT NULL DEFAULT 0")
        else:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    admin_password_hash TEXT,
                    created_at TEXT NOT NULL
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS users (
                    org_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    ownership TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    profile_notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (org_id, user_id),
                    FOREIGN KEY (org_id) REFERENCES organizations(id)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    FOREIGN KEY (org_id, user_id) REFERENCES users(org_id, user_id)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS org_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org_id TEXT NOT NULL,
                    from_user_id TEXT NOT NULL,
                    to_user_id TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    draft_response TEXT,
                    final_response TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'answered')),
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    FOREIGN KEY (org_id) REFERENCES organizations(id),
                    FOREIGN KEY (org_id, from_user_id) REFERENCES users(org_id, user_id),
                    FOREIGN KEY (org_id, to_user_id) REFERENCES users(org_id, user_id)
                )
                """,
            )
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS user_interactions (
                    org_id TEXT NOT NULL,
                    user_a_id TEXT NOT NULL,
                    user_b_id TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_interaction_at TEXT,
                    summary TEXT NOT NULL DEFAULT '',
                    private_notes TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (org_id, user_a_id, user_b_id),
                    FOREIGN KEY (org_id) REFERENCES organizations(id)
                )
                """,
            )
            ensure_column(conn, "organizations", "admin_password_hash", "TEXT")
            ensure_column(conn, "users", "profile_notes", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "users", "role", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "users", "level", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "users", "commit_history_opt_in", "INTEGER NOT NULL DEFAULT 0")
        # --- knowledge-graph tables (SQL is the system of record; ADR 0002) ---
        execute(conn,
            """
            CREATE TABLE IF NOT EXISTS projects (
                org_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                repo_url TEXT NOT NULL DEFAULT '',
                aliases TEXT NOT NULL DEFAULT '[]',
                context TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (org_id, project_id)
            )
            """,
        )
        execute(conn,
            """
            CREATE TABLE IF NOT EXISTS project_ownership (
                org_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'contributor',
                PRIMARY KEY (org_id, project_id, user_id)
            )
            """,
        )
        execute(conn,
            """
            CREATE TABLE IF NOT EXISTS reporting (
                org_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                PRIMARY KEY (org_id, user_id, manager_id)
            )
            """,
        )
        execute(conn,
            """
            CREATE TABLE IF NOT EXISTS skills (
                org_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                skill TEXT NOT NULL,
                PRIMARY KEY (org_id, user_id, skill)
            )
            """,
        )
        execute(conn,
            """
            CREATE TABLE IF NOT EXISTS project_context (
                org_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                author_id TEXT NOT NULL DEFAULT '',
                context_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (org_id, project_id, created_at)
            )
            """,
        )
        execute(conn,
            """
            CREATE TABLE IF NOT EXISTS contributions (
                org_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                commits INTEGER NOT NULL DEFAULT 0,
                last_commit_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (org_id, project_id, user_id)
            )
            """,
        )
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_users_org ON users (org_id)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_messages_inbox ON org_messages (org_id, to_user_id, status)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_messages_replies ON org_messages (org_id, from_user_id, status)")
        execute(conn, "CREATE INDEX IF NOT EXISTS idx_interactions_org_user ON user_interactions (org_id, user_a_id)")


def get_organization(org_id: str) -> Mapping[str, Any] | None:
    with db() as conn:
        return execute(conn, "SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()


def require_organization(org_id: str) -> Mapping[str, Any]:
    organization = get_organization(org_id)
    if organization is None:
        raise ValueError(f"Organization '{org_id}' was not found.")
    return organization


def verify_org_admin_password(org_id: str, admin_password: str) -> None:
    organization = require_organization(org_id)
    stored_hash = organization["admin_password_hash"]
    if not stored_hash:
        raise ValueError("Organization admin password is not configured.")
    if not verify_secret(admin_password, stored_hash):
        raise ValueError("Invalid organization admin password.")


def get_user(org_id: str, user_id: str) -> Mapping[str, Any] | None:
    with db() as conn:
        return execute(conn,
            "SELECT * FROM users WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        ).fetchone()


def require_user(org_id: str, user_id: str) -> Mapping[str, Any]:
    user = get_user(org_id, user_id)
    if user is None:
        raise ValueError(f"User '{user_id}' is not registered in organization '{org_id}'.")
    return user


def require_auth(auth_token: str) -> dict[str, Any]:
    token_hash = hash_token(auth_token)
    with db() as conn:
        row = execute(conn,
            """
            SELECT auth_tokens.org_id, auth_tokens.user_id, users.name, users.ownership,
                   users.notes, users.profile_notes, users.created_at
            FROM auth_tokens
            JOIN users ON users.org_id = auth_tokens.org_id AND users.user_id = auth_tokens.user_id
            WHERE auth_tokens.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("Invalid auth token. Run login again.")
        execute(conn,
            "UPDATE auth_tokens SET last_used_at = ? WHERE token_hash = ?",
            (utc_now(), token_hash),
        )
        return user_to_dict(row)


def record_interaction(org_id: str, user_a_id: str, user_b_id: str) -> None:
    if user_a_id == user_b_id:
        return
    first_user, second_user = sorted([user_a_id, user_b_id])
    now = utc_now()
    with db() as conn:
        execute(conn,
            """
            INSERT INTO user_interactions (
                org_id, user_a_id, user_b_id, message_count, last_interaction_at
            ) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(org_id, user_a_id, user_b_id)
            DO UPDATE SET
                message_count = message_count + 1,
                last_interaction_at = excluded.last_interaction_at
            """,
            (org_id, first_user, second_user, now),
        )


def get_interactions_for_user(org_id: str, user_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = execute(conn,
            """
            SELECT *
            FROM user_interactions
            WHERE org_id = ? AND (user_a_id = ? OR user_b_id = ?)
            ORDER BY last_interaction_at DESC
            """,
            (org_id, user_id, user_id),
        ).fetchall()
    interactions = []
    for row in rows:
        other_user_id = row["user_b_id"] if row["user_a_id"] == user_id else row["user_a_id"]
        interactions.append(
            {
                "other_user_id": other_user_id,
                "message_count": row["message_count"],
                "last_interaction_at": row["last_interaction_at"],
                "summary": row["summary"],
            }
        )
    return interactions


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def setup_page() -> FileResponse:
    return FileResponse(SETUP_PAGE)


@app.get("/messages")
def legacy_messages_disabled() -> dict[str, str]:
    raise HTTPException(status_code=410, detail="Unauthenticated REST API is disabled. Use MCP tools.")


@app.get("/roster/{_:path}")
def legacy_roster_disabled() -> dict[str, str]:
    raise HTTPException(status_code=410, detail="Unauthenticated REST API is disabled. Use MCP tools.")


@remote_mcp.tool()
def register_organization(org_id: str, name: str, admin_password: str) -> dict[str, Any]:
    """Create a new ctalk organization with a caller-chosen org_id, name, and admin password."""
    org_id = normalize_id(org_id, "org_id")
    if len(admin_password) < 12:
        raise ValueError("Organization admin password must be at least 12 characters.")
    now = utc_now()
    with db() as conn:
        try:
            execute(conn,
                """
                INSERT INTO organizations (id, name, admin_password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (org_id, name, hash_secret(admin_password), now),
            )
        except Exception as exc:
            if is_integrity_error(exc):
                raise ValueError(f"Organization '{org_id}' already exists.") from exc
            raise
    return {
        "org_id": org_id,
        "name": name,
        "created_at": now,
        "important": "Store the organization admin password carefully. It is required to register users and perform admin actions.",
    }


@remote_mcp.tool()
def register_user(
    org_id: str,
    admin_password: str,
    user_id: str,
    name: str,
    password: str,
    ownership: list[str],
    notes: str = "",
) -> dict[str, Any]:
    """Register a user in an organization using the organization admin password."""
    org_id = normalize_id(org_id, "org_id")
    user_id = normalize_id(user_id, "user_id")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    verify_org_admin_password(org_id, admin_password)
    now = utc_now()
    with db() as conn:
        try:
            execute(conn,
                """
                INSERT INTO users (org_id, user_id, name, password_hash, ownership, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (org_id, user_id, name, hash_secret(password), json_list(ownership), notes, now),
            )
        except Exception as exc:
            if is_integrity_error(exc):
                raise ValueError(f"User '{user_id}' already exists in organization '{org_id}'.") from exc
            raise
    return {
        "registered": True,
        "profile": {
            "org_id": org_id,
            "user_id": user_id,
            "name": name,
            "ownership": ownership,
            "notes": notes,
            "created_at": now,
        },
        "next_step": "Run login with org_id, user_id, and password to get an auth_token.",
    }


@remote_mcp.tool()
def login(org_id: str, user_id: str, password: str) -> dict[str, Any]:
    """Log in and return an auth_token for future ctalk tool calls."""
    org_id = normalize_id(org_id, "org_id")
    user_id = normalize_id(user_id, "user_id")
    user = require_user(org_id, user_id)
    if not verify_secret(password, user["password_hash"]):
        raise ValueError("Invalid username or password.")
    auth_token = make_auth_token()
    now = utc_now()
    with db() as conn:
        execute(conn,
            """
            INSERT INTO auth_tokens (token_hash, org_id, user_id, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hash_token(auth_token), org_id, user_id, now, now),
        )
    return {
        "logged_in": True,
        "auth_token": auth_token,
        "profile": user_to_dict(user),
        "important": "Treat auth_token like a password.",
        "assistant_instruction": "Reuse this auth_token for future ctalk tool calls in this conversation without asking the human to repeat it.",
    }


@remote_mcp.tool()
def whoami(auth_token: str) -> dict[str, Any]:
    """Return the user profile for this auth token."""
    profile = require_auth(auth_token)
    return {"profile": profile, "interactions": get_interactions_for_user(profile["org_id"], profile["user_id"])}


@remote_mcp.tool()
def list_users(auth_token: str) -> dict[str, Any]:
    """List users in the logged-in user's organization."""
    user = require_auth(auth_token)
    with db() as conn:
        rows = execute(conn,
            """
            SELECT org_id, user_id, name, ownership, notes, profile_notes, created_at
            FROM users
            WHERE org_id = ?
            ORDER BY user_id ASC
            """,
            (user["org_id"],),
        ).fetchall()
    return {"org_id": user["org_id"], "users": [user_to_dict(row) for row in rows]}


@remote_mcp.tool()
def get_user_profile(auth_token: str, user_id: str) -> dict[str, Any]:
    """Get a user's profile and interaction metadata inside your organization."""
    user = require_auth(auth_token)
    target_user_id = normalize_id(user_id, "user_id")
    target = user_to_dict(require_user(user["org_id"], target_user_id))
    return {
        "profile": target,
        "interactions": get_interactions_for_user(user["org_id"], target_user_id),
    }


@remote_mcp.tool()
def update_my_notes(auth_token: str, notes: str) -> dict[str, Any]:
    """Update the logged-in user's profile notes."""
    user = require_auth(auth_token)
    with db() as conn:
        execute(conn,
            """
            UPDATE users
            SET notes = ?
            WHERE org_id = ? AND user_id = ?
            """,
            (notes, user["org_id"], user["user_id"]),
        )
    return {"updated": True, "user_id": user["user_id"], "notes": notes}


@remote_mcp.tool()
def update_user_profile_notes(org_id: str, admin_password: str, user_id: str, profile_notes: str) -> dict[str, Any]:
    """Admin-only update for hidden/profile notes about a user."""
    org_id = normalize_id(org_id, "org_id")
    user_id = normalize_id(user_id, "user_id")
    verify_org_admin_password(org_id, admin_password)
    require_user(org_id, user_id)
    with db() as conn:
        execute(conn,
            """
            UPDATE users
            SET profile_notes = ?
            WHERE org_id = ? AND user_id = ?
            """,
            (profile_notes, org_id, user_id),
        )
    return {"updated": True, "org_id": org_id, "user_id": user_id}


@remote_mcp.tool()
def ask_person(auth_token: str, to_user_id: str, question: str) -> dict[str, Any]:
    """Ask a coworker in your organization a question."""
    sender = require_auth(auth_token)
    to_user_id = normalize_id(to_user_id, "to_user_id")
    recipient = user_to_dict(require_user(sender["org_id"], to_user_id))
    now = utc_now()
    with db() as conn:
        if USE_POSTGRES:
            cursor = execute(conn,
                """
                INSERT INTO org_messages (
                    org_id, from_user_id, to_user_id, query_text, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                RETURNING id
                """,
                (sender["org_id"], sender["user_id"], to_user_id, question, now),
            )
            message_id = cursor.fetchone()["id"]
        else:
            cursor = execute(conn,
                """
                INSERT INTO org_messages (
                    org_id, from_user_id, to_user_id, query_text, status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (sender["org_id"], sender["user_id"], to_user_id, question, now),
            )
            message_id = cursor.lastrowid
    record_interaction(sender["org_id"], sender["user_id"], to_user_id)
    return {
        "sent": True,
        "message_id": message_id,
        "to": recipient,
        "question": question,
        "created_at": now,
    }


@remote_mcp.tool()
def check_inbox(auth_token: str) -> dict[str, Any]:
    """Check pending questions for the logged-in user."""
    user = require_auth(auth_token)
    with db() as conn:
        rows = execute(conn,
            """
            SELECT *
            FROM org_messages
            WHERE org_id = ? AND to_user_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (user["org_id"], user["user_id"]),
        ).fetchall()
    messages = []
    for row in rows:
        message = message_to_dict(row)
        sender = get_user(user["org_id"], message["from_user_id"])
        message["from_profile"] = user_to_dict(sender) if sender else None
        messages.append(message)
    return {"user": user, "pending": messages}


@remote_mcp.tool()
def propose_response(auth_token: str, message_id: int, draft_text: str) -> dict[str, Any]:
    """Save a draft response for human review without delivering it."""
    user = require_auth(auth_token)
    with db() as conn:
        cursor = execute(conn,
            """
            UPDATE org_messages
            SET draft_response = ?
            WHERE id = ? AND org_id = ? AND to_user_id = ? AND status = 'pending'
            """,
            (draft_text, message_id, user["org_id"], user["user_id"]),
        )
        if cursor.rowcount == 0:
            raise ValueError("Message not found in your pending inbox.")
    return {
        "draft_saved": True,
        "message_id": message_id,
        "draft_text": draft_text,
        "approval_required": "Review this draft with the human before calling send_response.",
    }


@remote_mcp.tool()
def send_response(auth_token: str, message_id: int, final_text: str) -> dict[str, Any]:
    """Deliver a final response after explicit human approval. Use propose_response first."""
    user = require_auth(auth_token)
    answered_at = utc_now()
    with db() as conn:
        message = execute(conn,
            """
            SELECT draft_response
            FROM org_messages
            WHERE id = ? AND org_id = ? AND to_user_id = ?
            """,
            (message_id, user["org_id"], user["user_id"]),
        ).fetchone()
        if message is None:
            raise ValueError("Message not found in your inbox.")
        if message["draft_response"] is None:
            raise ValueError("Draft response required before sending.")
        execute(conn,
            """
            UPDATE org_messages
            SET final_response = ?, status = 'answered', answered_at = ?
            WHERE id = ? AND org_id = ? AND to_user_id = ?
            """,
            (final_text, answered_at, message_id, user["org_id"], user["user_id"]),
        )
        from_user = execute(conn,
            """
            SELECT from_user_id
            FROM org_messages
            WHERE id = ? AND org_id = ?
            """,
            (message_id, user["org_id"]),
        ).fetchone()
    if from_user is not None:
        record_interaction(user["org_id"], user["user_id"], from_user["from_user_id"])
    return {
        "sent": True,
        "message_id": message_id,
        "answered_at": answered_at,
        "final_text": final_text,
    }


@remote_mcp.tool()
def check_replies(auth_token: str, since: str = "1970-01-01T00:00:00+00:00") -> dict[str, Any]:
    """Check answers to questions the logged-in user previously asked."""
    user = require_auth(auth_token)
    with db() as conn:
        rows = execute(conn,
            """
            SELECT *
            FROM org_messages
            WHERE org_id = ?
              AND from_user_id = ?
              AND status = 'answered'
              AND answered_at > ?
            ORDER BY answered_at ASC
            """,
            (user["org_id"], user["user_id"], since),
        ).fetchall()
    return {
        "user": user,
        "since": since,
        "replies": [message_to_dict(row) for row in rows],
    }


# --- Knowledge-graph data layer (SQL of record -> Neo4j projection; ADR 0002) ---

def _slugify(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789"
    out: list[str] = []
    prev_dash = False
    for char in value.strip().lower():
        if char in allowed:
            out.append(char)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")
    return (slug or "project")[:64]


def _org_snapshot(org_id: str) -> dict[str, Any]:
    with db() as conn:
        users = [
            {"user_id": r["user_id"], "name": r["name"], "role": r["role"], "level": r["level"],
             "commit_history_opt_in": bool(r["commit_history_opt_in"])}
            for r in execute(conn, "SELECT user_id, name, role, level, commit_history_opt_in FROM users WHERE org_id = ?", (org_id,)).fetchall()
        ]
        projects = [
            {
                "project_id": r["project_id"], "name": r["name"], "description": r["description"],
                "repo_url": r["repo_url"], "aliases": parse_json_list(r["aliases"]), "context": r["context"],
            }
            for r in execute(conn, "SELECT * FROM projects WHERE org_id = ?", (org_id,)).fetchall()
        ]
        ownership = [
            {"project_id": r["project_id"], "user_id": r["user_id"], "role": r["role"]}
            for r in execute(conn, "SELECT project_id, user_id, role FROM project_ownership WHERE org_id = ?", (org_id,)).fetchall()
        ]
        reporting = [
            {"user_id": r["user_id"], "manager_id": r["manager_id"]}
            for r in execute(conn, "SELECT user_id, manager_id FROM reporting WHERE org_id = ?", (org_id,)).fetchall()
        ]
        skills = [
            {"user_id": r["user_id"], "skill": r["skill"]}
            for r in execute(conn, "SELECT user_id, skill FROM skills WHERE org_id = ?", (org_id,)).fetchall()
        ]
        interactions = [
            {"user_a_id": r["user_a_id"], "user_b_id": r["user_b_id"], "message_count": r["message_count"]}
            for r in execute(conn, "SELECT user_a_id, user_b_id, message_count FROM user_interactions WHERE org_id = ?", (org_id,)).fetchall()
        ]
        # Only project commit data for users who currently consent (defense-in-depth:
        # opt-out also deletes rows, but we never read non-consented data regardless).
        contributions = [
            {"project_id": r["project_id"], "user_id": r["user_id"], "commits": r["commits"], "last_commit_at": r["last_commit_at"]}
            for r in execute(conn,
                """
                SELECT c.project_id, c.user_id, c.commits, c.last_commit_at
                FROM contributions c
                JOIN users u ON u.org_id = c.org_id AND u.user_id = c.user_id
                WHERE c.org_id = ? AND u.commit_history_opt_in = 1
                """,
                (org_id,)).fetchall()
        ]
    return {
        "users": users, "projects": projects, "ownership": ownership,
        "reporting": reporting, "skills": skills, "interactions": interactions,
        "contributions": contributions,
    }


def reproject_org(org_id: str) -> None:
    """Rebuild the Neo4j projection for an org from SQL. Best-effort; never raises."""
    if not graph.graph_enabled():
        return
    try:
        graph.rebuild_org_graph(org_id, _org_snapshot(org_id))
    except Exception as exc:
        logger.warning("Graph projection for org '%s' failed: %s", org_id, exc)


def _upsert_project(conn, org_id, project_id, name, description, repo_url, aliases, context) -> None:
    # Merge with any existing project so a later onboarder (e.g. a maintainer) does not
    # clobber the creator's richer metadata: union the aliases, and keep the existing
    # description/repo_url/context whenever the incoming value is empty.
    existing = execute(conn,
        "SELECT name, description, repo_url, aliases, context FROM projects WHERE org_id = ? AND project_id = ?",
        (org_id, project_id)).fetchone()
    if existing is not None:
        name = name or existing["name"]
        description = description or existing["description"]
        repo_url = repo_url or existing["repo_url"]
        context = context or existing["context"]
        merged = list(parse_json_list(existing["aliases"]))
        for alias in aliases:
            if alias not in merged:
                merged.append(alias)
        aliases = merged
    execute(conn,
        """
        INSERT INTO projects (org_id, project_id, name, description, repo_url, aliases, context, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(org_id, project_id) DO UPDATE SET
            name = excluded.name, description = excluded.description,
            repo_url = excluded.repo_url, aliases = excluded.aliases, context = excluded.context
        """,
        (org_id, project_id, name, description, repo_url, json_list(aliases), context, utc_now()),
    )


def _set_ownership(conn, org_id, project_id, user_id, role) -> None:
    execute(conn,
        """
        INSERT INTO project_ownership (org_id, project_id, user_id, role)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(org_id, project_id, user_id) DO UPDATE SET role = excluded.role
        """,
        (org_id, project_id, user_id, role),
    )


def _set_reporting(conn, org_id, user_id, manager_ids) -> list[str]:
    execute(conn, "DELETE FROM reporting WHERE org_id = ? AND user_id = ?", (org_id, user_id))
    saved = []
    for manager_id in manager_ids:
        manager_id = normalize_id(manager_id, "manager_id")
        if manager_id == user_id:
            continue  # no self-reporting
        execute(conn,
            "INSERT INTO reporting (org_id, user_id, manager_id) VALUES (?, ?, ?) "
            "ON CONFLICT(org_id, user_id, manager_id) DO NOTHING",
            (org_id, user_id, manager_id),
        )
        saved.append(manager_id)
    return saved


def _set_skills(conn, org_id, user_id, skills) -> list[str]:
    execute(conn, "DELETE FROM skills WHERE org_id = ? AND user_id = ?", (org_id, user_id))
    saved = []
    for skill in skills:
        skill = skill.strip()
        if not skill:
            continue
        execute(conn,
            "INSERT INTO skills (org_id, user_id, skill) VALUES (?, ?, ?) "
            "ON CONFLICT(org_id, user_id, skill) DO NOTHING",
            (org_id, user_id, skill),
        )
        saved.append(skill)
    return saved


@remote_mcp.tool()
def onboard(
    auth_token: str,
    role: str = "",
    level: str = "",
    reports_to: list[str] | None = None,
    skills: list[str] | None = None,
    projects: list[dict] | None = None,
    store_commit_history: bool = False,
) -> dict[str, Any]:
    """Save onboarding answers and update the org knowledge graph.

    role/level describe the person; reports_to is a list of manager user_ids (matrix
    reporting is allowed); skills is a list of skill names; projects is a list of dicts,
    each: {name, role: creator|maintainer|contributor, description, repo_url, aliases[],
    context}. `context` is the paragraph an agent needs to answer questions about that
    project. store_commit_history records the user's CONSENT to have their commit
    contribution counts stored (opt-in, default false); ingest them later with
    record_commits.
    """
    user = require_auth(auth_token)
    org_id, user_id = user["org_id"], user["user_id"]
    reports_to = reports_to or []
    skills = skills or []
    projects = projects or []
    valid_roles = {"creator", "maintainer", "contributor"}
    saved_projects = []
    with db() as conn:
        execute(conn, "UPDATE users SET role = ?, level = ?, commit_history_opt_in = ? WHERE org_id = ? AND user_id = ?",
                (role, level, 1 if store_commit_history else 0, org_id, user_id))
        saved_managers = _set_reporting(conn, org_id, user_id, reports_to)
        saved_skills = _set_skills(conn, org_id, user_id, skills)
        for proj in projects:
            name = (str(proj.get("name") or proj.get("project_id") or "")).strip()
            if not name:
                continue
            project_id = normalize_id(proj["project_id"], "project_id") if proj.get("project_id") else _slugify(name)
            owner_role = proj.get("role", "contributor")
            if owner_role not in valid_roles:
                owner_role = "contributor"
            aliases = [str(a) for a in (proj.get("aliases") or [])]
            _upsert_project(conn, org_id, project_id, name, proj.get("description", ""),
                            proj.get("repo_url", ""), aliases, proj.get("context", ""))
            _set_ownership(conn, org_id, project_id, user_id, owner_role)
            saved_projects.append({"project_id": project_id, "name": name, "role": owner_role})
    reproject_org(org_id)
    return {
        "onboarded": True, "user_id": user_id, "role": role, "level": level,
        "reports_to": saved_managers, "skills": saved_skills, "projects": saved_projects,
        "commit_history_opt_in": bool(store_commit_history),
    }


@remote_mcp.tool()
def get_org_graph(auth_token: str) -> dict[str, Any]:
    """Return the organization knowledge graph (nodes + edges) for visualization."""
    user = require_auth(auth_token)
    data = graph.fetch_org_graph(user["org_id"])
    return {"org_id": user["org_id"], "nodes": data["nodes"], "edges": data["edges"]}


@remote_mcp.tool()
def find_owner(auth_token: str, query: str) -> dict[str, Any]:
    """Find which project/package matches `query` and who owns it (creator/maintainers)."""
    user = require_auth(auth_token)
    matches = graph.find_owner(user["org_id"], query)
    return {"org_id": user["org_id"], "query": query, "matches": matches}


@remote_mcp.tool()
def ask_owner(auth_token: str, query: str, question: str) -> dict[str, Any]:
    """Find the owner of a project/package matching `query` and ask them via the inbox.

    Prefers the original creator, then any maintainer/contributor. The recipient still
    reviews and approves the reply before it sends (propose_response -> send_response).
    """
    sender = require_auth(auth_token)
    org_id = sender["org_id"]
    matches = graph.find_owner(org_id, query)
    if not matches:
        raise ValueError(f"No project matching '{query}' found in your organization.")
    top = matches[0]
    owners = top.get("owners") or []
    creator = next((o for o in owners if o.get("role") == "creator"), None)
    target = creator or (owners[0] if owners else None)
    if target is None:
        raise ValueError(f"Project '{top['name']}' has no registered owner to ask.")
    to_user_id = target["user_id"]
    now = utc_now()
    with db() as conn:
        if USE_POSTGRES:
            cursor = execute(conn,
                "INSERT INTO org_messages (org_id, from_user_id, to_user_id, query_text, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?) RETURNING id",
                (org_id, sender["user_id"], to_user_id, question, now))
            message_id = cursor.fetchone()["id"]
        else:
            cursor = execute(conn,
                "INSERT INTO org_messages (org_id, from_user_id, to_user_id, query_text, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (org_id, sender["user_id"], to_user_id, question, now))
            message_id = cursor.lastrowid
    record_interaction(org_id, sender["user_id"], to_user_id)
    reproject_org(org_id)
    return {
        "sent": True, "message_id": message_id,
        "matched_project": {"project_id": top["project_id"], "name": top["name"], "score": top["score"]},
        "asked": {"user_id": to_user_id, "name": target.get("name"), "role": target.get("role")},
        "question": question, "created_at": now,
    }


def _tokens(text: str) -> list[str]:
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _skill_token_match(tok: str, sk: str) -> bool:
    """Match a query token to a skill: exact, or substring only for tokens >=3 chars
    (so 'authentication' matches 'auth' but short tokens like 'ml'/'go' need equality)."""
    if not tok:
        return False
    if tok == sk:
        return True
    return len(tok) >= 3 and len(sk) >= 3 and (tok in sk or sk in tok)


def _skill_hits(skills: set[str], tokens: set[str]) -> list[str]:
    hits = set()
    for sk in skills:
        if any(_skill_token_match(tok, sk) for tok in tokens):
            hits.add(sk)
    return sorted(hits)


ROLE_POINTS = {"creator": 5, "maintainer": 3, "contributor": 2}


@remote_mcp.tool()
def who_should_i_ask(auth_token: str, query: str) -> dict[str, Any]:
    """Rank coworkers to ask about a package/project/topic (graph-powered routing).

    Combines project ownership (creator > maintainer > contributor), commit-contribution
    volume, matching skills, how often you already talk with them, and reporting distance
    in the org graph. Returns ranked candidates each with a score breakdown and reason.
    """
    asker = require_auth(auth_token)
    org_id, me = asker["org_id"], asker["user_id"]
    snap = _org_snapshot(org_id)
    matches = graph.find_owner(org_id, query)
    matched_pids = {m["project_id"] for m in matches[:3]}
    tokens = set(_tokens(query))

    skills_by_user: dict[str, set[str]] = defaultdict(set)
    for s in snap["skills"]:
        skills_by_user[s["user_id"]].add(s["skill"].lower())
    commits_by: dict[tuple[str, str], int] = {
        (c["user_id"], c["project_id"]): c["commits"] for c in snap["contributions"]}
    own_by: dict[str, dict[str, str]] = defaultdict(dict)
    for o in snap["ownership"]:
        own_by[o["user_id"]][o["project_id"]] = o["role"]
    familiarity: dict[str, int] = defaultdict(int)
    for i in snap["interactions"]:
        if me in (i["user_a_id"], i["user_b_id"]):
            other = i["user_b_id"] if i["user_a_id"] == me else i["user_a_id"]
            familiarity[other] += i["message_count"]
    info = {u["user_id"]: u for u in snap["users"]}

    candidates = set()
    for uid in info:
        if uid == me:
            continue
        owns_matched = any(p in matched_pids for p in own_by.get(uid, {}))
        commits_matched = any((uid, p) in commits_by for p in matched_pids)
        skill_matched = bool(_skill_hits(skills_by_user.get(uid, set()), tokens))
        if owns_matched or commits_matched or skill_matched:
            candidates.add(uid)

    ranked = []
    for uid in candidates:
        roles_here = [own_by[uid][p] for p in own_by.get(uid, {}) if p in matched_pids]
        own_pts = max([ROLE_POINTS.get(r, 0) for r in roles_here] or [0])
        commits = sum(commits_by.get((uid, p), 0) for p in matched_pids)
        commit_pts = min(commits, 50) / 10.0
        matched_skills = _skill_hits(skills_by_user.get(uid, set()), tokens)
        skill_pts = 2.0 * len(matched_skills)
        fam = familiarity.get(uid, 0)
        fam_pts = min(fam, 5) * 0.4
        dist = graph.reporting_distance(org_id, me, uid)
        close_pts = 0.0 if dist is None else max(0, 3 - dist) * 0.5
        score = own_pts + commit_pts + skill_pts + fam_pts + close_pts
        reasons = []
        if roles_here:
            best = min(roles_here, key=lambda r: -ROLE_POINTS.get(r, 0))
            reasons.append(f"{best} of a matching project")
        if commits:
            reasons.append(f"{commits} commits there")
        if matched_skills:
            reasons.append("skills: " + ", ".join(matched_skills))
        if fam:
            reasons.append(f"you've talked {fam}x")
        if dist:
            reasons.append(f"{dist} reporting hop(s) away")
        ranked.append({
            "user_id": uid, "name": info[uid]["name"], "role": info[uid]["role"],
            "level": info[uid]["level"], "score": round(score, 2),
            "breakdown": {"ownership": own_pts, "commits": round(commit_pts, 2),
                          "skills": skill_pts, "familiarity": round(fam_pts, 2),
                          "closeness": round(close_pts, 2)},
            "reason": "; ".join(reasons) or "topic/skill match",
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return {
        "org_id": org_id, "query": query,
        "matched_projects": [{"project_id": m["project_id"], "name": m["name"]} for m in matches[:3]],
        "candidates": ranked[:5],
    }


@remote_mcp.tool()
def rank_experts(auth_token: str, skill: str) -> dict[str, Any]:
    """Rank people in your org by expertise in a skill/technology.

    Scored by having the skill, total commit volume, and how many projects they own.
    """
    user = require_auth(auth_token)
    org_id = user["org_id"]
    snap = _org_snapshot(org_id)
    token = skill.strip().lower()
    commits_total: dict[str, int] = defaultdict(int)
    for c in snap["contributions"]:
        commits_total[c["user_id"]] += c["commits"]
    owns_count: dict[str, int] = defaultdict(int)
    for o in snap["ownership"]:
        owns_count[o["user_id"]] += 1
    info = {u["user_id"]: u for u in snap["users"]}

    best: dict[str, dict[str, Any]] = {}
    for s in snap["skills"]:
        sk = s["skill"].lower()
        if not _skill_token_match(token, sk):
            continue
        uid = s["user_id"]
        score = 3.0 + min(commits_total[uid], 100) / 10.0 + 1.5 * owns_count[uid]
        entry = {
            "user_id": uid, "name": info[uid]["name"], "skill": s["skill"],
            "role": info[uid]["role"], "level": info[uid]["level"],
            "total_commits": commits_total[uid], "projects_owned": owns_count[uid],
            "score": round(score, 2),
        }
        if uid not in best or entry["score"] > best[uid]["score"]:
            best[uid] = entry
    experts = sorted(best.values(), key=lambda r: r["score"], reverse=True)
    return {"org_id": org_id, "skill": skill, "experts": experts[:10]}


@remote_mcp.tool()
def set_commit_history_consent(auth_token: str, opt_in: bool) -> dict[str, Any]:
    """Set whether your commit contribution history may be stored in ctalk (opt-in).

    Opting out also deletes any commit data already stored for you.
    """
    user = require_auth(auth_token)
    org_id, user_id = user["org_id"], user["user_id"]
    with db() as conn:
        execute(conn, "UPDATE users SET commit_history_opt_in = ? WHERE org_id = ? AND user_id = ?",
                (1 if opt_in else 0, org_id, user_id))
        if not opt_in:
            execute(conn, "DELETE FROM contributions WHERE org_id = ? AND user_id = ?", (org_id, user_id))
    reproject_org(org_id)
    return {
        "updated": True, "user_id": user_id, "commit_history_opt_in": bool(opt_in),
        "note": "Commit history will be stored." if opt_in
                else "Commit history disabled; any stored commit data was deleted.",
    }


@remote_mcp.tool()
def record_commits(auth_token: str, project_id: str, commits: int, last_commit_at: str = "") -> dict[str, Any]:
    """Store your commit-contribution count for a project. Requires commit-history consent.

    Run set_commit_history_consent(opt_in=True) first (or onboard with
    store_commit_history=True). Feeds expertise ranking and who_should_i_ask.
    """
    user = require_auth(auth_token)
    org_id, user_id = user["org_id"], user["user_id"]
    project_id = normalize_id(project_id, "project_id")
    with db() as conn:
        row = execute(conn, "SELECT commit_history_opt_in FROM users WHERE org_id = ? AND user_id = ?",
                      (org_id, user_id)).fetchone()
        if not row or not row["commit_history_opt_in"]:
            raise ValueError("Commit history is not enabled. Call set_commit_history_consent(opt_in=True) first.")
        if execute(conn, "SELECT 1 FROM projects WHERE org_id = ? AND project_id = ?",
                   (org_id, project_id)).fetchone() is None:
            raise ValueError(f"Project '{project_id}' not found. Create it via onboard first.")
        execute(conn,
            """
            INSERT INTO contributions (org_id, project_id, user_id, commits, last_commit_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(org_id, project_id, user_id) DO UPDATE SET
                commits = excluded.commits, last_commit_at = excluded.last_commit_at
            """,
            (org_id, project_id, user_id, int(commits), last_commit_at))
    reproject_org(org_id)
    return {"recorded": True, "project_id": project_id, "user_id": user_id, "commits": int(commits)}


@app.get("/graph")
def graph_page() -> FileResponse:
    return FileResponse(GRAPH_PAGE)


@app.get("/api/org-graph")
def api_org_graph(auth_token: str) -> dict[str, Any]:
    try:
        user = require_auth(auth_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    data = graph.fetch_org_graph(user["org_id"])
    return {"org_id": user["org_id"], "nodes": data["nodes"], "edges": data["edges"]}


mcp_app = remote_mcp.streamable_http_app()
app.mount("/", mcp_app)
