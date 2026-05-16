"""Token counting utilities for prompt evaluation."""


def count_tokens_ollama(text: str, model: str = "qwen2.5:32b-instruct") -> int:
    """Count tokens using Ollama's tokenizer via LangChain."""
    try:
        from langchain_ollama import ChatOllama
        llm = ChatOllama(model=model)
        return llm.get_num_tokens(text)
    except Exception as e:
        # Fallback: rough estimate
        print(f"Warning: Could not use Ollama tokenizer: {e}")
        return len(text) // 4


def count_tokens_tiktoken(text: str, model: str = "gpt-4o") -> int:
    """Count tokens using OpenAI's tiktoken."""
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return len(text) // 4


if __name__ == "__main__":
    import sys
    from .db import DatabaseConnection
    from .tools.assembly import assemble_prompt

    db = DatabaseConnection()
    prompt = assemble_prompt(db)
    db.close()

    print(f"Characters: {len(prompt)}")
    print(f"Tokens (Ollama): {count_tokens_ollama(prompt)}")
    print(f"Tokens (tiktoken/GPT-4o): {count_tokens_tiktoken(prompt)}")