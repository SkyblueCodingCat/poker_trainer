"""LLM provider abstraction.

Supports two providers, selected via the LLM_PROVIDER env var:
  - "anthropic" (default) — uses the official anthropic Python SDK,
    which honors ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN /
    ANTHROPIC_API_KEY. Works with the official API and any
    Anthropic-compatible relay (e.g. mify).
  - "openai" — uses the openai SDK; reads OPENAI_API_KEY,
    optionally OPENAI_BASE_URL and OPENAI_MODEL.

Each provider implements chat(system, user, max_tokens) -> str.
"""
from __future__ import annotations

import os
from typing import Optional


_provider_cache = None


def _get_provider():
    global _provider_cache
    if _provider_cache is not None:
        return _provider_cache

    name = (os.environ.get("LLM_PROVIDER") or "anthropic").lower().strip()

    if name == "anthropic":
        _provider_cache = AnthropicProvider()
    elif name == "openai":
        _provider_cache = OpenAIProvider()
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {name!r} (expected 'anthropic' or 'openai')")

    return _provider_cache


def chat(system: str, user: str, max_tokens: int = 300) -> str:
    """Single-turn chat. Returns the model's text response."""
    return _get_provider().chat(system, user, max_tokens)


# ---- Anthropic ----

class AnthropicProvider:
    def __init__(self) -> None:
        from anthropic import Anthropic  # lazy import so OpenAI users don't need it
        kwargs = {}
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if auth_token:
            kwargs["auth_token"] = auth_token
        elif api_key:
            kwargs["api_key"] = api_key
        self.client = Anthropic(**kwargs)
        # default to a recent Opus, but allow override
        self.model = (
            os.environ.get("POKER_AI_MODEL")
            or os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or "claude-opus-4-5"
        )

    def chat(self, system: str, user: str, max_tokens: int = 300) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text"))


# ---- OpenAI ----

class OpenAIProvider:
    def __init__(self) -> None:
        from openai import OpenAI  # lazy import
        kwargs = {}
        base_url = os.environ.get("OPENAI_BASE_URL")
        api_key = os.environ.get("OPENAI_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self.client = OpenAI(**kwargs)
        self.model = (
            os.environ.get("POKER_AI_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or "gpt-4o"
        )

    def chat(self, system: str, user: str, max_tokens: int = 300) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""
