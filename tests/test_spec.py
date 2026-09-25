"""The SDK's pack models against the Context Pack schema they implement (spec/context-pack.v0.json):
the same fields, the same section names, and the specification's example read without loss."""

import json
import typing
from pathlib import Path

from niadra.models.context import ContextPack, ContextResponse, PackSection, PackSectionName, PackStamp

SPEC = Path(__file__).resolve().parents[1] / "spec"
SCHEMA = json.loads((SPEC / "context-pack.v0.json").read_text())
DEFS = SCHEMA["$defs"]


def test_the_schema_is_the_context_pack_v0() -> None:
    assert SCHEMA["$id"] == "https://specs.niadra.com/schemas/context-pack.v0.json"
    assert SCHEMA["properties"]["pack"]["anyOf"][0] == {"$ref": "#/$defs/ContextPack"}
    assert DEFS["ContextPack"]["properties"]["spec"]["const"] == ContextPack.model_fields["spec"].default


def test_each_pack_model_has_exactly_the_fields_of_its_schema() -> None:
    for model, name in ((ContextPack, "ContextPack"), (PackSection, "PackSection"), (PackStamp, "PackStamp")):
        assert set(model.model_fields) == set(DEFS[name]["properties"]), name
        required = {f for f, info in model.model_fields.items() if info.is_required()}
        assert required <= set(DEFS[name]["required"]), name


def test_section_names_are_the_ones_the_specification_fixes() -> None:
    assert set(typing.get_args(PackSectionName)) == set(DEFS["PackSection"]["properties"]["name"]["enum"])
    assert set(DEFS["PackSection"]["properties"]["layer"]["enum"]) == {"account", "stable", "volatile"}


def test_the_specifications_example_reads_without_loss() -> None:
    example = json.loads((SPEC / "examples" / "context-pack-as-data.json").read_text())
    answer = ContextResponse.model_validate(example)
    assert answer.pack is not None
    assert answer.pack.model_dump(mode="json", exclude_none=True) == {
        k: v for k, v in example["pack"].items() if v is not None
    }
    inner = example["text"].split("\n")[1:-1]
    assert [answer.pack.preamble, *(line for s in answer.pack.sections for line in s.lines)] == inner
