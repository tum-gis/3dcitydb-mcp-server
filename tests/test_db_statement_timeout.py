"""CITYDB_STATEMENT_TIMEOUT_MS: default, override and bad values."""

import pytest

from citydb_mcp.db import _statement_timeout_ms


@pytest.mark.parametrize("raw, expected", [
    (None, 600000),        # default: 10 minutes
    ("", 600000),
    ("1800000", 1800000),  # configurable in .env
    ("0", 0),              # 0 = no limit
    ("-5", 0),
    ("ten", 600000),       # invalid -> default
])
def test_statement_timeout(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CITYDB_STATEMENT_TIMEOUT_MS", raising=False)
    else:
        monkeypatch.setenv("CITYDB_STATEMENT_TIMEOUT_MS", raw)
    assert _statement_timeout_ms() == expected
