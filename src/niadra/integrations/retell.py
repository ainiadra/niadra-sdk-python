"""Retell AI: the customer's memory in a Retell voice agent, from your server.

Retell reaches your server three ways, and `RetellWebhooks` answers each. Every request carries
`x-retell-signature` (`v=<unix ms>,d=<hex HMAC-SHA256 of body + ms>`, keyed by your Retell API
key); without a valid one, within five minutes, the answer is 401.

1. **Inbound call webhook** (`call_inbound`, set on the phone number): `inbound()` verifies the
   carrier's attestation when you pass one, reads `context(view="voice")` for the caller and
   answers the dynamic variable `niadra_context`. Put `{{niadra_context}}` in the agent's prompt,
   after your instructions. With `agent_memory=True`, the agent's own notes come as
   `niadra_agent_memory`: put `{{niadra_agent_memory}}` right before `{{niadra_context}}`. For an
   outbound call, `outbound()` gives the same variables and a `metadata` to pass to
   `create_phone_call`.
2. **Custom functions**: `tool_configs(url)` gives the history tools as Retell custom tools, with
   the kit's names, descriptions and JSON Schemas, word for word. Retell posts each call with the
   call it belongs to, and `custom_function()` runs the tool for that call's customer: the
   customer comes from the call, never from the model's arguments.
3. **Agent webhook** (`call_started`, `call_ended`, `call_analyzed`...): `webhook()` records, on
   `call_ended`, each utterance of `transcript_object` as a turn at its moment in the call, a
   transferred call as a handoff to a person, and the end of the conversation. Idempotency keys
   come from the call, so a redelivered event records nothing twice. The other events answer 200.

```python
from fastapi import FastAPI, Request, Response
from niadra import AsyncNiadra
from niadra.integrations.retell import RetellWebhooks, tool_configs

niadra = AsyncNiadra(channel="voice")
retell = RetellWebhooks(niadra, api_key=RETELL_API_KEY)
TOOLS = tool_configs("https://agent.example.com/retell/tools")  # the Retell LLM's general_tools
app = FastAPI()


@app.post("/retell/inbound")
async def inbound(request: Request) -> Response:
    answer = await retell.inbound(await request.body(), request.headers)
    return Response(answer.text(), answer.status, media_type=answer.content_type)


@app.post("/retell/tools")
async def tools(request: Request) -> Response:
    answer = await retell.custom_function(await request.body(), request.headers)
    return Response(answer.text(), answer.status, media_type=answer.content_type)


@app.post("/retell/events")
async def events(request: Request) -> Response:
    answer = await retell.webhook(await request.body(), request.headers)
    return Response(answer.text(), answer.status, media_type=answer.content_type)
```

The Niadra conversation id is Retell's `call_id`, or `metadata.niadra_conversation_id` when the
call carries one (what `outbound()` sets). The subject is the customer's number: the caller on an
inbound call, the called number on an outbound one; pass `subject=` (a function of the call) for
customers identified otherwise. Niadra slow or down never fails a call: the inbound webhook
answers empty variables, a tool answers that the history is unavailable, and the webhook still
answers 200.

With a custom LLM (Retell's LLM WebSocket), your server makes the model call: open the
conversation with the call id and use `wrap()` or the adapter of the framework you call.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from niadra._async_client import AsyncNiadra
from niadra._client import Niadra
from niadra.integrations._common import (
    AgentMemoryLike,
    AgentMemoryOption,
    blocks,
    call_tool,
    end,
    handoff,
    join_instructions,
    maybe_await,
    memory_kit_of,
    memory_option,
    phone_or_none,
    read_agent_memory,
    read_context,
    record,
    run_sync,
    tool_specs,
    verify_attestation,
    warn,
)
from niadra.integrations._webhooks import (
    BAD_REQUEST,
    OK,
    UNAUTHORIZED,
    Body,
    Session,
    WebhookResponse,
    header,
    mapping,
    parse,
    raw,
    restore_stamp,
    text,
)
from niadra.models.common import Handle
from niadra.tools import definitions
from niadra.vocabulary import Verification

__all__ = ["RetellWebhooks", "tool_configs", "verify_signature"]

SIGNATURE_HEADER = "x-retell-signature"
TOLERANCE_MS = 5 * 60 * 1000

CONTEXT_VARIABLE = "niadra_context"
MEMORY_VARIABLE = "niadra_agent_memory"
ETAG_VARIABLE = "niadra_context_etag"
INJECTED_VARIABLE = "niadra_injected_at"
CONVERSATION_METADATA = "niadra_conversation_id"

_SIGNATURE = re.compile(r"v=(\d+),d=([0-9a-f]{64})")


def verify_signature(
    body: Body,
    signature: str | None,
    api_key: str | None,
    *,
    now_ms: int | None = None,
    tolerance_ms: int = TOLERANCE_MS,
) -> bool:
    """Checks an `x-retell-signature` header: `v=<unix ms>,d=<hex HMAC-SHA256 of body + ms>`."""
    if not signature or not api_key:
        return False
    match = _SIGNATURE.fullmatch(signature.strip())
    if match is None:
        return False
    stamp, given = match.group(1), match.group(2)
    moment = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(moment - int(stamp)) > tolerance_ms:
        return False
    expected = hmac.new(api_key.encode(), raw(body) + stamp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


def _definitions(option: AgentMemoryOption | None) -> list[dict[str, Any]]:
    return definitions(agent_memory=option is not None, write_agent_memory=bool(option and option.write))


def tool_configs(
    url: str,
    *,
    agent_memory: AgentMemoryLike = None,
    timeout_ms: int = 10_000,
    speak_during_execution: bool = False,
) -> list[dict[str, Any]]:
    """The history tools as Retell custom tools (`general_tools` of a Retell LLM) that post to `url`.

    Name, description and parameters are the kit's, word for word. Retell signs each request and
    sends the call with it, which is how `custom_function()` knows the customer.
    """
    return [
        {
            "type": "custom",
            "name": spec.name,
            "description": spec.description,
            "url": url,
            "method": "POST",
            "parameters": copy.deepcopy(spec.parameters),
            "speak_during_execution": speak_during_execution,
            "speak_after_execution": True,
            "timeout_ms": timeout_ms,
        }
        for spec in tool_specs(_definitions(memory_option(agent_memory)))
    ]


def _customer_number(call: Mapping[str, Any]) -> str | None:
    """The customer's side of the call: who called in, or who was called."""
    if call.get("direction") == "outbound":
        return text(call.get("to_number"))
    return text(call.get("from_number"))


