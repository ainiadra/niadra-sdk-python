"""The hand-written models take what the server takes: a client never refuses or cuts a value the API keeps
whole, and reads every field the API added."""

import json
from importlib import resources

import pytest
from pydantic import ValidationError

from niadra.models.admin import ProfileMemory
from niadra.models.agent_memory import AgentMemoryBlock, CreateAgentNoteRequest, UpdateAgentNoteRequest
from niadra.models.events import ActionInfo


def test_a_long_note_is_sent_whole() -> None:
    note = CreateAgentNoteRequest(kind="procedure", title="t" * 300, body="b" * 20000)
    assert len(note.body) == 20000
    with pytest.raises(ValidationError):
        CreateAgentNoteRequest(kind="procedure", title="t", body="b" * 20001)
    assert UpdateAgentNoteRequest(title="t" * 300, body="b" * 20000).body == "b" * 20000
    with pytest.raises(ValidationError):
        UpdateAgentNoteRequest(tags=[f"t{i}" for i in range(9)])


def test_an_action_result_up_to_the_event_text_limit_is_sent_whole() -> None:
    assert len(ActionInfo(operation="notify", result="r" * 10000).result or "") == 10000
    assert len(ActionInfo(operation="notify", result="r" * 200_000).result or "") == 200_000
    with pytest.raises(ValidationError):
        ActionInfo(operation="notify", result="r" * 200_001)


def test_what_a_block_and_a_profile_leave_for_later_is_read() -> None:
    block = AgentMemoryBlock.model_validate({"text": "x", "left_out": 3})
    assert block.left_out == 3
    memory = ProfileMemory.model_validate(
        {"profile_id": "p", "policy_version": "v", "audience": "internal", "timeline_next": "c1"}
    )
    assert memory.timeline_next == "c1"


def test_the_remember_tool_offers_the_servers_note_limits() -> None:
    definitions = json.loads(resources.files("niadra").joinpath("tool_definitions.json").read_text())
    remember = next(d for d in definitions if d["function"]["name"] == "remember")
    properties = remember["function"]["parameters"]["properties"]
    assert properties["title"]["maxLength"] == 300
    assert properties["body"]["maxLength"] == 20000
