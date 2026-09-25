"""ElevenLabs Agents Platform: the customer's memory in a phone agent, from your server.

A call to an ElevenLabs agent reaches your server three ways, and this module answers each:

1. **Conversation initiation** ("Fetch initiation client data from a webhook"): before the agent
   speaks, `conversation_initiation()` verifies the carrier's attestation when you pass one, reads
   `context(view="voice")` for the caller and answers the dynamic variable `niadra_context`. Put
   `{{niadra_context}}` in the agent's system prompt, after your instructions.
2. **Server tools**: `tool_configs(url)` gives the three history tools as ElevenLabs webhook tools,
   with the same names, descriptions and parameters as every Niadra kit. The call's identifiers
   (`system__call_sid`, `system__conversation_id`, `system__caller_id`) are filled by ElevenLabs
   from the call, never by the model, and `server_tool()` binds the kit to that caller.
3. **Post-call webhook** (`post_call_transcription`): `post_call()` checks the `ElevenLabs-Signature`
   (HMAC-SHA256 of `"<t>.<body>"`, 30 minutes of tolerance), records each transcript item as a
   turn at its moment in the call (with the model's usage), records a `transfer_to_agent` or
   `transfer_to_number` as a handoff, and ends the conversation. Items carry idempotency keys
   from the call, so a redelivered webhook records nothing twice.

```python
from fastapi import FastAPI, Request, Response
from niadra import AsyncNiadra
from niadra.integrations.elevenlabs import ElevenLabsWebhooks

niadra = AsyncNiadra(channel="voice")
hooks = ElevenLabsWebhooks(niadra, webhook_secret=POST_CALL_SECRET, shared_secret=HEADER_SECRET)
app = FastAPI()


@app.post("/elevenlabs/initiation")
async def initiation(request: Request) -> Response:
    answer = await hooks.conversation_initiation(await request.body(), request.headers)
    return Response(answer.text(), answer.status, media_type=answer.content_type)
```

The Niadra conversation id is the phone call's `call_sid` (else ElevenLabs' conversation id), the
same in the three webhooks. The subject is the caller's number; pass `subject=` (a function of the
call's variables) for callers identified otherwise. The initiation webhook and the server tools
check the `X-Niadra-Secret` header you configure in ElevenLabs against `shared_secret`, since they
return customer data: without it they answer 401.

Nothing here fails a call because Niadra is slow or down: the initiation answers an empty
context, a tool answers that the history is unavailable, and the post-call webhook still answers 200.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from niadra._async_client import AsyncNiadra
from niadra._client import Niadra
from niadra.integrations._common import (
    blocks,
    call_tool,
    end,
    handoff,
    join_instructions,
    maybe_await,
    model_usage,
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
    same,
    text,
)
from niadra.models.common import Handle
from niadra.tools import BUILTIN_DEFINITIONS
from niadra.vocabulary import Verification

__all__ = ["CallVariables", "ElevenLabsWebhooks", "tool_configs", "verify_signature"]

SIGNATURE_HEADER = "ElevenLabs-Signature"
SECRET_HEADER = "X-Niadra-Secret"  # noqa: S105 - a header name
TOLERANCE = 30 * 60

CONTEXT_VARIABLE = "niadra_context"
ETAG_VARIABLE = "niadra_context_etag"
INJECTED_VARIABLE = "niadra_injected_at"

# ElevenLabs fills these from the call: the model never sees or sets them.
_SYSTEM = {
    "system__call_sid": "call_sid",
    "system__conversation_id": "conversation_id",
    "system__caller_id": "caller_id",
}
_TRANSFERS = {"transfer_to_agent": "agent", "transfer_to_number": "human"}


@dataclass(frozen=True)
class CallVariables:
    """What identifies one call, whichever webhook it arrived in."""

    call_sid: str | None = None
    conversation_id: str | None = None
    caller_id: str | None = None
    called_number: str | None = None
    agent_id: str | None = None
    user_id: str | None = None

    @property
    def niadra_id(self) -> str | None:
        """The Niadra conversation id: the phone call's id when there is one, else ElevenLabs'."""
        return self.call_sid or self.conversation_id


def verify_signature(
    body: Body,
    signature: str | None,
    secret: str | None,
    *,
    now: float | None = None,
    tolerance: int = TOLERANCE,
) -> bool:
    """Checks an `ElevenLabs-Signature` header: `t=<unix seconds>,v0=<hex HMAC-SHA256 of "t.body">`."""
    if not signature or not secret:
        return False
    parts = dict(part.strip().split("=", 1) for part in signature.split(",") if "=" in part)
    stamp, given = parts.get("t"), parts.get("v0")
    if not stamp or not given or not stamp.isdigit():
        return False
    moment = time.time() if now is None else now
    if abs(moment - int(stamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), stamp.encode() + b"." + raw(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


def _literal(schema: Mapping[str, Any]) -> dict[str, Any]:
    """One JSON Schema property in ElevenLabs' tool schema, which keeps type, description and enum."""
    kind = schema.get("type", "string")
    if kind == "array":
        items = _literal(schema.get("items", {"type": "string"}))
        items.setdefault("description", "")
        return {"type": "array", "description": schema.get("description", ""), "items": items}
    prop: dict[str, Any] = {"type": kind, "description": schema.get("description", "")}
    if "enum" in schema:
        prop["enum"] = list(schema["enum"])
    return prop


