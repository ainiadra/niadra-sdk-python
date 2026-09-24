"""Reads what a model provider reported for one call: every input token, the ones read from the
provider's prompt cache and the ones written to it.

Two shapes are understood, from the response object or its `usage` attribute (objects or dicts):

- OpenAI chat completions and compatible gateways: `usage.prompt_tokens` (cached tokens included) and
  `usage.prompt_tokens_details.cached_tokens`; the Responses API's `input_tokens` with
  `input_tokens_details.cached_tokens` too. Gateways that pass Anthropic's cache fields through
  (`cache_read_input_tokens`, `cache_creation_input_tokens`) are read as well.
- Anthropic messages: `usage.input_tokens` counts only the uncached rest, so the prompt is
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.

Nothing here raises: a response without usage gives None.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _get(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def token_counts(usage: Any) -> dict[str, int] | None:
    """`prompt_tokens`, `cached_tokens` and `cache_write_tokens` from a provider's usage, or None."""
    read = _count(_get(usage, "cache_read_input_tokens")) or 0
    written = _count(_get(usage, "cache_creation_input_tokens")) or 0
    prompt = _count(_get(usage, "prompt_tokens"))
    if prompt is not None:
        cached = _count(_get(_get(usage, "prompt_tokens_details"), "cached_tokens")) or read
    else:
        inputs = _count(_get(usage, "input_tokens"))
        if inputs is None:
            return None
        details = _get(usage, "input_tokens_details")
        if details is not None:
            # The Responses API counts cached tokens inside `input_tokens`, as chat completions do.
            prompt, cached = inputs, _count(_get(details, "cached_tokens")) or 0
        else:
            prompt, cached = inputs + read + written, read
    # A gateway can report a cache that does not fit its own prompt count; the prompt is at least that.
    prompt = max(prompt, cached + written)
    return {"prompt_tokens": prompt, "cached_tokens": cached, "cache_write_tokens": written}


def provider_of(model: str, usage: Any = None) -> str:
    """Who served the call: the router prefix of `vendor/model` names, else what the name or the usage
    shape says, else `openai` (the client `wrap()` takes)."""
    vendor, sep, _ = model.partition("/")
    if sep and vendor:
        return vendor.strip().lower()
    name = model.strip().lower()
    if name.startswith("claude") or ".anthropic." in f".{name}":
        return "anthropic"
    if name.startswith("gemini"):
        return "google"
    if _get(usage, "prompt_tokens") is None and _get(usage, "cache_read_input_tokens") is not None:
        return "anthropic"
    return "openai"


def usage_fields(
    response: Any, *, provider: str | None = None, model: str | None = None
) -> dict[str, Any] | None:
    """What `ModelUsage` needs from a provider's response (or its bare `usage`, with `model=`)."""
    usage = _get(response, "usage")
    if usage is None or isinstance(usage, (int, float, str)):
        usage = response
    counts = token_counts(usage)
    name = model or _get(response, "model")
    if counts is None or not isinstance(name, str) or not name:
        return None
    return {"provider": (provider or provider_of(name, usage)).lower(), "model": name, **counts}
