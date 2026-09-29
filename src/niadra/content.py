"""The company's content resolver: in a space whose content Niadra keeps by pointer, the text itself stays in
the company's storage, and the SDK puts it back in the agent's process.

```python
niadra.content.register(lambda pointer: our_bucket.read_text(pointer))
context = conversation.context(include=["state"])  # content fields come back with their text
```

A state read marks a content field whose text Niadra does not hold with `content: {mode: "pointer", pointer,
sha256, scan}` and no value. With a resolver registered, each such field whose scan is `clean` gets its text
from the resolver, which is kept only when its SHA-256 is the one Niadra recorded. Content that is `pending`
or `flagged` is never fetched: it does not reach the model. A fetch that fails leaves the field without a
value, and the read goes on. The same resolver reads the values a replay runs with, in `pointer` mode.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from niadra.models.results import Context
from niadra.models.state import FieldState, ObjectRead

logger = logging.getLogger("niadra")

Fetch = Callable[[str], "str | bytes | Awaitable[str | bytes]"]


class ContentResolver:
    """`niadra.content`: the function that reads content by pointer inside the company's boundary."""

    def __init__(self) -> None:
        self._fetch: Fetch | None = None

    def register(self, fetch: Fetch) -> None:
        """`fetch(pointer)` returns the text (or its UTF-8 bytes) the pointer names; it may be a coroutine
        function with the async client."""
        self._fetch = fetch

    @property
    def registered(self) -> bool:
        return self._fetch is not None

    def read(self, pointer: str) -> bytes | None:
        """The bytes a pointer names, for a synchronous caller; None without a resolver or on a failure."""
        if self._fetch is None:
            return None
        try:
            found = self._fetch(pointer)
            if inspect.isawaitable(found):
                raise TypeError("an async content resolver needs the async client")
            return _bytes(found)
        except Exception:
            logger.warning("niadra: the content resolver failed", exc_info=True)
            return None

    async def aread(self, pointer: str) -> bytes | None:
        if self._fetch is None:
            return None
        try:
            found = self._fetch(pointer)
            if inspect.isawaitable(found):
                found = await found
            return _bytes(found)
        except Exception:
            logger.warning("niadra: the content resolver failed", exc_info=True)
            return None

    def fill(self, context: Context) -> Context:
        """`context` with the text of its clean pointer fields."""
        wanted = _wanted(context)
        if not wanted:
            return context
        return _filled(context, {pointer: self.read(pointer) for pointer in wanted})

    async def afill(self, context: Context) -> Context:
        wanted = _wanted(context)
        if not wanted:
            return context
        return _filled(context, {pointer: await self.aread(pointer) for pointer in wanted})


def _bytes(found: Any) -> bytes:
    return found.encode() if isinstance(found, str) else bytes(found)


def _pointers(field: FieldState) -> str | None:
    marker = field.content
    if marker is None or marker.mode != "pointer" or marker.scan != "clean" or not marker.pointer:
        return None
    return marker.pointer if field.v is None else None


def _wanted(context: Context) -> list[str]:
    if context.state is None:
        return []
    return sorted(
        {p for item in context.state.objects for f in item.fields.values() if (p := _pointers(f)) is not None}
    )


def _filled(context: Context, texts: dict[str, bytes | None]) -> Context:
    assert context.state is not None
    objects: list[ObjectRead] = []
    for item in context.state.objects:
        fields: dict[str, FieldState] = {}
        for name, field in item.fields.items():
            pointer = _pointers(field)
            data = texts.get(pointer) if pointer is not None else None
            if data is not None and field.content is not None and _matches(data, field.content.sha256):
                field = field.model_copy(update={"v": data.decode("utf-8", errors="replace")})
            fields[name] = field
        objects.append(item.model_copy(update={"fields": fields}))
    return context.model_copy(update={"state": context.state.model_copy(update={"objects": objects})})


def _matches(data: bytes, sha256: str | None) -> bool:
    """Text is kept only when it is the text Niadra recorded; without a recorded digest, as read."""
    if sha256 is None:
        return True
    ok = hashlib.sha256(data).hexdigest() == sha256
    if not ok:
        logger.warning("niadra: content read by pointer does not match its recorded digest; left out")
    return ok
