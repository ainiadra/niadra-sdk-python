"""The voice read path against a region 150 to 400 ms away (niadra._voice).

`Region` is a fake API behind `httpx.MockTransport` that answers every request after a random
delay in that range, the round trip from a caller far from the cell plus the server's time. The
tests speak like a voice platform does: partial transcripts while the customer talks, then the
platform's end-of-turn delay, then the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from niadra import AsyncNiadra, Niadra, Timeouts, VoiceOptions, phone
from niadra._voice import covers, words_of
from niadra.options import QueueOptions
from tests.conftest import KEY, context_payload

MARINA = phone("+5511912345678")
BODY = '<context source="niadra">Marina, prefers WhatsApp; March: the same reason came up before</context>'
# LiveKit's default minimum endpointing delay is 0.5 s; the turns here wait a little more than the
# slowest read (0.2 s of settle plus 0.4 s) so the read of the last partial has landed.
END_OF_TURN = 0.65
TURNS = [
    "e o roteador novo que voces iam mandar",
    "quero saber da fatura de agosto",
    "o credito de quarenta reais ja entrou",
    "entao pode cancelar o chamado",
    "obrigado era isso mesmo",
]


class Region:
    """A fake region: every answer `latency` seconds after the request left (`requests` notes when), a
    pinned body with a stable ETag, slots that name the words they were read for, and each delta once."""

    def __init__(self, latency: tuple[float, float] = (0.15, 0.40), seed: int = 7) -> None:
        self.latency = latency
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.requests: list[tuple[float, str, dict[str, Any]]] = []
        self.learned: str | None = None
        self.fail_turns = False

    def reads(self) -> list[dict[str, Any]]:
        return [body for _, path, body in self.requests if path == "/v1/context"]

    def delay(self) -> float:
        with self.lock:
            return self.rng.uniform(*self.latency)

    def arrive(self, request: httpx.Request) -> None:
        body = json.loads(request.content) if request.content else {}
        with self.lock:
            self.requests.append((time.monotonic() - self.t0, request.url.path, body))

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/context/prefetch":
            return httpx.Response(202, json={"queued": True})
        if path == "/v1/batch":
            return httpx.Response(
                200, json={"accepted": len(body.get("items", [])), "duplicates": 0, "errors": []}
            )
        if path != "/v1/context":
            return httpx.Response(404, json={"code": "not_found"})
        if self.fail_turns and body.get("query"):
            return httpx.Response(503, json={"code": "unavailable"})
        payload = context_payload(text=BODY, etag="etag-pinned", path="t0")
        if body.get("known_etag") == "etag-pinned":
            payload.update(not_modified=True, text=None, path="not_modified")
        if body.get("query"):
            payload["slots"] = f"[Slots] {body['query']}"
        with self.lock:
            if body.get("delta") and self.learned:
                payload["delta"], self.learned = self.learned, None
        return httpx.Response(200, json=payload)

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        self.arrive(request)
        await asyncio.sleep(self.delay())
        return self.answer(request)

    def handle_sync(self, request: httpx.Request) -> httpx.Response:
        self.arrive(request)
        time.sleep(self.delay())
        return self.answer(request)


QUIET = QueueOptions(batch_size=10_000, interval=3600, turn_interval=3600)


@pytest.fixture
def region() -> Region:
    return Region()


@pytest.fixture
async def voice(region: Region) -> AsyncIterator[AsyncNiadra]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(KEY, channel="voice", queue=QUIET, http_client=http)
    yield niadra
    await niadra.close(timeout=0)
    await http.aclose()


async def speak(call: Any, text: str, gap: float = 0.08) -> str:
    """Partial transcripts, one word more every `gap` seconds, as a streaming STT sends them."""
    said = ""
    for word in text.split():
        said = f"{said} {word}".strip()
        call.prefetch(said)
        await asyncio.sleep(gap)
    return said


def report(label: str, samples: list[float]) -> str:
    ms = sorted(s * 1000 for s in samples)
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    line = f"{label}: n={len(ms)} p50={statistics.median(ms):.1f} ms p95={p95:.1f} ms max={ms[-1]:.1f} ms"
    print(line)  # the numbers the change is measured by; `pytest -s` shows them
    return line


def test_words_are_compared_the_way_speech_to_text_revises_them() -> None:
    assert words_of("Quero saber da FATURA, de agosto?") == ("quero", "saber", "da", "fatura", "de", "agosto")
    assert words_of("é o roteador") == words_of("e o Roteador")
    final = words_of("quero saber da fatura de agosto")
    assert covers(words_of("quero saber da fatura de agosto"), final, 0.75)
    assert covers(words_of("quero saber da fatura de"), final, 0.75), "five of six words"
    assert not covers(words_of("quero saber da fatura"), final, 0.75), "four of six is not enough"
    assert not covers(words_of("quero saber do boleto de agosto"), final, 0.75), "not the same words"
    assert covers((), (), 0.75) and not covers((), final, 0.75)


async def test_turns_after_the_first_return_in_a_few_ms_with_the_right_body(
    region: Region, voice: AsyncNiadra
) -> None:
    turn_times: list[float] = []
    async with voice.conversation("call-1", subject=MARINA, channel="voice", view="voice") as call:
        call.begin()  # the call starts: ringing, the inbound webhook, the caller joining
        await asyncio.sleep(0.5)  # call setup and the greeting
        opened = time.monotonic()
        first = await call.ready()
        ready_time = time.monotonic() - opened
        assert first.text == BODY and first.slots is None
        for turn in TURNS:
            said = await speak(call, turn)
            await asyncio.sleep(END_OF_TURN)
            call.customer(said)
            started = time.monotonic()
            context = await call.context()
            turn_times.append(time.monotonic() - started)
            assert context.text == BODY, "the pinned body, byte for byte"
            assert context.slots == f"[Slots] {turn}", "the slots of this turn's words"
            call.mark_injected(context)
            call.agent("Certo.")
    report("voice turns (150-400 ms region)", turn_times)
    report("first read after call setup", [ready_time])
    assert ready_time < 0.02
    assert max(turn_times) < 0.02, "no turn waited on a round trip"
    turn_reads = [b for b in region.reads() if b.get("query")]
    assert {b["query"] for b in turn_reads} >= set(TURNS), "every turn's words were read while spoken"
    print(f"reads of partial transcripts: {len(turn_reads)} for {len(TURNS)} turns")


async def test_without_the_voice_path_every_turn_pays_the_round_trip(region: Region) -> None:
    """The same call on the path of 0.5.0 (`VoiceOptions(enabled=False)` and its 150 ms budget)."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(
        KEY,
        channel="voice",
        queue=QUIET,
        http_client=http,
        voice=VoiceOptions(enabled=False),
        timeouts=Timeouts(context_voice=0.15),
    )
    turn_times: list[float] = []
    with_slots = 0
    try:
        async with niadra.conversation("call-0", subject=MARINA, channel="voice", view="voice") as call:
            for turn in TURNS:
                said = await speak(call, turn)
                await asyncio.sleep(END_OF_TURN)
                call.customer(said)
                started = time.monotonic()
                context = await call.context()
                turn_times.append(time.monotonic() - started)
                with_slots += context.slots is not None
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    report("0.5.0 path, same turns (150-400 ms region)", turn_times)
    print(f"0.5.0 path: {with_slots} of {len(TURNS)} turns got their slots")
    assert min(turn_times) > 0.1, "each turn waited on the network"


