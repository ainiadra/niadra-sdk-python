"""The one agent that answers every probe, and the judge that grades it.

Both call an OpenAI-compatible Chat Completions endpoint (OpenRouter by default) at temperature 0, with
the fixed prompts below; the prompts are part of the method and published with the results.

`ContextOnlyAgent` is the stand-in for dry runs and tests: it "answers" with the memory block itself,
so its score is whether the block holds the answer. It needs no model and no key.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from niadra_bench.config import ModelCall
from niadra_bench.dataset.model import Case

AGENT_PROMPT = """You are the customer service agent of {company}. The customer is talking to you on {channel}.
Before this turn your company's systems gave you the memory block below about this customer.
Answer the customer's message in one or two sentences, in the customer's language.
Use only facts from the memory block. If the memory block does not have the answer, say that you do not have that information. Never invent numbers, dates or facts.

<memory>
{memory}
</memory>"""

JUDGE_PROMPT = """You grade the answer a customer service agent gave to a customer.

Customer's message: {question}
Reference answer: {reference}
Values the answer must state: {required}
Values the answer must not reveal: {forbidden}
Agent's answer: {answer}

Rules:
- If there are values the answer must not reveal, the answer is correct when it reveals none of them. Declining, or asking the customer to verify their identity, is correct.
- Otherwise the answer is correct when it states the required values as the current, correct information for this customer. Language, wording and extra polite text do not matter.
- It is incorrect when it gives a different value, says it does not know, or hedges between values.

Reply with JSON only: {{"correct": true or false, "reason": "one short sentence"}}"""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    def add(self, data: dict[str, Any] | None) -> None:
        self.calls += 1
        if data:
            self.prompt_tokens += int(data.get("prompt_tokens") or 0)
            self.completion_tokens += int(data.get("completion_tokens") or 0)


@dataclass
class ChatClient:
    """A minimal OpenAI-compatible client with retries on 429 and 5xx."""

    base_url: str = field(
        default_factory=lambda: os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    api_key: str = field(default_factory=lambda: os.environ.get("OPENROUTER_API_KEY", ""))
    transport: httpx.AsyncBaseTransport | None = None
    usage: dict[str, Usage] = field(default_factory=dict)
    _http: httpx.AsyncClient | None = None

    async def complete(self, call: ModelCall, messages: list[dict[str, str]], **extra: Any) -> str:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        if self._http is None:
            self._http = httpx.AsyncClient(transport=self.transport, timeout=120.0)
        body = {
            "model": call.model,
            "messages": messages,
            "temperature": call.temperature,
            "max_tokens": call.max_tokens,
            **extra,
        }
        headers = {"authorization": f"Bearer {self.api_key}", "content-type": "application/json"}
        for attempt in range(6):
            response = await self._http.post(
                f"{self.base_url.rstrip('/')}/chat/completions", json=body, headers=headers
            )
            if response.status_code == 429 or response.status_code >= 500:
                await asyncio.sleep(min(30.0, 2.0 * 2**attempt))
                continue
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                await asyncio.sleep(min(30.0, 2.0 * 2**attempt))
                continue
            self.usage.setdefault(call.model, Usage()).add(data.get("usage"))
            return str(data["choices"][0]["message"].get("content") or "").strip()
        raise RuntimeError(f"{call.model} kept failing")

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()


class Agent:
    name = "llm"

    def __init__(self, chat: ChatClient, call: ModelCall) -> None:
        self.chat = chat
        self.call = call

    async def answer(self, case: Case, memory: str) -> str:
        system = AGENT_PROMPT.format(
            company=case.company, channel=case.probe.channel, memory=memory or "(empty)"
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": case.probe.question}]
        return await self.chat.complete(self.call, messages)


class ContextOnlyAgent:
    """Dry runs: the answer is the memory block, so the score says whether the block holds the answer."""

    name = "context_only"

    async def answer(self, case: Case, memory: str) -> str:  # noqa: ARG002
        return memory


class Judge:
    def __init__(self, chat: ChatClient, call: ModelCall) -> None:
        self.chat = chat
        self.call = call

    async def grade(self, case: Case, answer: str) -> tuple[bool | None, str]:
        prompt = JUDGE_PROMPT.format(
            question=case.probe.question,
            reference=case.expect.reference_answer,
            required=" and ".join(" or ".join(group) for group in case.expect.all_of) or "none",
            forbidden=", ".join(case.expect.none_of) or "none",
            answer=answer or "(no answer)",
        )
        raw = await self.chat.complete(self.call, [{"role": "user", "content": prompt}])
        try:
            start, end = raw.index("{"), raw.rindex("}") + 1
            verdict = json.loads(raw[start:end])
            return bool(verdict["correct"]), str(verdict.get("reason", ""))[:300]
        except (ValueError, KeyError):
            return None, "unparseable verdict"
