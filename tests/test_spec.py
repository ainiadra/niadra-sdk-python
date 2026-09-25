"""The SDK's pack models against the Context Pack schema they implement (spec/context-pack.v1.json):
the same fields, the same section names, and the specification's examples read without loss, the
earlier version's too."""

import json
import typing
from pathlib import Path

from niadra.models.context import (
    ContextPack,
    ContextResponse,
    PackSection,
    PackSectionName,
    PackSlot,
    PackSlotDerived,
    PackStamp,
)
from niadra.models.results import Context

SPEC = Path(__file__).resolve().parents[1] / "spec"
SCHEMA = json.loads((SPEC / "context-pack.v1.json").read_text())
DEFS = SCHEMA["$defs"]


def test_the_schema_is_the_context_pack_v1() -> None:
    assert SCHEMA["$id"] == "https://specs.niadra.com/schemas/context-pack.v1.json"
    assert SCHEMA["properties"]["pack"]["anyOf"][0] == {"$ref": "#/$defs/ContextPack"}
    assert DEFS["ContextPack"]["properties"]["spec"]["const"] == ContextPack.model_fields["spec"].default
    assert "slots" in SCHEMA["properties"] and "slots" in ContextResponse.model_fields


def test_each_pack_model_has_exactly_the_fields_of_its_schema() -> None:
    models = ((ContextPack, "ContextPack"), (PackSection, "PackSection"), (PackStamp, "PackStamp"))
    for model, name in (*models, (PackSlot, "PackSlot")):
        assert set(model.model_fields) == set(DEFS[name]["properties"]), name
        required = {f for f, info in model.model_fields.items() if info.is_required()}
        assert required <= set(DEFS[name]["required"]), name


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
    inner = example["text"].split("\n")[1:-1]
    assert [answer.pack.preamble, *(line for s in answer.pack.sections for line in s.lines)] == inner
    assert [slot.text for slot in answer.pack.slots] == example["slots"].split("\n")[2:-1]
    block = answer.turn_block
    assert block.index("<live_turns") < block.index(example["slots"]) < block.index(example["delta"]), (
        "the slots after the live turns and before the delta"
    )


def test_the_earlier_versions_example_still_reads() -> None:
    example = json.loads((SPEC / "examples" / "context-pack-as-data.json").read_text())
    answer = ContextResponse.model_validate(example)
    assert answer.pack is not None and answer.pack.spec == "context-pack.v0"
    assert answer.pack.slots == [] and answer.slots is None
    assert answer.pack.model_dump(mode="json", exclude_none=True, exclude={"slots"}) == {
        k: v for k, v in example["pack"].items() if v is not None
    }
