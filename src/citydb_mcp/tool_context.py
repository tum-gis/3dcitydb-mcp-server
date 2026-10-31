"""Per-call session context sent by the web UI as the reserved ``_ctx`` argument.

The web UI adds ``_ctx = {"user_id", "session_id", "turn_id"}`` to every tool
call it makes (the LLM never builds tool arguments directly). ``call_tool``
strips it before dispatch, so existing tools see unchanged arguments, and
per-user tools read it with ``current_tool_ctx()``.
"""

import contextvars

_CTX_KEY = "_ctx"
_FIELDS = ("user_id", "session_id", "turn_id")

_tool_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("citydb_tool_ctx", default={})


def split_tool_ctx(arguments: dict | None) -> tuple[dict, dict]:
    """Return ``(arguments without _ctx, sanitized ctx)``."""
    arguments = dict(arguments or {})
    raw = arguments.pop(_CTX_KEY, None)
    ctx = {}
    if isinstance(raw, dict):
        ctx = {k: str(raw[k]) for k in _FIELDS if raw.get(k)}
    return arguments, ctx


def set_tool_ctx(ctx: dict) -> contextvars.Token:
    return _tool_ctx.set(dict(ctx))


def reset_tool_ctx(token: contextvars.Token) -> None:
    _tool_ctx.reset(token)


def current_tool_ctx() -> dict:
    """The context of the tool call being executed ({} outside the web UI)."""
    return dict(_tool_ctx.get())
