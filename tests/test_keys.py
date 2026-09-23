from __future__ import annotations

import logging

import pytest
import respx

from niadra import ApiKey, ConfigurationError, Niadra
from tests.conftest import BASE, KEY, batch_ok


def test_parses_every_part_of_a_key() -> None:
    key = ApiKey.parse("nia_sk_test_sa-east-1_acme-corp_K7q2_abc_def")
    assert (key.environment, key.region, key.space, key.key_id) == ("test", "sa-east-1", "acme-corp", "K7q2")
    assert key.secret == "abc_def"


def test_derives_the_base_url_from_space_and_region() -> None:
    assert ApiKey.parse(KEY).base_url == BASE


def test_repr_hides_the_secret() -> None:
    key = ApiKey.parse(KEY)
    assert "s3cret" not in repr(key)
    assert KEY not in repr(key)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sk_live_br1_acme_k1_secret",
        "nia_sk_prod_br1_acme_k1_secret",
        "nia_sk_live_BR1_acme_k1_secret",
        "nia_sk_live_br1_acme.corp_k1_secret",
        "nia_sk_live_br1_acme_k1_",
        "nia_sk_live_br1_acme_k-1_secret",
    ],
)
def test_rejects_malformed_keys_without_echoing_them(raw: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        ApiKey.parse(raw)
    assert raw == "" or raw not in str(caught.value)


def test_client_uses_the_derived_address(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, channel="chat")
    niadra.track(
        {
            "speaker": {"role": "customer"},
            "handles": [{"type": "email", "value": "a@b.co"}],
            "content": {"text": "hi"},
        }
    )
    assert niadra.flush()
    assert route.called
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    niadra.close()


def test_base_url_overrides_the_derived_address(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("http://127.0.0.1:8765/v1/batch").respond(200, json=batch_ok())
    niadra = Niadra(KEY, base_url="http://127.0.0.1:8765/", channel="chat")
    niadra.track(
        {
            "speaker": {"role": "customer"},
            "handles": [{"type": "email", "value": "a@b.co"}],
            "content": {"text": "hi"},
        }
    )
    niadra.flush()
    assert route.called
    niadra.close()


def test_reads_key_and_address_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIADRA_API_KEY", KEY)
    monkeypatch.setenv("NIADRA_BASE_URL", "http://localhost:9000")
    niadra = Niadra()
    assert niadra.enabled
    assert niadra.base_url == "http://localhost:9000"
    assert niadra.mcp_url == "http://localhost:9000/mcp"
    niadra.close()


def test_a_custom_base_url_accepts_any_key_shape() -> None:
    niadra = Niadra("nia_sk_local", base_url="http://localhost:8765")
    assert niadra.enabled
    niadra.close()


def test_a_malformed_key_disables_the_client(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="niadra")
    niadra = Niadra("not-a-key")
    assert not niadra.enabled
    assert "not-a-key" not in caplog.text


def test_strict_mode_refuses_a_missing_or_malformed_key() -> None:
    with pytest.raises(ConfigurationError):
        Niadra(strict=True)
    with pytest.raises(ConfigurationError):
        Niadra("not-a-key", strict=True)
