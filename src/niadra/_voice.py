"""The voice read path: a spoken turn never waits on a round trip for what can be known in advance.

A read in the `voice` view with a conversation (or task) id, and the cache on, goes through the
conversation's `VoiceLine`:

- The pinned pack is the same bytes for the whole conversation (the server guarantees it), so once
  a read brought it, every later turn gets it from memory at once, whatever its age, and the SDK
  revalidates it by ETag in the background (`ContextCache.get(..., pinned=True)`).
- `begin()` starts the first read when the call starts (ringing, the inbound webhook, the caller
  joining the room), so it runs while the call is set up, and `ready()` waits for it there, within
  `Timeouts.context_voice_start`. A turn that finds it still running waits for it only within its
  own budget.
- While the customer speaks, `prefetch()` warms the server as before and, once the partial
  transcript has stayed the same for `VoiceOptions.settle`, reads the turn with it (one such read
  in flight per conversation; the newest settled text waits behind it). The slots and the delta of
  that read answer the final turn when the final words start with the partial's and the partial
  carries at least `VoiceOptions.min_coverage` of them. Otherwise the turn reads its own words.
- A turn waits for the read of its words at most its budget (`Timeouts.context_voice`). Past it,
  the turn gets the pinned body without slots, and the read goes on in the background: its answer
  revalidates the body and leaves its delta for the next turn.
- The first voice read of a client measures the round trip to the region once (`GET /healthz`,
  twice, the faster) and logs a warning when the budgets cannot hold it.

What a line keeps is what the server sent for this conversation, in process memory only; ending the
conversation drops the line and the conversation's packs, and an answer still on its way is then
not stored. Nothing here does I/O: the clients run the reads and the waits.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from niadra.models.context import ContextRequest
from niadra.models.results import Context
from niadra.options import Timeouts, VoiceOptions

# Reads a line remembers for matching turns; older ones are forgotten.
MAX_READS = 4
# Conversations with a line at once; past this, the least recently used line goes.
MAX_LINES = 1000

_WORD = re.compile(r"\w+")

Words = tuple[str, ...]


def words_of(text: str | None) -> Words:
    """The words of a transcript, compared the way speech-to-text revises them: case, accents and
    punctuation left out."""
    if not text:
        return ()
    folded = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return tuple(_WORD.findall(plain))


def covers(partial: Words, final: Words, min_coverage: float) -> bool:
    """Whether a read of `partial` answers the turn `final`: the final words start with the partial's,
    and the partial carries at least `min_coverage` of them. A read without words answers only a
    read without words."""
    if not partial or not final:
        return partial == final
    return final[: len(partial)] == partial and len(partial) >= min_coverage * len(final)


@dataclass(eq=False)
class TurnRead:
    """One read of a line: its words (none for the first read), and its answer once it lands."""

    words: Words
    started: float = field(default_factory=time.monotonic)
    speculative: bool = False
    handle: Any = None
    """The `Future` (sync client) or `Task` (async client) that runs it."""
    result: Context | None = None
    failed: BaseException | None = None
    done: bool = False

    @property
    def ok(self) -> bool:
        return self.done and self.result is not None


@dataclass(eq=False)
class VoiceLine:
    """What the SDK keeps for one voice conversation between its turns."""

    scope: str
    request: ContextRequest | None = None
    """The conversation's read without the turn: what a speculative read sends with the partial."""
    reads: list[TurnRead] = field(default_factory=list)
    heard: str | None = None
    """The newest partial transcript not read yet, and when it last changed."""
    heard_at: float = 0.0
    settling: bool = False
    speculating: TurnRead | None = None
    waiting: str | None = None
    """The newest settled text, waiting for the speculative read in flight."""
    timer: Any = None
    closed: bool = False

    def add(self, read: TurnRead) -> None:
        self.reads.append(read)
        del self.reads[:-MAX_READS]

    def in_flight(self) -> list[TurnRead]:
        return [read for read in self.reads if not read.done]

    def covering(self, words: Words, min_coverage: float) -> list[TurnRead]:
        """The reads that answer a turn of `words`, newest first, failed ones left out."""
        return [
            read
            for read in reversed(self.reads)
            if covers(read.words, words, min_coverage) and not (read.done and read.result is None)
        ]

    def already_read(self, words: Words) -> bool:
        return any(read.words == words and not (read.done and read.result is None) for read in self.reads)


