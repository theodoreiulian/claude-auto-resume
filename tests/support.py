"""Shared fixtures for the Conductor-backed tests."""

import json

LIMIT_TEXT = "You've hit your session limit · resets 10:20pm (Europe/Paris)"

# Verbatim from a Conductor Codex session.
CODEX_LIMIT_TEXT = (
    "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
    "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try "
    "again at 11:54 PM."
)

# The subset of Conductor's schema the app actually reads.
SCHEMA = """
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY, workspace_name TEXT, directory_name TEXT,
    active_session_id TEXT, state TEXT DEFAULT 'active', updated_at TEXT,
    branch TEXT
);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, agent_type TEXT, title TEXT,
    status TEXT DEFAULT 'idle', is_hidden INTEGER DEFAULT 0
);
CREATE TABLE session_messages (
    id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
    created_at TEXT, cancelled_at TEXT
);
"""


def synthetic(text: str = LIMIT_TEXT) -> str:
    """A notice Claude Code generated itself — how a real limit arrives."""
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "model": "<synthetic>",
                    "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False)


def codex_error(text: str = CODEX_LIMIT_TEXT, will_retry: bool = False) -> str:
    """A turn error as Conductor stores it for Codex — how a Codex usage limit arrives."""
    return json.dumps({
        "type": "error", "content": text, "session_id": "01a08796",
        "willRetry": will_retry,
        "errorInfo": {"responseStreamDisconnected": {}} if will_retry else "usageLimitExceeded",
    }, ensure_ascii=False)


def assistant_prose(text: str) -> str:
    """Ordinary model output, which may quote the limit text without meaning it."""
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "model": "claude-opus-5",
                    "content": [{"type": "text", "text": text}]},
    }, ensure_ascii=False)
