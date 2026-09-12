"""Unit test for the assemble_prompt process-lifetime cache.

These tests monkeypatch the expensive ``_assemble_prompt_uncached`` builder
so they run without a database connection.

Usage:
    pytest tests/test_assembly_cache.py -v
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from citydb_mcp.tools import assembly


def _fresh_cache():
    with assembly._prompt_cache_lock:
        assembly._prompt_cache.clear()


def test_first_call_builds_and_second_hits_cache(monkeypatch):
    _fresh_cache()
    calls = {"n": 0}

    def fake_build(db, include_query_agent_extras=True, compact=False):
        calls["n"] += 1
        return f"prompt-{compact}"

    monkeypatch.setattr(assembly, "_assemble_prompt_uncached", fake_build)

    first = assembly.assemble_prompt(None, compact=True)
    second = assembly.assemble_prompt(None, compact=True)

    assert first == "prompt-True"
    assert second == "prompt-True"
    assert calls["n"] == 1, "second call should be served from the cache"


def test_force_refresh_rebuilds(monkeypatch):
    _fresh_cache()
    calls = {"n": 0}

    def fake_build(db, include_query_agent_extras=True, compact=False):
        calls["n"] += 1
        return f"prompt-{calls['n']}"

    monkeypatch.setattr(assembly, "_assemble_prompt_uncached", fake_build)

    assembly.assemble_prompt(None)
    refreshed = assembly.assemble_prompt(None, force_refresh=True)

    assert calls["n"] == 2
    assert refreshed == "prompt-2"


def test_invalid_cache_busts(monkeypatch):
    _fresh_cache()
    calls = {"n": 0}

    def fake_build(db, include_query_agent_extras=True, compact=False):
        calls["n"] += 1
        return "x"

    monkeypatch.setattr(assembly, "_assemble_prompt_uncached", fake_build)

    assembly.assemble_prompt(None)
    assembly.invalidate_prompt_cache()
    assembly.assemble_prompt(None)

    assert calls["n"] == 2, "invalidate_prompt_cache should force a rebuild"


def test_variants_are_cached_independently(monkeypatch):
    _fresh_cache()
    calls = {"n": 0}

    def fake_build(db, include_query_agent_extras=True, compact=False):
        calls["n"] += 1
        return f"prompt-extras={include_query_agent_extras},compact={compact}"

    monkeypatch.setattr(assembly, "_assemble_prompt_uncached", fake_build)

    a = assembly.assemble_prompt(None, compact=False)
    b = assembly.assemble_prompt(None, compact=True)
    c = assembly.assemble_prompt(None, include_query_agent_extras=False)

    assert a == "prompt-extras=True,compact=False"
    assert b == "prompt-extras=True,compact=True"
    assert c == "prompt-extras=False,compact=False"
    # Three distinct variants -> three builds; repeat calls should not add more.
    assembly.assemble_prompt(None, compact=False)
    assembly.assemble_prompt(None, compact=True)
    assembly.assemble_prompt(None, include_query_agent_extras=False)
    assert calls["n"] == 3


def teardown_function():
    _fresh_cache()