async def test_the_first_read_overlaps_the_call_setup(region: Region, voice: AsyncNiadra) -> None:
    region.latency = (0.35, 0.35)
    async with voice.conversation("call-2", subject=MARINA, channel="voice", view="voice") as call:
        region.t0 = began = time.monotonic()
        call.begin()
        await asyncio.sleep(0.4)  # the platform answers the call meanwhile
        started = time.monotonic()
        context = await call.ready()
        waited = time.monotonic() - started
    first_read = next(at for at, path, _ in region.requests if path == "/v1/context")
    report("first read sent after begin()", [first_read])
    report("ready() after 0.4 s of setup, 0.35 s region", [waited])
    assert first_read < 0.02, "the read left when the call started, not at the first turn"
    assert context.text == BODY and waited < 0.02
    assert time.monotonic() - began > 0.35


async def test_ready_waits_for_a_first_read_still_running(region: Region, voice: AsyncNiadra) -> None:
    region.latency = (0.3, 0.3)
    async with voice.conversation("call-3", subject=MARINA, channel="voice", view="voice") as call:
        started = time.monotonic()
        context = await call.ready()
        waited = time.monotonic() - started
    assert context.text == BODY
    assert 0.28 < waited < 0.6, "within context_voice_start, not the 0.2 s turn budget"


