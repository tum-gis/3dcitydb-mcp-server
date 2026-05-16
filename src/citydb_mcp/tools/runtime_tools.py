"""Query execution and user context tools."""

import os
import threading
import time
import uuid
from datetime import datetime
from ..db import DatabaseConnection
from ..models import (
    UserSessionContext, UserModuleSelection,
    HistoryContext, Message, QueryFeedback
)

# Bounded in-memory session store. Sessions expire after _SESSION_TTL_SECONDS
# of inactivity; if the store exceeds _SESSION_MAX, the oldest are evicted.
_SESSION_TTL_SECONDS = int(os.getenv("CITYDB_SESSION_TTL", "3600"))
_SESSION_MAX = int(os.getenv("CITYDB_SESSION_MAX", "256"))
_sessions_lock = threading.Lock()


# ============================================================
# run_query
# ============================================================

def run_query(db: DatabaseConnection, sql: str, row_limit: int = 500) -> dict:
    """
    Executes read-only SQL against 3DCityDB.
    Returns results as JSON with column names, rows, execution time.
    Maps to: QueryFeedback (execution_time_ms, result_count, error_message)
    """
    # Safety: only allow SELECT statements
    sql_stripped = sql.strip().upper()
    if not sql_stripped.startswith("SELECT") and not sql_stripped.startswith("WITH"):
        return {
            "success": False,
            "error": "Only SELECT and WITH (CTE) queries are allowed.",
            "results": [],
            "row_count": 0,
            "execution_time_ms": 0,
        }

    # Enforce row limit if not already present
    if "LIMIT" not in sql_stripped:
        sql = f"{sql.rstrip(';')} LIMIT {row_limit};"

    start = time.time()
    try:
        results = db.execute(sql)
        elapsed_ms = int((time.time() - start) * 1000)

        return {
            "success": True,
            "error": "",
            "results": results,
            "row_count": len(results),
            "execution_time_ms": elapsed_ms,
            "columns": list(results[0].keys()) if results else [],
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            "success": False,
            "error": str(e),
            "results": [],
            "row_count": 0,
            "execution_time_ms": elapsed_ms,
        }


# ============================================================
# User context management
# ============================================================

# In-memory session store (per server instance). Each entry carries a
# `last_used` monotonic timestamp; the store is GC'd in _touch_session.
_sessions: dict[str, dict] = {}


def _gc_sessions_locked(now: float) -> None:
    """Evict expired entries; then LRU-trim if still over the cap.

    Caller must hold _sessions_lock.
    """
    expired = [
        sid for sid, entry in _sessions.items()
        if now - entry.get("last_used", now) > _SESSION_TTL_SECONDS
    ]
    for sid in expired:
        _sessions.pop(sid, None)

    if len(_sessions) > _SESSION_MAX:
        # Drop oldest (smallest last_used) until back under cap.
        overflow = len(_sessions) - _SESSION_MAX
        oldest = sorted(_sessions.items(), key=lambda kv: kv[1].get("last_used", 0))
        for sid, _ in oldest[:overflow]:
            _sessions.pop(sid, None)


def _touch_session(session_id: str) -> None:
    entry = _sessions.get(session_id)
    if entry is not None:
        entry["last_used"] = time.monotonic()


def get_session_context(session_id: str = None) -> UserSessionContext:
    """
    Returns or creates a session context.
    Maps to UML: UserSessionContext
    """
    now = time.monotonic()
    with _sessions_lock:
        if session_id and session_id in _sessions:
            _sessions[session_id]["last_used"] = now
            return _sessions[session_id]["context"]

        _gc_sessions_locked(now)

        new_session = UserSessionContext(
            session_id=session_id or str(uuid.uuid4()),
            started_at=datetime.now(),
        )
        _sessions[new_session.session_id] = {
            "context": new_session,
            "module_selection": None,
            "history": HistoryContext(),
            "last_used": now,
        }
        return new_session


def update_module_selection(
    session_id: str,
    objectclass_ids: list[int],
    modules: list[str] = None,
    reason: str = ""
) -> UserModuleSelection:
    """
    Updates the user's module/objectclass selection for the session.
    Maps to UML: UserModuleSelection
    """
    selection = UserModuleSelection(
        selected_modules=modules or [],
        selected_objectclass_ids=objectclass_ids,
        timestamp=datetime.now(),
        selection_reason=reason,
    )

    with _sessions_lock:
        if session_id in _sessions:
            _sessions[session_id]["module_selection"] = selection
            _touch_session(session_id)

    return selection


def get_history(session_id: str) -> HistoryContext:
    """
    Returns conversation history for the session.
    Maps to UML: HistoryContext + Message
    """
    with _sessions_lock:
        if session_id in _sessions:
            _touch_session(session_id)
            return _sessions[session_id]["history"]
    return HistoryContext()


def add_to_history(
    session_id: str,
    message_type: str,
    token_count: int,
    was_successful: bool
):
    """Adds a message to the session history."""
    with _sessions_lock:
        if session_id not in _sessions:
            return
        history = _sessions[session_id]["history"]
        _touch_session(session_id)

    msg = Message(
        timestamp=datetime.now(),
        number_of_tokens=token_count,
        message_type=message_type,
        was_successful=was_successful,
    )
    history.last_n_messages.append(msg)

    # Enforce sliding window
    if len(history.last_n_messages) > history.max_window_size:
        history.last_n_messages = history.last_n_messages[-history.max_window_size:]


def submit_feedback(
    session_id: str,
    query: str,
    rating: int,
    execution_time_ms: int = 0,
    result_count: int = 0,
    error: str = ""
) -> QueryFeedback:
    """
    Logs query feedback.
    Maps to UML: QueryFeedback
    """
    feedback = QueryFeedback(
        query_text=query,
        execution_time_ms=execution_time_ms,
        result_count=result_count,
        error_message=error,
        timestamp=datetime.now(),
        user_rating=rating,
    )

    with _sessions_lock:
        if session_id in _sessions:
            history = _sessions[session_id]["history"]
            _touch_session(session_id)
        else:
            history = None

    if history is not None:
        history.feedbacks.append(feedback)
        if error:
            history.failed_queries.append(query)
        else:
            history.successful_queries.append(query)

    return feedback