def tool_configs(url: str, *, secret: str | None = None) -> list[dict[str, Any]]:
    """The history tools as ElevenLabs webhook tool configurations, one `POST {url}/{name}` each.

    Names, descriptions and parameters are the kit's. Three more body fields carry the call's
    identifiers from ElevenLabs' system variables. With `secret`, each request sends it in
    `X-Niadra-Secret` (store it as an ElevenLabs secret in production).
    """
    configs: list[dict[str, Any]] = []
    for spec in tool_specs(BUILTIN_DEFINITIONS):
        properties = {name: _literal(prop) for name, prop in spec.parameters.get("properties", {}).items()}
        for variable in _SYSTEM:
            properties[variable] = {"type": "string", "description": "", "dynamic_variable": variable}
        api_schema: dict[str, Any] = {
            "url": f"{url.rstrip('/')}/{spec.name}",
            "method": "POST",
            "request_body_schema": {
                "type": "object",
                "properties": properties,
                "required": list(spec.parameters.get("required", [])),
            },
        }
        if secret is not None:
            api_schema["request_headers"] = {SECRET_HEADER: secret}
        configs.append(
            {"type": "webhook", "name": spec.name, "description": spec.description, "api_schema": api_schema}
        )
    return configs


class ElevenLabsWebhooks:
    """The three ElevenLabs webhooks of one agent, answered with Niadra.

    `niadra` is a `Niadra` or an `AsyncNiadra`: await the methods with the async client, and call
    the `*_sync` twins with the sync one. `attestation`, when given, returns the carrier's
    STIR/SHAKEN level (`A`, `B` or `C`) for a call, from the initiation payload (for instance a
    SIP header ElevenLabs passed along); it is verified before the first context. `clock` returns
    the current Unix time, for the signature window.
    """

    def __init__(
        self,
        niadra: Niadra | AsyncNiadra,
        *,
        webhook_secret: str | None = None,
        shared_secret: str | None = None,
        subject: Callable[[CallVariables], Handle | None] | None = None,
        attestation: Callable[[Mapping[str, Any]], str | None] | None = None,
        channel: str = "voice",
        view: str = "voice",
        agent_id: str | None = None,
        tool_verification: Verification | str = Verification.V2,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.niadra = niadra
        self.webhook_secret = webhook_secret
        self.shared_secret = shared_secret
        self.subject = subject or (lambda call: phone_or_none(call.caller_id))
        self.attestation = attestation
        self.channel = channel
        self.view = view
        self.agent_id = agent_id
        # A tool asks for this level; the API serves the lower one the conversation proved.
        self.tool_verification = Verification(tool_verification)
        self.clock = clock or time.time

    def _session(
        self, call: CallVariables, verification: Verification | str = Verification.V0
    ) -> Session | None:
        if not call.niadra_id:
            return None
        try:
            handle = self.subject(call)
        except Exception as exc:
            warn("resolve the caller", exc)
            handle = None
        return self.niadra.conversation(
            call.niadra_id,
            subject=handle,
            channel=self.channel,
            view=self.view,
            verification=verification,
            agent_id=self.agent_id,
        )

    def _authorized(self, headers: Mapping[str, str] | None) -> bool:
        return same(header(headers, SECRET_HEADER), self.shared_secret)

    async def conversation_initiation(
        self, body: Body, headers: Mapping[str, str] | None = None
    ) -> WebhookResponse:
        """Answers the initiation webhook with `niadra_context` (and its stamp) as dynamic variables."""
        if not self._authorized(headers):
            return UNAUTHORIZED
        payload = parse(body)
        if payload is None:
            return BAD_REQUEST
        call = CallVariables(
            call_sid=text(payload.get("call_sid")),
            conversation_id=text(payload.get("conversation_id")),
            caller_id=text(payload.get("caller_id")),
            called_number=text(payload.get("called_number")),
            agent_id=text(payload.get("agent_id")),
        )
        variables: dict[str, str] = {CONTEXT_VARIABLE: "", ETAG_VARIABLE: "", INJECTED_VARIABLE: ""}
        session = self._session(call)
        if session is not None:
            if self.attestation is not None:
                try:
                    level = self.attestation(payload)
                except Exception as exc:
                    warn("read the attestation", exc)
                    level = None
                await verify_attestation(session, level)
            context = await read_context(session)
            if context is not None:
                variables[CONTEXT_VARIABLE] = join_instructions(*blocks(context))
                variables[ETAG_VARIABLE] = context.etag
                variables[INJECTED_VARIABLE] = datetime.now(timezone.utc).isoformat()
        return WebhookResponse(
            200, {"type": "conversation_initiation_client_data", "dynamic_variables": variables}
        )

    async def server_tool(
        self, name: str, body: Body, headers: Mapping[str, str] | None = None
    ) -> WebhookResponse:
        """Runs one history tool for the call named by the system variables in the body."""
        if not self._authorized(headers):
            return UNAUTHORIZED
        payload = parse(body)
        if payload is None:
            return BAD_REQUEST
        known = {spec.name for spec in tool_specs(BUILTIN_DEFINITIONS)}
        if name not in known:
            return WebhookResponse(404, {"error": f"unknown tool {name}"})
        identifiers = {field: text(payload.pop(variable, None)) for variable, field in _SYSTEM.items()}
        session = self._session(CallVariables(**identifiers), self.tool_verification)
        kit = session.tools() if session is not None else None
        if kit is None:
            return WebhookResponse(200, {"error": "no customer on this call; answer without the history"})
        return WebhookResponse(200, json.loads(await call_tool(kit, name, payload)))

    async def post_call(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """Records a `post_call_transcription`: every turn, the transfers, and the end of the call."""
        signature = header(headers, SIGNATURE_HEADER)
        if not verify_signature(body, signature, self.webhook_secret, now=self.clock()):
            return UNAUTHORIZED
        payload = parse(body)
        if payload is None:
            return BAD_REQUEST
        if payload.get("type") != "post_call_transcription":
            return OK
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return BAD_REQUEST
        try:
            await self._record(data)
        except Exception as exc:
            warn("record the call", exc)
        return OK

    async def _record(self, data: Mapping[str, Any]) -> None:
        metadata = mapping(data.get("metadata"))
        phone_call = mapping(metadata.get("phone_call"))
        variables = mapping(mapping(data.get("conversation_initiation_client_data")).get("dynamic_variables"))
        call = CallVariables(
            call_sid=text(phone_call.get("call_sid")) or text(variables.get("system__call_sid")),
            conversation_id=text(data.get("conversation_id")),
            caller_id=text(phone_call.get("external_number")) or text(variables.get("system__caller_id")),
            called_number=text(phone_call.get("agent_number")),
            agent_id=text(data.get("agent_id")),
            user_id=text(data.get("user_id")),
        )
        session = self._session(call)
        if session is None:
            return
        restore_stamp(session, variables.get(ETAG_VARIABLE), variables.get(INJECTED_VARIABLE))
        started = metadata.get("start_time_unix_secs")
        for index, item in enumerate(data.get("transcript") or []):
            if isinstance(item, Mapping):
                self._turn(session, call, index, item, started)
        end(session)
        await maybe_await(self.niadra.flush(5.0))

    def _turn(
        self, session: Session, call: CallVariables, index: int, item: Mapping[str, Any], started: Any
    ) -> None:
        key = f"elevenlabs:{call.niadra_id}:{index}"
        occurred = _moment(started, item.get("time_in_call_secs"))
        message = text(item.get("message"))
        extra: dict[str, Any] = {"idempotency_key": key}
        if occurred is not None:
            extra["occurred_at"] = occurred
        if message and item.get("role") == "user":
            record(session, "record the customer's turn", lambda s: s.customer(message, **extra))
        elif message and item.get("role") == "agent":
            usage = _usage(item.get("llm_usage"))
            record(session, "record the agent's turn", lambda s: s.agent(message, usage=usage, **extra))
        for tool_call in item.get("tool_calls") or []:
            target = _TRANSFERS.get(str((tool_call or {}).get("tool_name")))
            if target == "agent":
                handoff(session, "agent", reason="transfer_to_agent")
            elif target == "human":
                handoff(session, "human", reason="transfer_to_number")

    def _sync(self) -> ElevenLabsWebhooks:
        if isinstance(self.niadra, AsyncNiadra):
            raise TypeError("the *_sync methods take a Niadra client; await the others with AsyncNiadra")
        return self

    def conversation_initiation_sync(
        self, body: Body, headers: Mapping[str, str] | None = None
    ) -> WebhookResponse:
        """`conversation_initiation()` for a sync `Niadra` client."""
        return run_sync(self._sync().conversation_initiation(body, headers))

    def server_tool_sync(
        self, name: str, body: Body, headers: Mapping[str, str] | None = None
    ) -> WebhookResponse:
        """`server_tool()` for a sync `Niadra` client."""
        return run_sync(self._sync().server_tool(name, body, headers))

    def post_call_sync(self, body: Body, headers: Mapping[str, str] | None) -> WebhookResponse:
        """`post_call()` for a sync `Niadra` client."""
        return run_sync(self._sync().post_call(body, headers))


def _moment(started: Any, offset: Any) -> datetime | None:
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return None
    seconds = offset if isinstance(offset, (int, float)) and not isinstance(offset, bool) else 0
    return datetime.fromtimestamp(started + seconds, tz=timezone.utc)


def _usage(value: Any) -> Any:
    """The usage of the model call behind an agent's turn, from `llm_usage.model_usage`."""
    models = (value or {}).get("model_usage") if isinstance(value, Mapping) else None
    if not isinstance(models, Mapping) or not models:
        return None
    model, counts = next(iter(models.items()))

    def tokens(kind: str) -> int:
        category = counts.get(kind) if isinstance(counts, Mapping) else None
        amount = category.get("tokens") if isinstance(category, Mapping) else None
        return amount if isinstance(amount, int) else 0

    read, written = tokens("input_cache_read"), tokens("input_cache_write")
    return model_usage(None, str(model), tokens("input") + read + written, read, written)
