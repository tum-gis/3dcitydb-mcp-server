"""Backend dispatcher: routes to cloud (native tool calling) or local (ReAct) backend."""

from typing import Callable, Generator


def stream(
    provider: str,
    model: str,
    temperature: float,
    messages: list[dict],
    tool_executor: Callable[[str], str],
    *,
    enable_thinking: bool = False,
    num_ctx: int | None = None,
) -> Generator[tuple, None, None]:
    if provider == "ollama":
        from webui.backends.local import react_stream
        yield from react_stream(
            provider, model, temperature, messages, tool_executor,
            enable_thinking=enable_thinking,
            num_ctx=num_ctx,
        )
    elif provider in ("anthropic", "openai"):
        from webui.backends.cloud import native_tool_stream
        yield from native_tool_stream(
            provider, model, temperature, messages, tool_executor,
            enable_thinking=enable_thinking,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")
