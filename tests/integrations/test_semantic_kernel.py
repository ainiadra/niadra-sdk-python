"""Semantic Kernel: a real `Kernel` and `ChatCompletionAgent` with the plugin, the filters and the
thread, a scripted chat service with Semantic Kernel's own function calling loop, and the emulator
behind."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

pytest.importorskip("semantic_kernel")

from openai.types.completion_usage import PromptTokensDetails
from pydantic import Field
from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai import FunctionChoiceBehavior, PromptExecutionSettings
from semantic_kernel.connectors.ai.chat_completion_client_base import ChatCompletionClientBase
from semantic_kernel.connectors.ai.completion_usage import CompletionUsage
from semantic_kernel.connectors.ai.function_calling_utils import (
    kernel_function_metadata_to_function_call_format,
)
from semantic_kernel.contents import AuthorRole, ChatHistory, ChatMessageContent, FunctionCallContent
from semantic_kernel.functions import KernelArguments

from niadra import AsyncNiadra, Niadra
from niadra.integrations.semantic_kernel import NiadraKernel
from niadra_mock import MockApp
from tests.integrations.support import DEFINITIONS, EARLIER, MARINA, items, seed, seed_async, turns

INSTRUCTIONS = "You are Acme's agent."


class FakeChat(ChatCompletionClientBase):
    """A chat service that answers from a script and keeps every prompt and tool list it got."""

    SUPPORTS_FUNCTION_CALLING: ClassVar[bool] = True
    replies: list[Any] = Field(default_factory=list)
    prompts: list[list[Any]] = Field(default_factory=list)
    offered: list[list[dict[str, Any]]] = Field(default_factory=list)

    async def _inner_get_chat_message_contents(
        self, chat_history: ChatHistory, settings: PromptExecutionSettings
    ) -> list[ChatMessageContent]:
        self.prompts.append(list(chat_history.messages))
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, tuple):
            name, arguments = reply
            call = FunctionCallContent(id="call-1", name=f"niadra-{name}", arguments=json.dumps(arguments))
            return [ChatMessageContent(role=AuthorRole.ASSISTANT, items=[call])]
        usage = CompletionUsage(
            prompt_tokens=1200,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=1024),
            completion_tokens=7,
        )
        return [
            ChatMessageContent(
                role=AuthorRole.ASSISTANT, content=reply, ai_model_id="gpt-4.1", metadata={"usage": usage}
            )
        ]

    def _update_function_choice_settings_callback(self) -> Any:
        def update(configuration: Any, settings: Any, choice_type: Any) -> None:
            self.offered.append(
                [
                    kernel_function_metadata_to_function_call_format(f)
                    for f in configuration.available_functions
                ]
            )

        return update


def agent_for(memory: NiadraKernel, chat: FakeChat) -> ChatCompletionAgent:
    return ChatCompletionAgent(
        service=chat,
        kernel=memory.register(Kernel()),
        name="acme",
        instructions=INSTRUCTIONS,
        function_choice_behavior=FunctionChoiceBehavior.Auto(),
    )


@pytest.fixture
async def chat(on_mock_async: AsyncNiadra) -> Any:
    await seed_async(on_mock_async)
    return on_mock_async.conversation("sk-1", subject=MARINA)


async def test_the_agent_thread_places_the_pack_after_the_instructions_and_records_turns(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    service = FakeChat(ai_model_id="gpt-4.1", replies=["Your new lid ships today."])
    memory = NiadraKernel(chat)
    thread = memory.thread()
    response = await agent_for(memory, service).get_response(messages="About my lid", thread=thread)
    assert str(response.content) == "Your new lid ships today."
    prompt = service.prompts[0]
    assert (prompt[0].role, prompt[0].content) == (AuthorRole.SYSTEM, INSTRUCTIONS)
    assert prompt[1].role == AuthorRole.SYSTEM and EARLIER in prompt[1].content
    assert prompt[2].content == "About my lid"
    stored = thread._chat_history.messages
    assert [m.content for m in stored] == ["About my lid", "Your new lid ships today."], "only the chat"
    await on_mock_async.flush()
    assert turns(mock_app.cell, "sk-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]
    reply = next(e.item for e in mock_app.cell.events if e.item.speaker.role.value == "ai_agent")
    assert reply.usage is not None and (reply.usage.prompt_tokens, reply.usage.cached_tokens) == (1200, 1024)


async def test_the_plugin_is_the_kits_tools_and_runs_for_the_customer(chat: Any) -> None:
    memory = NiadraKernel(chat)
    service = FakeChat(
        ai_model_id="gpt-4.1", replies=[("search_customer_history", {"query": "lid"}), "4471."]
    )
    await agent_for(memory, service).get_response(messages="What did I report?", thread=memory.thread())
    offered = {t["function"]["name"]: t["function"] for t in service.offered[0]}
    assert set(offered) == {f"niadra-{name}" for name in DEFINITIONS}
    for name, definition in DEFINITIONS.items():
        function = offered[f"niadra-{name}"]
        assert function["description"] == definition["description"]
        assert function["parameters"]["properties"] == definition["parameters"]["properties"]
        assert function["parameters"]["required"] == definition["parameters"].get("required", [])
    results = [m for m in service.prompts[1] if m.role == AuthorRole.TOOL]
    assert "4471" in str(results[0].items[0].result)


async def test_a_prompt_function_gets_the_pack_through_the_filters(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    service = FakeChat(ai_model_id="gpt-4.1", replies=["Your new lid ships today."])
    kernel = NiadraKernel(chat).register(Kernel())
    kernel.add_service(service)
    template = f'<message role="system">{INSTRUCTIONS}</message><message role="user">{{{{$text}}}}</message>'
    result = await kernel.invoke_prompt(template, arguments=KernelArguments(text="About my lid"))
    assert str(result) == "Your new lid ships today."
    prompt = service.prompts[0]
    assert prompt[0].content == INSTRUCTIONS
    assert prompt[1].role == AuthorRole.SYSTEM and EARLIER in prompt[1].content
    assert prompt[2].content == "About my lid"
    await on_mock_async.flush()
    assert turns(mock_app.cell, "sk-1") == [
        ("customer", "About my lid"),
        ("ai_agent", "Your new lid ships today."),
    ]


async def test_a_history_for_a_direct_service_call(chat: Any) -> None:
    history = ChatHistory(system_message=INSTRUCTIONS)
    history.add_user_message("About my lid")
    placed = await NiadraKernel(chat).chat_history(history)
    assert placed.messages[0].content == INSTRUCTIONS and EARLIER in placed.messages[1].content
    assert len(history.messages) == 2, "the history given is left alone"


async def test_the_agents_notes_and_transfers(
    chat: Any, on_mock_async: AsyncNiadra, mock_app: MockApp
) -> None:
    await on_mock_async.remember(
        "procedure", "Replacement parts", "Open a replacement order before any refund."
    )
    memory = NiadraKernel(chat, agent_memory={"write": True})
    names = [f.name for f in memory.plugin.functions.values()]
    assert names[-2:] == ["search_agent_memory", "remember"]
    service = FakeChat(ai_model_id="gpt-4.1", replies=["Opening it."])
    await agent_for(memory, service).get_response(messages="Hi", thread=memory.thread())
    slot = service.prompts[0][1].content
    assert slot.index("replacement order") < slot.index(EARLIER)
    memory.transferred_to_agent("billing")
    memory.transferred_to_human("asked for a person")
    await on_mock_async.flush()
    assert [h.target for h in items(mock_app.cell, "handoff")] == ["agent", "human"]


async def test_niadra_down_never_stops_the_agent(chat: Any, mock_app: MockApp) -> None:
    mock_app.cell.fail_next("/v1/", 503, times=100)
    memory = NiadraKernel(chat)
    service = FakeChat(
        ai_model_id="gpt-4.1", replies=[("search_customer_history", {"query": "lid"}), "Still here."]
    )
    response = await agent_for(memory, service).get_response(messages="Hi", thread=memory.thread())
    assert str(response.content) == "Still here."
    assert not any(EARLIER in str(m.content) for m in service.prompts[0])
    results = [m for m in service.prompts[1] if m.role == AuthorRole.TOOL]
    assert "unavailable" in str(results[0].items[0].result)


async def test_the_sync_client(on_mock: Niadra, mock_app: MockApp) -> None:
    seed(on_mock)
    memory = NiadraKernel(on_mock.conversation("sk-sync", subject=MARINA))
    service = FakeChat(
        ai_model_id="gpt-4.1", replies=[("search_customer_history", {"query": "lid"}), "Done."]
    )
    await agent_for(memory, service).get_response(messages="About my lid", thread=memory.thread())
    assert EARLIER in service.prompts[0][1].content
    result = next(m for m in service.prompts[1] if m.role == AuthorRole.TOOL)
    assert "4471" in str(result.items[0].result)
    on_mock.flush()
    assert turns(mock_app.cell, "sk-sync") == [("customer", "About my lid"), ("ai_agent", "Done.")]
