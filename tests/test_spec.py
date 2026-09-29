"""The SDK's pack models against the Context Pack schema they implement (spec/context-pack.v1.json):
the same fields, the same section names, and the specification's example read without loss."""

import json
import typing
from pathlib import Path

from niadra.models.context import (
    ContextPack,
    ContextResponse,
    CoordinationBlock,
    PackGuard,
    PackSection,
    PackSectionName,
    PackSlot,
    PackSlotDerived,
    PackStamp,
    SlotChannelRank,
    SlotWhy,
)
from niadra.models.results import Context
from niadra.models.signals import ConstraintsBlock
from niadra.models.state import StateView

SPEC = Path(__file__).resolve().parents[1] / "spec"
SCHEMA = json.loads((SPEC / "context-pack.v1.json").read_text())
DEFS = SCHEMA["$defs"]


def test_the_schema_is_the_context_pack_v1() -> None:
    assert SCHEMA["$id"] == "https://specs.niadra.com/schemas/context-pack.v1.json"
    assert SCHEMA["properties"]["pack"]["anyOf"][0] == {"$ref": "#/$defs/ContextPack"}
    assert DEFS["ContextPack"]["properties"]["spec"]["const"] == ContextPack.model_fields["spec"].default
    assert "slots" in SCHEMA["properties"] and "slots" in ContextResponse.model_fields
    assert "guards" in SCHEMA["properties"] and "guards" in ContextResponse.model_fields


def test_each_pack_model_has_exactly_the_fields_of_its_schema() -> None:
    models = (
        (ContextPack, "ContextPack"),
        (PackSection, "PackSection"),
        (PackStamp, "PackStamp"),
        (PackSlot, "PackSlot"),
        (SlotWhy, "SlotWhy"),
        (SlotChannelRank, "SlotChannelRank"),
        (PackGuard, "PackGuard"),
    )
    for model, name in models:
        assert set(model.model_fields) == set(DEFS[name]["properties"]), name
        required = {f for f, info in model.model_fields.items() if info.is_required()}
        assert required <= set(DEFS[name].get("required", [])), name


def test_pack_slot_why_is_explain() -> None:
    assert PackSlot.model_fields["why"].annotation == SlotWhy | None
    assert "SlotWhy" in DEFS and "SlotChannelRank" in DEFS
    channels = DEFS["SlotWhy"]["properties"]["channels"]["items"]
    assert channels == {"$ref": "#/$defs/SlotChannelRank"}


def test_section_names_are_the_ones_the_specification_fixes() -> None:
    assert set(typing.get_args(PackSectionName)) == set(DEFS["PackSection"]["properties"]["name"]["enum"])
    assert set(DEFS["PackSection"]["properties"]["layer"]["enum"]) == {"account", "stable", "volatile"}
    derived = DEFS["PackSlot"]["properties"]["derived"]["anyOf"][0]["enum"]
    assert set(typing.get_args(PackSlotDerived)) == set(derived)


def test_the_specifications_example_reads_without_loss() -> None:
    example = json.loads((SPEC / "examples" / "context-pack-v1-turn-as-data.json").read_text())
    answer = Context.model_validate(example)
    assert answer.pack is not None
    assert answer.pack.model_dump(mode="json") == example["pack"]
    # A section's lines carry no label; the text puts the section's label back before each one.
    inner = example["text"].split("\n")[1:-1]
    labeled = [f"[{s.label}] {line}" for s in answer.pack.sections for line in s.lines]
    assert [answer.pack.preamble, *labeled] == inner
    assert [slot.text for slot in answer.pack.slots] == example["slots"].split("\n")[1:-1]
    # The producer's example answers a turn of a conversation the pack already covers, so it carries no delta:
    # the block is the live turns, then the slots, where a delta would follow.
    block = answer.turn_block
    assert example["delta"] is None
    assert block.index("<live_turns") < block.index(example["slots"])
    assert block.endswith(example["slots"]), "the slots close the block when there is no delta"


V2 = json.loads((SPEC / "context-pack.v2.json").read_text())


def test_the_answer_has_the_blocks_of_the_context_pack_v2() -> None:
    assert V2["$id"] == "https://specs.niadra.com/schemas/context-pack.v2.json"
    assert set(V2["properties"]) == set(ContextResponse.model_fields)
    for name, model in (
        ("ConstraintsBlock", ConstraintsBlock),
        ("StateView", StateView),
        ("CoordinationBlock", CoordinationBlock),
    ):
        if name in V2["$defs"]:
            assert set(V2["$defs"][name]["properties"]) == {
                field.alias or key for key, field in model.model_fields.items()
            }, name