async def test_a_turn_never_waits_past_its_budget_and_the_next_turn_catches_up(
    region: Region, voice: AsyncNiadra
) -> None:
    region.latency = (0.4, 0.4)
    async with voice.conversation("call-4", subject=MARINA, channel="voice", view="voice") as call:
        await call.ready()
        region.learned = "Envio de roteador novo (open item)"
        call.customer("e o roteador novo que voces iam mandar")  # never prefetched
        started = time.monotonic()
        late = await call.context()
        waited = time.monotonic() - started
        assert late.text == BODY and late.slots is None and late.delta is None
        assert waited < 0.2 + 0.05, "the budget, not the round trip"
        await asyncio.sleep(0.3)  # the read goes on and lands
        call.customer("e agora")
        caught = await call.context()
    report("turn whose read had not landed (0.4 s region)", [waited])
    assert caught.text == BODY
    assert caught.delta == "Envio de roteador novo (open item)", "the next turn catches up"


async def test_a_delta_learned_by_a_prefetched_read_is_delivered_once(
    region: Region, voice: AsyncNiadra
) -> None:
    async with voice.conversation("call-5", subject=MARINA, channel="voice", view="voice") as call:
        await call.ready()
        call.customer("oi")
        await call.context()  # the session now asks for deltas
        region.learned = "Credito de R$ 40 na fatura de agosto"
        said = await speak(call, "o credito de quarenta reais ja entrou")
        await asyncio.sleep(END_OF_TURN)
        call.customer(said)
        first = await call.context()
        call.customer("e depois")
        await asyncio.sleep(END_OF_TURN)
        second = await call.context()
    assert first.delta == "Credito de R$ 40 na fatura de agosto"
    assert second.delta == first.delta, "the session keeps it; the server sent it once"
    assert sum(1 for b in region.reads() if b.get("delta")) >= 2


async def test_voice_turns_fail_open(region: Region, voice: AsyncNiadra) -> None:
    async with voice.conversation("call-6", subject=MARINA, channel="voice", view="voice") as call:
        await call.ready()
        region.fail_turns = True
        said = await speak(call, "quero saber da fatura de agosto")
        await asyncio.sleep(END_OF_TURN)
        call.customer(said)
        context = await call.context()
    assert context.text == BODY and context.slots is None, "the body still, without slots"


async def test_a_region_that_does_not_answer_gives_an_empty_context(
    voice: AsyncNiadra, region: Region
) -> None:
    region.latency = (5.0, 5.0)
    async with voice.conversation("call-7", subject=MARINA, channel="voice", view="voice") as call:
        call.customer("alo")
        started = time.monotonic()
        context = await call.context()
        waited = time.monotonic() - started
    assert context.text is None and context.origin == "empty" and context.error
    assert waited < 0.25


async def test_ending_the_call_drops_what_was_kept(region: Region, voice: AsyncNiadra) -> None:
    async with voice.conversation("call-8", subject=MARINA, channel="voice", view="voice") as call:
        await call.ready()
        call.prefetch("quero saber da fatura de agosto")
        await asyncio.sleep(0.25)  # a read of the partial is on its way
    assert voice._voice.find("c:call-8") is None
    assert len(voice._cache) == 0
    await asyncio.sleep(0.5)  # the read lands after the end
    assert len(voice._cache) == 0, "an answer that arrives after the end is not kept"


async def test_the_round_trip_is_measured_once_and_a_short_budget_is_reported(
    region: Region, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="niadra")
    region.latency = (0.3, 0.3)
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(KEY, channel="voice", queue=QUIET, http_client=http)
    try:
        async with niadra.conversation("call-9", subject=MARINA, channel="voice", view="voice") as call:
            await call.ready()
            await asyncio.sleep(0.7)
            async with niadra.conversation("call-10", subject=MARINA, channel="voice", view="voice") as other:
                await other.ready()
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert niadra.rtt == pytest.approx(0.3, abs=0.05)
    assert sum(1 for _, path, _ in region.requests if path == "/healthz") == 2, "once per client"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("longer than Timeouts.context_voice (200 ms)" in w for w in warnings)
    assert all("+55" not in r.getMessage() for r in caplog.records), "numbers only, never the customer"


