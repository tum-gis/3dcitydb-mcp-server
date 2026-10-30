"""Backend dispatcher: routes to cloud (native tool calling) or local (ReAct) backend."""

from typing import Callable, Generator


def stream(
    provider: str,
    model: str,
    temperature: float | None,
    messages: list[dict],
    tool_executor: Callable[[str], str],
    *,
    enable_thinking: bool | str = False,
    num_ctx: int | None = None,
    turn=None,
    extra_tools: dict | None = None,
    full_lists: bool = True,
) -> Generator[tuple, None, None]:
    if provider == "ollama":
        from webui.backends.local import react_stream
        yield from react_stream(
            provider, model, temperature, messages, tool_executor,
            enable_thinking=enable_thinking,
            num_ctx=num_ctx,
            turn=turn,
            extra_tools=extra_tools,
            full_lists=full_lists,
        )
    elif provider in ("anthropic", "openai"):
        from webui.backends.cloud import native_tool_stream
        yield from native_tool_stream(
            provider, model, temperature, messages, tool_executor,
            enable_thinking=enable_thinking,
            turn=turn,
            extra_tools=extra_tools,
            full_lists=full_lists,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")
