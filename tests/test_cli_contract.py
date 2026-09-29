"""`niadra contract test`: the contract against its negative corpus and the company's example turns, with the
exit codes a CI job gates on."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from niadra import Niadra
from niadra import cli as command
from niadra.cli.contract import check_contract
from niadra.models.state import ClaimContractSummary
from niadra_mock import MockApp

SPEC = Path(__file__).resolve().parents[1] / "spec" / "examples" / "claim-contract"
RETAIL = SPEC / "retail.json"

EXAMPLES = {
    "examples": [
        {
            "id": "sale_price_with_evidence",
            "output": {"text": "Está por R$ 199,90.", "lang": "pt"},
            "turn": {
                "values": [
                    {"class": "money", "role": "price_sale", "value": {"amount": "199.9", "unit": "BRL"}}
                ]
            },
            "expect": {"claims": [{"category": "price", "verdict": "matched", "action": "none"}]},
        },
        {
            "id": "a_price_the_model_made_up",
            "output": {"text": "Esse tênis está por R$ 150,00.", "lang": "pt"},
            "turn": {},
            "expect": {"claims": [{"category": "price", "action": "warn"}]},
        },
        {
            "id": "no_claim_in_a_question",
            "output": {"text": "Quer que eu separe por cor ou por tamanho?", "lang": "pt"},
            "expect": {"claims": []},
        },
    ]
}


def test_the_example_contracts_pass_their_own_corpus(capsys: pytest.CaptureFixture[str]) -> None:
    for path in sorted(SPEC.glob("*.json")):
        assert command.main(["contract", "test", "--contract", str(path)]) == 0, path.name
        report = json.loads(capsys.readouterr().out)
        assert report["phrases"] > 0
        assert report["triggered"] == 0


def test_examples_pass_and_a_wrong_expectation_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps(EXAMPLES))
    assert command.main(["contract", "test", "--contract", str(RETAIL), "--examples", str(examples)]) == 0
    assert json.loads(capsys.readouterr().out)["examples"] == 3

    wrong = json.loads(json.dumps(EXAMPLES))
    wrong["examples"][2]["expect"]["claims"] = [{"category": "price"}]
    examples.write_text(json.dumps(wrong))
    assert command.main(["contract", "test", "--contract", str(RETAIL), "--examples", str(examples)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["failed"] == 1
    assert "example no_claim_in_a_question" in captured.err


def test_a_phrase_of_the_corpus_that_triggers_fails_the_ci(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"version": "2026-10-01", "phrases": ["Está por R$ 150,00 hoje."]}))
    assert command.main(["contract", "test", "--contract", str(RETAIL), "--corpus", str(corpus)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["triggered"] >= 1
    assert "triggered: 'Está por R$ 150,00 hoje.'" in captured.err


def test_the_contract_the_space_serves_with_the_company_corpus(
    mock_app: MockApp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_app.cell.features.add("claims")
    mock_app.cell.agent_features.claim_contract = ClaimContractSummary.model_validate(
        json.loads(RETAIL.read_text())
    )
    client = Niadra(
        "nia_sk_test",
        base_url="http://mock",
        http_client=httpx.Client(transport=httpx.WSGITransport(app=mock_app.wsgi)),
    )
    monkeypatch.setattr(command, "Niadra", lambda: client)
    assert command.main(["contract", "test", "--corpus", str(RETAIL)]) == 0
    assert json.loads(capsys.readouterr().out)["contract"] == "2026-09-29.1"
    assert command.main(["contract", "test"]) == 2, "the profile never carries the corpus's phrases"


def test_nothing_to_read_is_an_error_not_a_pass(tmp_path: Path) -> None:
    broken = tmp_path / "contract.json"
    broken.write_text("{not json")
    assert command.main(["contract", "test", "--contract", str(broken)]) == 2
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps({"examples": [{"id": "x"}]}))
    assert command.main(["contract", "test", "--contract", str(RETAIL), "--examples", str(examples)]) == 2


def test_a_phrase_triggers_in_any_language_and_for_any_agent() -> None:
    contract = ClaimContractSummary.model_validate(json.loads((SPEC / "health-plan-sales.json").read_text()))
    report = check_contract(contract, ["O plano sai por R$ 499,90 por pessoa."])
    assert report.triggered and not report.passed
    assert {t["lang"] for t in report.triggered} <= set(contract.languages)