class RetellWebhooks:
    """Answers Retell's webhooks and custom functions with Niadra. See the module documentation.

    `niadra` is a `Niadra` or an `AsyncNiadra`: await the methods with the async client, and call
    the `*_sync` twins with the sync one. `api_key` is the Retell API key that signs the requests
    (one with the webhook badge). `attestation`, when given, returns the carrier's STIR/SHAKEN
    level (`A`, `B` or `C`) from the inbound payload, for instance from `custom_sip_headers`; it
    is verified before the first context. `clock` returns the current Unix time in seconds.
    """

    def __init__(
        self,
        niadra: Niadra | AsyncNiadra,
        *,
        api_key: str | None,
        subject: Callable[[Mapping[str, Any]], Handle | None] | None = None,
        attestation: Callable[[Mapping[str, Any]], str | None] | None = None,
        override_agent_id: str | None = None,
        channel: str = "voice",
        view: str = "voice",
        agent_id: str | None = None,
        tool_verification: Verification | str = Verification.V2,
        agent_memory: AgentMemoryLike = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.niadra = niadra
        self.api_key = api_key
        self.agent_memory = memory_option(agent_memory)
        self._names = frozenset(spec.name for spec in tool_specs(_definitions(self.agent_memory)))
        self.subject = subject or (lambda call: phone_or_none(_customer_number(call)))
        self.attestation = attestation
        self.override_agent_id = override_agent_id
        self.channel = channel
        self.view = view
        self.agent_id = agent_id
        # A tool asks for this level; the API serves the lower one the conversation proved.
        self.tool_verification = Verification(tool_verification)
        self.clock = clock or time.time

    def _signed(self, body: Body, headers: Mapping[str, str] | None) -> bool:
        signature = header(headers, SIGNATURE_HEADER)
        return verify_signature(body, signature, self.api_key, now_ms=int(self.clock() * 1000))

    def _session(
        self, call: Mapping[str, Any], verification: Verification | str = Verification.V0
    ) -> Session | None:
        conversation_id = text(mapping(call.get("metadata")).get(CONVERSATION_METADATA)) or text(
            call.get("call_id")
        )
        if conversation_id is None:
            return None
        return self.niadra.conversation(
            conversation_id,
            subject=self._subject(call),
            channel=self.channel,
            view=self.view,
            verification=verification,
            agent_id=self.agent_id,
        )

    async def _variables(self, session: Session | None, call: Mapping[str, Any]) -> dict[str, str]:
        variables = {CONTEXT_VARIABLE: "", MEMORY_VARIABLE: "", ETAG_VARIABLE: "", INJECTED_VARIABLE: ""}
        if session is None:
            return variables
        if self.attestation is not None:
            try:
                level = self.attestation(call)
            except Exception as exc:
                warn("read the attestation", exc)
                level = None
            await verify_attestation(session, level)
        variables[MEMORY_VARIABLE] = await read_agent_memory(session, self.agent_memory)
        context = await read_context(session)
        if context is not None:
            variables[CONTEXT_VARIABLE] = join_instructions(*blocks(context))
            variables[ETAG_VARIABLE] = context.etag or ""
            variables[INJECTED_VARIABLE] = datetime.now(timezone.utc).isoformat()
        return variables

    async def inbound(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """Answers the inbound call webhook with the context as dynamic variables."""
        if not self._signed(body, headers):
            return UNAUTHORIZED
        payload = parse(body)
        call = mapping((payload or {}).get("call_inbound"))
        if payload is None or payload.get("event", "call_inbound") != "call_inbound" or not call:
            return BAD_REQUEST
        answer: dict[str, Any] = {"dynamic_variables": await self._variables(self._session(call), call)}
        if self.override_agent_id is not None:
            answer["override_agent_id"] = self.override_agent_id
        return WebhookResponse(200, {"call_inbound": answer})

    async def outbound(self, to_number: str, *, conversation_id: str | None = None) -> dict[str, Any]:
        """What to pass to `create_phone_call` for an outbound call: the variables and the metadata.

        `conversation_id` names the Niadra conversation (one is minted without it); the call's
        metadata carries it, so the tools and the webhook find the same conversation.
        """
        call = {"direction": "outbound", "to_number": to_number}
        session = self.niadra.conversation(
            conversation_id,
            subject=self._subject(call),
            channel=self.channel,
            view=self.view,
            agent_id=self.agent_id,
        )
        return {
            "retell_llm_dynamic_variables": await self._variables(session, call),
            "metadata": {CONVERSATION_METADATA: session.id},
        }

    def _subject(self, call: Mapping[str, Any]) -> Handle | None:
        try:
            return self.subject(call)
        except Exception as exc:
            warn("resolve the caller", exc)
            return None

    async def custom_function(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """Runs one history tool for the customer of the call the request names."""
        if not self._signed(body, headers):
            return UNAUTHORIZED
        payload = parse(body)
        if payload is None:
            return BAD_REQUEST
        name = str(payload.get("name") or "")
        if name not in self._names:
            return WebhookResponse(404, {"error": f"unknown tool {name}"})
        session = self._session(mapping(payload.get("call")), self.tool_verification)
        kit = memory_kit_of(session, self.agent_memory)
        if kit is None:
            return WebhookResponse(200, {"error": "no customer on this call; answer without the history"})
        arguments = payload.get("args")
        result = await call_tool(kit, name, arguments if isinstance(arguments, (Mapping, str)) else {})
        return WebhookResponse(200, json.loads(result))

    async def webhook(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """Records a `call_ended` event: every utterance, the transfer, and the end of the call."""
        if not self._signed(body, headers):
            return UNAUTHORIZED
        payload = parse(body)
        if payload is None or not isinstance(payload.get("call"), Mapping):
            return BAD_REQUEST
        if payload.get("event") == "call_ended":
            try:
                await self._record(payload["call"])
            except Exception as exc:
                warn("record the call", exc)
        return OK

    async def _record(self, call: Mapping[str, Any]) -> None:
        session = self._session(call)
        if session is None:
            return
        variables = mapping(call.get("retell_llm_dynamic_variables"))
        restore_stamp(session, variables.get(ETAG_VARIABLE), variables.get(INJECTED_VARIABLE))
        started = call.get("start_timestamp")
        for index, item in enumerate(call.get("transcript_object") or []):
            if isinstance(item, Mapping):
                self._turn(session, index, item, started)
        reason = str(call.get("disconnection_reason") or "")
        if reason == "call_transfer" or call.get("transfer_destination"):
            handoff(session, "human", reason=reason or "transfer")
        end(session)
        await maybe_await(self.niadra.flush(5.0))

    def _turn(self, session: Session, index: int, item: Mapping[str, Any], started: Any) -> None:
        said = text(item.get("content"))
        role = item.get("role")
        if said is None or role not in ("user", "agent"):
            return
        extra: dict[str, Any] = {"idempotency_key": f"retell:{session.id}:{index}"}
        occurred = _moment(started, item.get("words"))
        if occurred is not None:
            extra["occurred_at"] = occurred
        if role == "user":
            record(session, "record the customer's turn", lambda s: s.customer(said, **extra))
        else:
            record(session, "record the agent's turn", lambda s: s.agent(said, **extra))

    def _sync(self) -> RetellWebhooks:
        if isinstance(self.niadra, AsyncNiadra):
            raise TypeError("the *_sync methods take a Niadra client; await the others with AsyncNiadra")
        return self

    def inbound_sync(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """`inbound()` for a sync `Niadra` client."""
        return run_sync(self._sync().inbound(body, headers))

    def outbound_sync(self, to_number: str, *, conversation_id: str | None = None) -> dict[str, Any]:
        """`outbound()` for a sync `Niadra` client."""
        return run_sync(self._sync().outbound(to_number, conversation_id=conversation_id))

    def custom_function_sync(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """`custom_function()` for a sync `Niadra` client."""
        return run_sync(self._sync().custom_function(body, headers))

    def webhook_sync(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """`webhook()` for a sync `Niadra` client."""
        return run_sync(self._sync().webhook(body, headers))


def _moment(started: Any, words: Any) -> datetime | None:
    """When an utterance began: the call's start (Unix ms) plus its first word's offset (seconds)."""
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return None
    offset: Any = 0
    if isinstance(words, list) and words and isinstance(words[0], Mapping):
        offset = words[0].get("start") or 0
    if not isinstance(offset, (int, float)) or isinstance(offset, bool):
        offset = 0
    return datetime.fromtimestamp(started / 1000, tz=timezone.utc) + timedelta(seconds=offset)
