"""Tests for the session layer: per-tab stop, user registry, user folders,
expiry, and the _ctx hand-over from the web UI to the MCP server.

Usage:
    pytest tests/test_session_layer.py -v
"""

import asyncio
import concurrent.futures as cf
import os
import sys
import time

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from citydb_mcp import tool_context  # noqa: E402
from webui import session  # noqa: E402

UID_A = "11111111-1111-4111-8111-111111111111"
UID_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CITYDB_USER_DATA_DIR", str(tmp_path / "users"))
    session._users.clear()
    session._turns.clear()
    session._purge_hooks.clear()
    yield
    session._users.clear()
    session._turns.clear()
    session._purge_hooks.clear()


# ── Turns and Stop ────────────────────────────────────────────────────────────

def test_stop_only_hits_the_own_session():
    session.register_user("tab-a", UID_A)
    session.register_user("tab-b", UID_B)
    ctx_a, turn_a = session.begin_turn("tab-a")
    ctx_b, turn_b = session.begin_turn("tab-b")

    assert session.stop_session("tab-a") is True
    assert turn_a.stopped() and not turn_b.stopped()
    assert (ctx_a.user_id, ctx_b.user_id) == (UID_A, UID_B)


def test_new_turn_does_not_clear_another_sessions_stop():
    _, turn_a = session.begin_turn("tab-a")
    session.stop_session("tab-a")
    session.begin_turn("tab-b")  # used to call the global _clear_stop()
    assert turn_a.stopped()


def test_stop_cancels_the_registered_future():
    turn = session.TurnControl()
    fut = cf.Future()
    turn.set_future(fut)
    turn.stop()
    assert fut.cancelled()


def test_end_turn_keeps_a_newer_turn_of_the_same_tab():
    _, old = session.begin_turn("tab-a")
    _, new = session.begin_turn("tab-a")
    session.end_turn("tab-a", old)
    assert session.stop_session("tab-a") is True and new.stopped()
    session.end_turn("tab-a", new)
    assert session.stop_session("tab-a") is False


# ── User registry ─────────────────────────────────────────────────────────────

def test_register_keeps_valid_id_and_replaces_invalid_ones():
    assert session.register_user("tab-a", UID_A.upper()) == UID_A
    fresh = session.register_user("tab-b", "../../etc")
    assert fresh != "../../etc" and session._valid_user_id(fresh)
    assert session.register_user("tab-c", None) != fresh


def test_user_for_unknown_tab_gets_a_fresh_id_and_forget_stops_its_turn():
    uid = session.user_for("tab-x")
    assert session.user_for("tab-x") == uid
    _, turn = session.begin_turn("tab-x")
    session.forget_session("tab-x")
    assert turn.stopped() and "tab-x" not in session._users


def test_tool_ctx_shape():
    ctx = session.UserContext(UID_A, "tab-a", "t1")
    assert ctx.to_tool_ctx() == {"user_id": UID_A, "session_id": "tab-a", "turn_id": "t1"}


# ── User folders and expiry ───────────────────────────────────────────────────

def test_user_dir_is_created_and_rejects_non_uuids():
    path = session.user_dir(UID_A)
    assert os.path.isdir(path) and path.endswith(UID_A)
    for bad in ("..", "../x", "", "abc"):
        with pytest.raises(ValueError):
            session.user_dir(bad)


def test_purge_removes_only_inactive_offline_users_and_runs_hooks():
    seen = []
    session.register_purge_hook(seen.append)
    session.touch_user(UID_A)
    session.touch_user(UID_B)
    old = time.time() - 30 * 86_400
    os.utime(os.path.join(session.user_dir(UID_A), ".last_seen"), (old, old))
    os.makedirs(os.path.join(session.user_data_root(), "not-a-user"))

    assert session.purge_inactive_users(14) == [UID_A]
    assert seen == [UID_A]
    assert not os.path.exists(os.path.join(session.user_data_root(), UID_A))
    assert os.path.isdir(session.user_dir(UID_B, create=False))
    assert os.path.isdir(os.path.join(session.user_data_root(), "not-a-user"))


def test_purge_skips_users_with_an_open_tab_and_survives_hook_errors():
    def boom(_uid):
        raise RuntimeError("hook failed")

    session.register_purge_hook(boom)
    for uid in (UID_A, UID_B):
        session.touch_user(uid)
        old = time.time() - 30 * 86_400
        os.utime(os.path.join(session.user_dir(uid), ".last_seen"), (old, old))
    session.register_user("tab-b", UID_B)

    assert session.purge_inactive_users(14) == [UID_A]
    assert session.purge_inactive_users(0) == []


# ── _ctx from the web UI to the MCP server ────────────────────────────────────

def test_split_tool_ctx_strips_and_sanitizes():
    args, ctx = tool_context.split_tool_ctx(
        {"sql": "SELECT 1", "_ctx": {"user_id": UID_A, "session_id": "s", "evil": "x"}})
    assert args == {"sql": "SELECT 1"}
    assert ctx == {"user_id": UID_A, "session_id": "s"}
    assert tool_context.split_tool_ctx({"sql": "x", "_ctx": "nope"}) == ({"sql": "x"}, {})


def test_current_tool_ctx_is_scoped_to_the_call():
    token = tool_context.set_tool_ctx({"user_id": UID_A})
    assert tool_context.current_tool_ctx() == {"user_id": UID_A}
    tool_context.reset_tool_ctx(token)
    assert tool_context.current_tool_ctx() == {}


def test_mcp_client_adds_ctx_only_when_given(monkeypatch):
    pytest.importorskip("mcp")
    from webui import mcp_client

    sent = []

    class FakeSession:
        pass

    class FakeCM:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, *exc):
            return False

    async def fake_call_tool(_session, name, arguments):
        sent.append((name, arguments))
        return "ok"

    monkeypatch.setattr(mcp_client, "mcp_session", lambda **_kw: FakeCM())
    monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)
    asyncio.run(mcp_client.run_tool("run_query", {"sql": "SELECT 1"}, ctx={"user_id": UID_A}))
    asyncio.run(mcp_client.run_tool("run_query", {"sql": "SELECT 1"}))
    assert sent == [("run_query", {"sql": "SELECT 1", "_ctx": {"user_id": UID_A}}),
                    ("run_query", {"sql": "SELECT 1"})]


# ── Backends stop through the turn ────────────────────────────────────────────

def test_dispatcher_forwards_the_turn(monkeypatch):
    from webui import backends
    from webui.backends import cloud

    seen = {}

    def fake_native(*_args, turn=None, **_kw):
        seen["turn"] = turn
        yield ("final", "x")

    monkeypatch.setattr(cloud, "native_tool_stream", fake_native)
    turn = session.TurnControl()
    assert list(backends.stream("openai", "m", None, [], lambda _s: "", turn=turn)) == [("final", "x")]
    assert seen["turn"] is turn


def test_cloud_backend_exits_when_its_turn_is_stopped(monkeypatch):
    from webui.backends import cloud

    monkeypatch.setattr(cloud, "_litellm_kwargs", lambda *a, **k: {"model": "m"})
    turn = session.TurnControl()
    turn.stop()
    events = list(cloud.native_tool_stream("openai", "m", None,
                                           [{"role": "user", "content": "hi"}],
                                           lambda _s: "", turn=turn))
    assert events == [("stopped", "")]