class VoiceLines:
    """The lines of one client, by scope, least recently used first out, and its round trip."""

    def __init__(self, options: VoiceOptions) -> None:
        self.options = options
        self.lock = threading.RLock()
        self._lines: OrderedDict[str, VoiceLine] = OrderedDict()
        self.rtt: float | None = None
        self.probed = False

    def line(self, scope: str) -> VoiceLine:
        with self.lock:
            line = self._lines.get(scope)
            if line is None:
                line = self._lines[scope] = VoiceLine(scope)
                while len(self._lines) > MAX_LINES:
                    _, gone = self._lines.popitem(last=False)
                    gone.closed = True
            self._lines.move_to_end(scope)
            return line

    def find(self, scope: str) -> VoiceLine | None:
        with self.lock:
            return self._lines.get(scope)

    def drop(self, scope: str) -> VoiceLine | None:
        """Forgets a line: its reads' answers are no longer used, and its timer is returned to cancel."""
        with self.lock:
            line = self._lines.pop(scope, None)
            if line is not None:
                line.closed = True
                line.reads = []
                line.heard = line.waiting = None
            return line

    def clear(self) -> list[VoiceLine]:
        """Forgets every line (the client is closing) and returns them, for their timers."""
        with self.lock:
            lines = list(self._lines.values())
            self._lines.clear()
        for line in lines:
            line.closed = True
        return lines

    def claim_probe(self) -> bool:
        """True once per client: the caller measures the round trip."""
        with self.lock:
            if self.probed or not self.options.probe:
                return False
            self.probed = True
            return True

    def __len__(self) -> int:
        with self.lock:
            return len(self._lines)


def compose(body: Context, read: TurnRead | None) -> Context:
    """The pinned body from the cache with the slots and guards of the read that answers the turn."""
    fetched = read.result if read is not None else None
    if fetched is None:
        pack = body.pack.model_copy(update={"slots": []}) if body.pack is not None else None
        return body.model_copy(update={"slots": None, "guards": [], "pack": pack})
    pack = body.pack
    if pack is not None:
        pack = pack.model_copy(update={"slots": fetched.pack.slots if fetched.pack is not None else []})
    return body.model_copy(update={"slots": fetched.slots, "guards": fetched.guards, "pack": pack})


RTT_MARGIN = 0.05
"""What a read needs on top of the round trip at the least: the API's own time (20 to 40 ms at its p95)."""


def budget_warnings(rtt: float, timeouts: Timeouts, explicit: set[str]) -> list[str]:
    """The read budgets the caller set below the round trip plus `RTT_MARGIN`: every such read would run out
    of time. Log-safe: numbers only."""
    ms = round(rtt * 1000)
    found = []
    for name in sorted(explicit):
        budget = getattr(timeouts, name)
        if budget < rtt + RTT_MARGIN:
            found.append(
                f"Timeouts.{name} ({round(budget * 1000)} ms) is shorter than the round trip to the region "
                f"({ms} ms) plus {round(RTT_MARGIN * 1000)} ms for the API: its reads will run out of time. "
                f"Leave it at its default, which adds the measured round trip, or raise it"
            )
    return found


def rtt_warnings(rtt: float, timeouts: Timeouts) -> list[str]:
    """What to warn about once the round trip to the region is known. Log-safe: numbers only."""
    ms = round(rtt * 1000)
    found = []
    if timeouts.context_voice <= rtt:
        found.append(
            f"the round trip to the region ({ms} ms) is longer than Timeouts.context_voice "
            f"({round(timeouts.context_voice * 1000)} ms): a voice turn whose words were not prefetched "
            "gets the pinned pack without slots. Send partial transcripts with prefetch() (the voice "
            "adapters do) or raise the budget"
        )
    if timeouts.context_voice_start <= 3 * rtt:
        found.append(
            f"the round trip to the region ({ms} ms) leaves Timeouts.context_voice_start "
            f"({round(timeouts.context_voice_start * 1000)} ms) short of a cold connection (three round "
            "trips): the first read of a call may miss it. Raise the budget"
        )
    return found
