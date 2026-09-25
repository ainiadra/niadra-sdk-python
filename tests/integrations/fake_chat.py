"""A chat model for the LangChain and LangGraph tests: scripted answers, every prompt kept."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


def answer(text: str, message_id: str = "msg-1") -> AIMessage:
    return AIMessage(
        content=text,
        id=message_id,
        usage_metadata={
            "input_tokens": 1200,
            "output_tokens": 7,
            "total_tokens": 1207,
            "input_token_details": {"cache_read": 1024},
        },
        response_metadata={"model_name": "gpt-4.1"},
    )


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="", id=f"msg-{call_id}", tool_calls=[{"name": name, "args": arguments, "id": call_id}]
    )


class FakeChat(BaseChatModel):
    replies: list[AIMessage]
    prompts: list[list[BaseMessage]] = Field(default_factory=list)
    tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        self.tools = list(tools)
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.prompts.append(list(messages))
        reply = self.replies.pop(0) if self.replies else AIMessage(content="ok")
        return ChatResult(generations=[ChatGeneration(message=reply)])
