"""niadra-expr resolved against a type declaration: what the registry refuses, and why. The vectors in
`test_vectors.py` cover parsing and evaluation; resolution is the part they do not reach."""

from __future__ import annotations

import pytest

from niadra.state.expr import Expect, ExprError, Kind, Scope, compile_expression, duration_ms
from niadra.state.logic import Logic

SCOPE = Scope(
    fields={"available": Kind.BOOL, "price": Kind.NUMBER, "content": Kind.STRING, "tags": Kind.LIST},
    completeness=frozenset({"content"}),
    values=frozenset({"due_date"}),
    axes=frozenset({"published_on"}),
    absent_names=frozenset({"no_deadline"}),
    inputs=frozenset({"lead.city"}),
    states=frozenset({"open", "no_deadline"}),
    sources={"live": "pull", "checkout": "quote"},
)


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("available == yes and age(available) <= 5s", Expect.CONDITION),
        ("due_date != no_deadline and state == 'open'", Expect.CONDITION),
        ("quote(checkout).price < price", Expect.CONDITION),
        ("changed(lead.city) or age(lead.city) > 1d", Expect.CONDITION),
        ("content.completeness == 'full'", Expect.CONDITION),
        ("undeclared_field == 'x'", Expect.CONDITION),
        ("due_date - 48h", Expect.TIME),
        ("published_on + business_days(15, court)", Expect.TIME),
        ("launch_at", Expect.TIME),
        ("due_date - now()", Expect.ANY),
        ("sha256(content, published_on)", Expect.ANY),
        ("count(tags) > 2 and 'vip' in tags", Expect.CONDITION),
        ("config.discount_source == 'table'", Expect.CONDITION),
    ],
)
def test_accepted(text: str, expect: Expect) -> None:
    compile_expression(text, SCOPE, expect)


@pytest.mark.parametrize(
    ("text", "expect", "detail"),
    [
        ("age(available)", Expect.CONDITION, "a condition must be true or false, and this gives a duration"),
        ("'noon'", Expect.TIME, "a time must be a date or a datetime, and this gives a string"),
        ("state == open", Expect.CONDITION, "'open' is a state: write it as a string, 'open'"),
        ("live == yes", Expect.CONDITION, "'live' is a source: only quote() takes a source"),
        ("config == 'x'", Expect.CONDITION, "config takes a key: config.<key>"),
        ("lead.state == 'ny'", Expect.CONDITION, "unknown name 'lead.state'"),
        ("price.completeness == 'full'", Expect.CONDITION, "field 'price' declares no completeness levels"),
        (
            "age(state) > 1s",
            Expect.CONDITION,
            "age() takes a field, a value, a time axis or an input: 'state'",
        ),
        (
            "changed(lead.state)",
            Expect.CONDITION,
            "changed() takes a field, a value, a time axis or an input",
        ),
        ("quote(nothing).price > 1", Expect.CONDITION, "quote() names an unknown source 'nothing'"),
        (
            "quote(live).price > 1",
            Expect.CONDITION,
            "quote() takes a source of kind quote, and 'live' is pull",
        ),
        ("price.x == 1", Expect.CONDITION, "unknown name 'price.x'"),
        ("(price).x == 1", Expect.CONDITION, "only quote() has fields: '.x'"),
        ("count(price) > 1", Expect.CONDITION, "count() takes a list"),
        ("sha256(tags)", Expect.ANY, "sha256() takes strings, numbers, booleans, durations and times"),
        ("not age(available)", Expect.CONDITION, "not takes conditions, and one side gives a duration"),
        ("price and available", Expect.CONDITION, "and takes conditions, and one side gives a number"),
        ("'a' in content", Expect.CONDITION, "in takes a list on its right, and this is a string"),
        ("age(available) > 5", Expect.CONDITION, "> compares a duration with a number"),
        ("now() + now()", Expect.ANY, "+ does not apply to a datetime and a datetime"),
        ("business_days('3', court)", Expect.ANY, "business_days() counts a number of days"),
        ("business_days(3, court)", Expect.ANY, "an expression cannot end in a business days"),
        ("quote(checkout)", Expect.ANY, "an expression cannot end in a quote"),
    ],
)
def test_refused_with_the_reason(text: str, expect: Expect, detail: str) -> None:
    with pytest.raises(ExprError) as caught:
        compile_expression(text, SCOPE, expect)
    assert caught.value.code == "expr_invalid"
    assert detail in caught.value.detail


def test_a_duration_literal_reads_as_milliseconds() -> None:
    assert duration_ms("90min") == 5_400_000
    with pytest.raises(ExprError) as caught:
        duration_ms("1.5h")
    assert caught.value.code == "expr_invalid"


def test_the_enums_print_their_value_on_every_python() -> None:
    assert (str(Logic.KNOWN_DEFECT), f"{Kind.BUSINESS_DAYS}", f"{Expect.TIME}") == (
        "known_defect",
        "business_days",
        "time",
    )
