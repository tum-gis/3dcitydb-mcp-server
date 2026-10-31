"""Session layer: who is asking, which tab, which turn.

Three levels (see production/docs/session-layer.md):

- user    — durable, a uuid the browser keeps in localStorage (no login).
- session — one browser tab, Gradio's ``request.session_hash``.
- turn    — one question; owns its own stop flag.

Later per-user features (scratchpad, RAG memory, uploads, web search) build on
``UserContext``, ``user_dir`` and ``register_purge_hook``.
"""

import concurrent.futures as _cf
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LAST_SEEN = ".last_seen"


def _valid_user_id(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip().lower()
    return raw if _UUID_RE.match(raw) else None


# ── Turn control (replaces the old process-wide stop flag) ────────────────────

class TurnControl:
    """Stop flag + in-flight future of one chat turn. Backends poll ``stopped()``
    and register their LLM future with ``set_future`` so Stop can cancel it."""

    def __init__(self, turn_id: str | None = None):
        self.turn_id = turn_id or uuid.uuid4().hex[:12]
        self._event = threading.Event()
        self._future: "_cf.Future | None" = None
        self._lock = threading.Lock()

    def stopped(self) -> bool:
        return self._event.is_set()

    def stop(self) -> None:
        self._event.set()
        with self._lock:
            fut = self._future
        if fut is not None:
            fut.cancel()

    def set_future(self, fut: "_cf.Future | None") -> None:
        with self._lock:
            self._future = fut


@dataclass(frozen=True)
class UserContext:
    user_id: str
    session_id: str
    turn_id: str = ""

    def to_tool_ctx(self) -> dict:
        """The ``_ctx`` the web UI adds to MCP tool arguments (never the LLM)."""
        return {"user_id": self.user_id, "session_id": self.session_id, "turn_id": self.turn_id}


# ── Registry: session → user, session → running turn ─────────────────────────

_lock = threading.Lock()
_users: dict[str, str] = {}
_turns: dict[str, TurnControl] = {}


def register_user(session_id: str, raw_user_id=None) -> str:
    """Bind a tab to a user. An invalid or missing id gets a fresh uuid."""
    user_id = _valid_user_id(raw_user_id) or str(uuid.uuid4())
    with _lock:
        _users[session_id] = user_id
    return user_id


def user_for(session_id: str) -> str:
    """The tab's user; registers a fresh one if the page never sent its id."""
    with _lock:
        user_id = _users.get(session_id)
    return user_id or register_user(session_id)


def forget_session(session_id: str) -> None:
    with _lock:
        _users.pop(session_id, None)
        turn = _turns.pop(session_id, None)
    if turn is not None:
        turn.stop()


def begin_turn(session_id: str) -> tuple[UserContext, TurnControl]:
    turn = TurnControl()
    with _lock:
        _turns[session_id] = turn
    return UserContext(user_for(session_id), session_id, turn.turn_id), turn


def end_turn(session_id: str, turn: TurnControl) -> None:
    with _lock:
        if _turns.get(session_id) is turn:
            del _turns[session_id]


def stop_session(session_id: str) -> bool:
    """Stop the running turn of this tab only. Returns False if none runs."""
    with _lock:
        turn = _turns.get(session_id)
    if turn is None:
        return False
    turn.stop()
    return True


def active_sessions() -> int:
    with _lock:
        return len(_users)


# ── Per-user data folder + expiry ─────────────────────────────────────────────

def user_data_root() -> str:
    return os.environ.get("CITYDB_USER_DATA_DIR", "/app/data/users")


def user_dir(user_id: str, create: bool = True) -> str:
    """Folder for one user's files. Only uuids are accepted, so no traversal."""
    valid = _valid_user_id(user_id)
    if valid is None:
        raise ValueError(f"invalid user id: {user_id!r}")
    path = os.path.join(user_data_root(), valid)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def touch_user(user_id: str) -> None:
    """Record activity (once per turn); drives expiry."""
    try:
        with open(os.path.join(user_dir(user_id), _LAST_SEEN), "w") as fh:
            fh.write(str(int(time.time())))
    except (OSError, ValueError) as exc:
        print(f"[session] touch_user failed: {exc}", flush=True)


def last_seen(user_id: str) -> float | None:
    path = os.path.join(user_dir(user_id, create=False), _LAST_SEEN)
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


_purge_hooks: list[Callable[[str], None]] = []


def register_purge_hook(fn: Callable[[str], None]) -> None:
    """Called with the user id when an inactive user is purged (e.g. delete the
    user's scratchpad rows). Exceptions are logged, never stop the purge."""
    if fn not in _purge_hooks:
        _purge_hooks.append(fn)


def purge_inactive_users(days: float, now: float | None = None) -> list[str]:
    """Delete the data of users not seen for ``days``. Returns the purged ids."""
    root = user_data_root()
    if days <= 0 or not os.path.isdir(root):
        return []
    cutoff = (now if now is not None else time.time()) - days * 86_400
    with _lock:
        online = set(_users.values())
    purged = []
    for name in os.listdir(root):
        user_id = _valid_user_id(name)
        if user_id is None or user_id in online:
            continue
        seen = last_seen(user_id)
        if seen is None:
            seen = os.path.getmtime(os.path.join(root, name))
        if seen >= cutoff:
            continue
        for hook in list(_purge_hooks):
            try:
                hook(user_id)
            except Exception as exc:
                print(f"[session] purge hook {getattr(hook, '__name__', hook)} failed: {exc}", flush=True)
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        purged.append(user_id)
    return purged


_purge_timer_started = False


def start_purge_timer(interval_s: float = 86_400) -> None:
    """Purge at startup and then every ``interval_s``. ``CITYDB_USER_EXPIRY_DAYS``
    (default 14) sets the inactivity window; 0 disables."""
    global _purge_timer_started
    try:
        days = float(os.environ.get("CITYDB_USER_EXPIRY_DAYS", "14"))
    except ValueError:
        days = 14.0
    if days <= 0 or _purge_timer_started:
        return
    _purge_timer_started = True

    def _loop():
        try:
            purged = purge_inactive_users(days)
            if purged:
                print(f"[session] purged {len(purged)} inactive user(s)", flush=True)
        except Exception as exc:
            print(f"[session] purge failed: {exc}", flush=True)
        t = threading.Timer(interval_s, _loop)
        t.daemon = True
        t.start()

    threading.Thread(target=_loop, name="user-purge", daemon=True).start()
