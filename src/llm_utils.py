"""Shared OpenAI LLM calling utility for FlowDelta."""

from __future__ import annotations

import os
from typing import Optional


def call_llm(
    system_prompt: str,
    user_content: str,
    model: str = "gpt-4o",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: float = 0.2,
) -> str:
    """
    Send a chat completion request to an OpenAI-compatible API.

    Returns the assistant's response text (empty string on failure).
    """
    try:
        from openai import OpenAI  # lazy import
    except ImportError as exc:
        raise ImportError("Install openai: pip install openai>=1.0.0") from exc

    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    kwargs = {"api_key": key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content or ""
