"""Vapi: the customer's memory in a Vapi assistant, from your server URL.

Vapi posts server messages to one URL; `VapiServer.handle()` answers the ones that matter:

- **`assistant-request`**: verifies the carrier's attestation when you pass one, reads
  `context(view="voice")` for the caller and answers the assistant. With `assistant=` (a transient
  assistant), the pack goes into `model.messages` right after its system messages; with
  `assistant_id=`, it goes in `assistantOverrides.variableValues.niadra_context`, for a prompt that
  says `{{niadra_context}}` after its instructions.
- **`tool-calls`**: runs the history tools for the caller of the call. `tool_definitions(url)` gives
  them as Vapi function tools with the kit's definitions, word for word; the caller comes from the
  call, never from the model's arguments.
- **`end-of-call-report`**: records each user and bot message as a turn at its moment, a forwarded
  call as a handoff to a person, and the end of the conversation. Idempotency keys come from the
  call, so a redelivered report records nothing twice.
- **`transfer-destination-request`** and **`handoff-destination-request`**: record the handoff (to a
  person, or to another assistant of a squad) and answer the destination your `destination=`
  function returns.

```python
from fastapi import FastAPI, Request, Response
from niadra import AsyncNiadra
from niadra.integrations.vapi import VapiServer

niadra = AsyncNiadra(channel="voice")
vapi = VapiServer(niadra, secret=VAPI_SERVER_SECRET, assistant_id="asst_...")
app = FastAPI()


@app.post("/vapi")
async def server(request: Request) -> Response:
    answer = await vapi.handle(await request.body(), request.headers)
    return Response(answer.text(), answer.status, media_type=answer.content_type)
```

Requests without the server secret (`x-vapi-secret`) answer 401: they would read customer data.
The Niadra conversation id is Vapi's call id and the subject is the customer's number; pass
`subject=` (a function of the server message) for customers identified otherwise. Niadra slow or
down never fails the call: the assistant starts without the context and a tool answers that the
history is unavailable.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from niadra._async_client import AsyncNiadra
from niadra._client import Niadra
from niadra.integrations._common import (
    blocks,
    call_tool,
    end,
    handoff,
    instruction_count,
    join_instructions,
    maybe_await,
    phone_or_none,
    read_context,
    record,
    run_sync,
    tool_specs,
    verify_attestation,
    warn,
)
from niadra.integrations._webhooks import (
    BAD_REQUEST,
    UNAUTHORIZED,
    Body,
    Session,
    WebhookResponse,
    header,
    mapping,
    parse,
    restore_stamp,
    same,
    text,
)
from niadra.models.common import Handle
from niadra.tools import BUILTIN_DEFINITIONS
from niadra.vocabulary import Verification

__all__ = ["VapiServer", "tool_definitions"]

SECRET_HEADER = "x-vapi-secret"  # noqa: S105 - a header name
CONTEXT_VARIABLE = "niadra_context"
ETAG_VARIABLE = "niadra_context_etag"
INJECTED_VARIABLE = "niadra_injected_at"
_NAMES = frozenset(spec.name for spec in tool_specs(BUILTIN_DEFINITIONS))


def tool_definitions(url: str, *, secret: str | None = None) -> list[dict[str, Any]]:
    """The history tools as Vapi function tools that call your server URL.

    The `function` part is the kit's definition as it is: the same names, descriptions and JSON
    Schemas as every Niadra SDK.
    """
    server: dict[str, Any] = {"url": url}
    if secret is not None:
        server["secret"] = secret
    return [
        {"type": "function", "function": copy.deepcopy(d["function"]), "server": server}
        for d in BUILTIN_DEFINITIONS
    ]


def _customer_number(message: Mapping[str, Any]) -> str | None:
    customer = mapping(message.get("customer")) or mapping(mapping(message.get("call")).get("customer"))
    return text(customer.get("number"))


class VapiServer:
    """Answers Vapi's server messages with Niadra. See the module documentation."""

    def __init__(
        self,
        niadra: Niadra | AsyncNiadra,
        *,
        secret: str | None,
        assistant_id: str | None = None,
        assistant: Mapping[str, Any] | None = None,
        subject: Callable[[Mapping[str, Any]], Handle | None] | None = None,
        attestation: Callable[[Mapping[str, Any]], str | None] | None = None,
        destination: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        channel: str = "voice",
        view: str = "voice",
        agent_id: str | None = None,
        tool_verification: Verification | str = Verification.V2,
    ) -> None:
        self.niadra = niadra
        self.secret = secret
        self.assistant_id = assistant_id
        self.assistant = assistant
        self.subject = subject or (lambda message: phone_or_none(_customer_number(message)))
        self.attestation = attestation
        self.destination = destination
        self.channel = channel
        self.view = view
        self.agent_id = agent_id
        # A tool asks for this level; the API serves the lower one the conversation proved.
        self.tool_verification = Verification(tool_verification)

    async def handle(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """Answers one server message. Unknown message types get an empty 200."""
        if not same(header(headers, SECRET_HEADER), self.secret):
            return UNAUTHORIZED
        payload = parse(body)
        message = mapping((payload or {}).get("message"))
        kind = message.get("type")
        if not kind:
            return BAD_REQUEST
        if kind == "assistant-request":
            return await self._assistant(message)
        if kind == "tool-calls":
            return await self._tools(message)
        if kind == "end-of-call-report":
            await self._report(message)
        elif kind in ("transfer-destination-request", "handoff-destination-request"):
            return self._transfer(message, "human" if kind.startswith("transfer") else "agent")
        return WebhookResponse(200, {})

    def handle_sync(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """`handle()` for a sync `Niadra` client."""
        if isinstance(self.niadra, AsyncNiadra):
            raise TypeError("handle_sync takes a Niadra client; await handle() with AsyncNiadra")
        return run_sync(self.handle(body, headers))

    def _session(self, message: Mapping[str, Any], verification: Verification | str = "V0") -> Session | None:
        call_id = text(mapping(message.get("call")).get("id"))
        if call_id is None:
            return None
        try:
            handle = self.subject(message)
        except Exception as exc:
            warn("resolve the caller", exc)
            handle = None
        return self.niadra.conversation(
            call_id,
            subject=handle,
            channel=self.channel,
            view=self.view,
            verification=verification,
            agent_id=self.agent_id,
        )

    async def _assistant(self, message: Mapping[str, Any]) -> WebhookResponse:
        session = self._session(message)
        pack, variables = "", {CONTEXT_VARIABLE: "", ETAG_VARIABLE: "", INJECTED_VARIABLE: ""}
        if session is not None:
            if self.attestation is not None:
                try:
                    level = self.attestation(message)
                except Exception as exc:
                    warn("read the attestation", exc)
                    level = None
                await verify_attestation(session, level)
            context = await read_context(session)
            if context is not None:
                pack = join_instructions(*blocks(context))
                variables = {
                    CONTEXT_VARIABLE: pack,
                    ETAG_VARIABLE: context.etag,
                    INJECTED_VARIABLE: datetime.now(timezone.utc).isoformat(),
                }
        overrides = {"variableValues": variables}
        if self.assistant is not None:
            assistant = copy.deepcopy(dict(self.assistant))
            if pack:
                model = assistant.setdefault("model", {})
                messages = list(model.get("messages") or [])
                messages.insert(instruction_count(messages), {"role": "system", "content": pack})
                model["messages"] = messages
            return WebhookResponse(200, {"assistant": assistant, "assistantOverrides": overrides})
        answer: dict[str, Any] = {"assistantOverrides": overrides}
        if self.assistant_id is not None:
            answer["assistantId"] = self.assistant_id
        return WebhookResponse(200, answer)

    async def _tools(self, message: Mapping[str, Any]) -> WebhookResponse:
        session = self._session(message, self.tool_verification)
        kit = session.tools() if session is not None else None
        results: list[dict[str, Any]] = []
        for call in message.get("toolCallList") or []:
            call = mapping(call)
            function = mapping(call.get("function"))
            name = str(function.get("name") or "")
            result: dict[str, Any] = {"toolCallId": call.get("id"), "name": name}
            if name not in _NAMES:
                result["error"] = f"unknown tool {name}"
            elif kit is None:
                result["result"] = json.dumps(
                    {"error": "no customer on this call; answer without the history"}
                )
            else:
                result["result"] = await call_tool(kit, name, function.get("arguments"))
            results.append(result)
        return WebhookResponse(200, {"results": results})

    async def _report(self, message: Mapping[str, Any]) -> None:
        session = self._session(message)
        if session is None:
            return
        try:
            artifact = mapping(message.get("artifact"))
            variables = mapping(artifact.get("variableValues"))
            restore_stamp(session, variables.get(ETAG_VARIABLE), variables.get(INJECTED_VARIABLE))
            call_id = session.id
            for index, item in enumerate(artifact.get("messages") or []):
                self._turn(session, call_id, index, mapping(item))
            reason = str(message.get("endedReason") or "")
            if "forwarded" in reason or artifact.get("transfers"):
                handoff(session, "human", reason=reason or "transfer")
            end(session)
            await maybe_await(self.niadra.flush(5.0))
        except Exception as exc:
            warn("record the call", exc)

    def _turn(self, session: Session, call_id: str, index: int, item: Mapping[str, Any]) -> None:
        said = text(item.get("message"))
        role = item.get("role")
        if said is None or role not in ("user", "bot", "assistant"):
            return
        extra: dict[str, Any] = {"idempotency_key": f"vapi:{call_id}:{index}"}
        moment = item.get("time")
        if isinstance(moment, (int, float)) and not isinstance(moment, bool):
            extra["occurred_at"] = datetime.fromtimestamp(moment / 1000, tz=timezone.utc)
        if role == "user":
            record(session, "record the customer's turn", lambda s: s.customer(said, **extra))
        else:
            record(session, "record the agent's turn", lambda s: s.agent(said, **extra))

    def _transfer(self, message: Mapping[str, Any], target: str) -> WebhookResponse:
        session = self._session(message)
        handoff(session, "human" if target == "human" else "agent", reason=f"vapi {target} transfer")
        destination = None
        if self.destination is not None:
            try:
                destination = self.destination(message)
            except Exception as exc:
                warn("choose the destination", exc)
        return WebhookResponse(200, {"destination": dict(destination)} if destination else {})
