from niadra_bench.dataset.model import Expectation
from niadra_bench.text import contains, matches_all, matches_none, normalize, passes


def test_normalize_drops_accents_case_and_punctuation() -> None:
    assert normalize("Três vezes, NÃO é?") == ["tres", "vezes", "nao", "e"]


def test_amounts_match_whatever_the_decimal_separator() -> None:
    assert contains("O crédito de R$ 37,90 saiu.", "37.90")
    assert contains("A credit of $37.90 went through.", "37,90")


def test_matching_is_by_whole_tokens() -> None:
    assert not contains("record 58217", "17")
    assert contains("by the 17th", "17th")
    assert contains("CEP 01310-100", "01310-100")
    assert not contains("anything", "")


def test_expectations() -> None:
    expect = Expectation(all_of=[["26", "26th"]], reference_answer="By the 26th.")
    assert passes("You said the 26th.", expect)
    assert not passes("You said the 25th.", expect)
    privacy = Expectation(none_of=["32388258"], verified_all_of=[["32388258"]], reference_answer="No.")
    assert passes("I can't share that.", privacy)
    assert not passes("It is 32388258.", privacy)
    assert passes("It is 32388258.", privacy, verified=True)
    assert matches_all("a b", []) and matches_none("a b", [])