async def test_the_voice_path_can_be_turned_off(region: Region) -> None:
    region.latency = (0.05, 0.05)
    http = httpx.AsyncClient(transport=httpx.MockTransport(region.handle_async))
    niadra = AsyncNiadra(
        KEY, channel="voice", queue=QUIET, http_client=http, voice=VoiceOptions(enabled=False)
    )
    try:
        async with niadra.conversation("call-11", subject=MARINA, channel="voice", view="voice") as call:
            assert call.begin() is False
            call.customer("quero saber da fatura")
            context = await call.context()
    finally:
        await niadra.close(timeout=0)
        await http.aclose()
    assert context.slots == "[Slots] quero saber da fatura"
    assert not any(path == "/healthz" for _, path, _ in region.requests)


@pytest.fixture
def sync_voice(region: Region) -> Iterator[Niadra]:
    http = httpx.Client(transport=httpx.MockTransport(region.handle_sync))
    niadra = Niadra(KEY, channel="voice", queue=QUIET, http_client=http, timeouts=Timeouts())
    yield niadra
    niadra.close(timeout=0)
    http.close()


def test_the_sync_client_serves_voice_turns_the_same_way(region: Region, sync_voice: Niadra) -> None:
    turn_times: list[float] = []
    with sync_voice.conversation("call-12", subject=MARINA, channel="voice", view="voice") as call:
        call.begin()
        time.sleep(0.5)
        assert call.ready().text == BODY
        for turn in TURNS[:3]:
            said = ""
            for word in turn.split():
                said = f"{said} {word}".strip()
                call.prefetch(said)
                time.sleep(0.08)
            time.sleep(END_OF_TURN)
            call.customer(said)
            started = time.monotonic()
            context = call.context()
            turn_times.append(time.monotonic() - started)
            assert context.text == BODY and context.slots == f"[Slots] {turn}"
    report("sync voice turns (150-400 ms region)", turn_times)
    assert max(turn_times) < 0.02
    assert sync_voice._voice.find("c:call-12") is None and len(sync_voice._cache) == 0


async def test_a_call_start_webhook_waits_for_the_first_read_within_the_start_budget(
    region: Region, voice: AsyncNiadra
) -> None:
    from tests.integrations.test_retell import hooks, payload, signed

    region.latency = (0.35, 0.35)  # longer than the 200 ms turn budget
    body = payload("call_inbound")
    started = time.monotonic()
    answer = await hooks(voice).inbound(body, signed(body))
    waited = time.monotonic() - started
    report("Retell inbound webhook, 0.35 s region", [waited])
    assert BODY in answer.body["call_inbound"]["dynamic_variables"]["niadra_context"]
    assert waited < 1.5


async def test_twilios_incoming_call_starts_the_first_read(region: Region, voice: AsyncNiadra) -> None:
    from niadra.integrations.twilio import TwilioCall

    ringing = TwilioCall(call_sid="CA-voice", from_number="+5511912345678", status="ringing")
    conversation = ringing.conversation(voice)
    line = voice._voice.find("c:CA-voice")
    assert line is not None and line.in_flight(), "the read left while the call rings"
    await asyncio.sleep(0.45)
    started = time.monotonic()
    context = await conversation.ready()
    assert context.text == BODY and time.monotonic() - started < 0.02
    TwilioCall(call_sid="CA-done", from_number="+5511912345678", status="completed").conversation(voice)
    assert voice._voice.find("c:CA-done") is None, "a status callback starts nothing"


async def test_an_attested_call_starts_its_first_read_at_the_level_it_proved(
    region: Region, voice: AsyncNiadra
) -> None:
    from niadra.integrations.twilio import TwilioCall

    region.latency = (0.05, 0.05)
    ringing = TwilioCall(
        call_sid="CA-attested",
        from_number="+5511912345678",
        status="ringing",
        attestation="TN-Validation-Passed-A",
    )
    conversation = ringing.conversation(voice)
    assert voice._voice.find("c:CA-attested") is None, "nothing read before the level is known"
    await ringing.verify(conversation)
    line = voice._voice.find("c:CA-attested")
    assert line is not None and line.request is not None and line.in_flight()
    assert line.request.verification.value == "V2"
    assert (await conversation.ready()).text == BODY
