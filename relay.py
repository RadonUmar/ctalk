import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from mcp.server.fastmcp import FastMCP

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
    async with remote_mcp.session_manager.run():
        yield


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


mcp_app = remote_mcp.streamable_http_app()
app.mount("/", mcp_app)
