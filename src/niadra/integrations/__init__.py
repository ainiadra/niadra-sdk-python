"""Integrations with model clients and agent frameworks. Each one imports nothing it wraps."""

from niadra.integrations.openai import wrap

__all__ = ["wrap"]
