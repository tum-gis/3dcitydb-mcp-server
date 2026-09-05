"""Tests for Ollama model discovery through OpenAI-compatible mode."""

import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import patch

for _cand in (
    os.path.join(os.path.dirname(__file__), "..", "production"),
    "/app",
):
    if os.path.isdir(os.path.join(_cand, "webui")):
        sys.path.insert(0, _cand)
        break

from webui import llm_utils  # noqa: E402


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_openai_base_url_normalization() -> None:
    assert llm_utils._ollama_root_from_openai_base("http://host:11434/v1/") == "http://host:11434"
    assert llm_utils._ollama_root_from_openai_base("http://host:11434/v1") == "http://host:11434"
    assert llm_utils._ollama_root_from_openai_base("http://host:11434/api") == "http://host:11434/api"


def test_discovers_models_from_ollama_tags() -> None:
    captured = []

    def fake_urlopen(request, timeout=0):
        captured.append((request.full_url, timeout))
        return _Response({"models": [{"name": "qwen3:8b"}, {"name": "granite4.2:30b"}]})

    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "ollama", "OPENAI_BASE_URL": "http://ollama:11434/v1/"},
        clear=False,
    ), patch.object(llm_utils.urllib.request, "urlopen", fake_urlopen):
        assert llm_utils.get_openai_ollama_models() == ["qwen3:8b", "granite4.2:30b"]

    assert captured == [("http://ollama:11434/api/tags", 5)]


def test_non_ollama_key_returns_no_discovered_models() -> None:
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "sk-example", "OPENAI_BASE_URL": "http://ollama:11434/v1/"},
        clear=False,
    ), patch.object(llm_utils.urllib.request, "urlopen") as urlopen:
        assert llm_utils.get_openai_ollama_models() == []
        urlopen.assert_not_called()


def test_failed_probe_falls_back_to_static_openai_models() -> None:
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "ollama", "OPENAI_BASE_URL": "http://not-ollama/v1/"},
        clear=False,
    ), patch.object(llm_utils.urllib.request, "urlopen", side_effect=OSError("not Ollama")):
        assert llm_utils.models_for_provider("openai") == llm_utils.OPENAI_MODELS


def test_default_provider_precedence() -> None:
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "ollama",
            "OPENAI_BASE_URL": "http://ollama:11434/v1/",
            "OLLAMA_BASE_URL": "http://ollama:11434",
            "ANTHROPIC_API_KEY": "anthropic-key",
        },
        clear=False,
    ), patch.object(llm_utils, "_ollama_reachable", return_value=True):
        assert llm_utils.detect_default_provider() == "openai"

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "",
            "OPENAI_BASE_URL": "",
            "OPENAI_API_BASE": "",
            "OLLAMA_BASE_URL": "http://ollama:11434",
            "ANTHROPIC_API_KEY": "anthropic-key",
        },
        clear=False,
    ), patch.object(llm_utils, "_ollama_reachable", return_value=True):
        assert llm_utils.detect_default_provider() == "ollama"

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "",
            "OPENAI_BASE_URL": "",
            "OPENAI_API_BASE": "",
            "ANTHROPIC_API_KEY": "anthropic-key",
        },
        clear=False,
    ), patch.object(llm_utils, "_ollama_reachable", return_value=False):
        assert llm_utils.detect_default_provider() == "anthropic"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
