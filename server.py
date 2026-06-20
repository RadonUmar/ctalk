import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


logging.getLogger("httpx").setLevel(logging.WARNING)

RELAY_URL = os.environ.get("RELAY_URL", "").rstrip("/")
CURRENT_USER_ID = os.environ.get("CURRENT_USER_ID", "").strip()
STATE_DIR = Path(os.environ.get("CTALK_STATE_DIR", Path.home() / ".ctalk"))
LAST_CHECKED_PATH = STATE_DIR / f"last_checked.{CURRENT_USER_ID or 'unknown'}"


mcp = FastMCP("ctalk")


def _config_error() -> str | None:
    missing = []
    if not RELAY_URL:
        missing.append("RELAY_URL")
    if not CURRENT_USER_ID:
        missing.append("CURRENT_USER_ID")
    if missing:
        return f"Missing required env var(s): {', '.join(missing)}."
    return None


def _request(method: str, path: str, **kwargs: Any) -> Any:
    config_error = _config_error()
    if config_error:
        raise RuntimeError(config_error)

    url = f"{RELAY_URL}{path}"
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        raise RuntimeError(f"Could not reach relay at {RELAY_URL}: {exc}") from exc

    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(f"Relay returned HTTP {response.status_code} for {path}: {detail}")

    if not response.content:
        return None
    return response.json()


def _get_roster(user_id: str) -> dict[str, Any] | None:
    try:
        return _request("GET", f"/roster/{user_id}")
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def _require_self_registered() -> dict[str, Any]:
    profile = _get_roster(CURRENT_USER_ID)
    if profile is None:
        raise RuntimeError(
            f"CURRENT_USER_ID '{CURRENT_USER_ID}' is not registered yet. "
            "Ask Claude Code to call register_self first."
        )
    return profile


def _read_last_checked() -> str:
    if LAST_CHECKED_PATH.exists():
        timestamp = LAST_CHECKED_PATH.read_text(encoding="utf-8").strip()
        if timestamp:
            return timestamp
    return "1970-01-01T00:00:00+00:00"


def _write_last_checked(timestamp: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_CHECKED_PATH.write_text(timestamp, encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
def register_self(name: str, ownership: list[str], notes: str = "") -> dict[str, Any]:
    """Register this Claude Code user in the shared relay roster."""
    payload = {
        "id": CURRENT_USER_ID,
        "name": name,
        "ownership": ownership,
        "notes": notes,
    }
    entry = _request("POST", "/roster", json=payload)
    return {
        "registered": True,
        "profile": entry,
        "message": f"Registered {CURRENT_USER_ID} as {name}.",
    }


@mcp.tool()
def ask_person(to_id: str, question: str) -> dict[str, Any]:
    """Ask a registered coworker a question through the relay."""
    _require_self_registered()
    recipient = _get_roster(to_id)
    if recipient is None:
        raise RuntimeError(
            f"'{to_id}' is not registered in the relay roster yet. "
            "Ask them to run register_self in their Claude Code session first."
        )

    result = _request(
        "POST",
        "/messages",
        json={"from_id": CURRENT_USER_ID, "to_id": to_id, "question": question},
    )
    return {
        "sent": True,
        "message_id": result["id"],
        "to": recipient,
        "question": question,
    }


@mcp.tool()
def check_inbox() -> dict[str, Any]:
    """Check pending questions for this user, including sender roster context."""
    _require_self_registered()
    messages = _request("GET", f"/inbox/{CURRENT_USER_ID}")
    enriched = []
    for message in messages:
        sender = _get_roster(message["from_id"])
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
    return {"user_id": CURRENT_USER_ID, "pending": enriched}


@mcp.tool()
def propose_response(message_id: int, draft_text: str) -> dict[str, Any]:
    """Save a draft response for human review without delivering it."""
    _require_self_registered()
    result = _request("POST", f"/messages/{message_id}/draft", json={"draft_text": draft_text})
    return {
        "draft_saved": True,
        "message_id": result["id"],
        "draft_text": draft_text,
        "approval_required": "Review this draft with the human before calling send_response.",
    }


@mcp.tool()
def send_response(message_id: int, final_text: str) -> dict[str, Any]:
    """
    Deliver a final response after explicit human approval.

    Do not call this tool unless the human has approved final_text in this
    Claude Code session. Use propose_response first for drafting.
    """
    _require_self_registered()
    result = _request("POST", f"/messages/{message_id}/respond", json={"final_text": final_text})
    return {
        "sent": True,
        "message_id": result["id"],
        "answered_at": result["answered_at"],
        "final_text": final_text,
    }


@mcp.tool()
def check_replies() -> dict[str, Any]:
    """Check answers to questions this user previously asked."""
    _require_self_registered()
    since = _read_last_checked()
    checked_at = _now()
    replies = _request("GET", f"/replies/{CURRENT_USER_ID}", params={"since": since})
    _write_last_checked(checked_at)
    return {
        "user_id": CURRENT_USER_ID,
        "since": since,
        "checked_at": checked_at,
        "replies": replies,
    }


if __name__ == "__main__":
    mcp.run()
