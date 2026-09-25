from niadra import phone

from niadra_bench.identity import EMAIL_DOMAIN, Identities


def test_every_repetition_is_a_new_person_with_valid_handles(cases) -> None:
    seen: set[str] = set()
    for case in cases:
        for tag in ("aaaaaar1", "aaaaaar2"):
            ids = Identities.for_case(case, tag)
            assert phone(ids.phone).value == ids.phone
            assert ids.email.endswith("@" + EMAIL_DOMAIN)
            subscriber = ids.phone[-8:]
            assert len(set(subscriber)) > 2 and not subscriber.startswith("1234567")
            assert ids.phone not in seen
            seen.add(ids.phone)


def test_mem0_user_ids_per_scenario(cases) -> None:
    ids = Identities.for_case(cases[0], "t1")
    assert ids.mem0_user("known_id", "whatsapp") == ids.mem0_user("known_id", "voice")
    assert ids.mem0_user("per_channel_id", "whatsapp") != ids.mem0_user("per_channel_id", "voice")
    assert ids.mem0_user("per_channel_id", "crm") == f"crm_id:{ids.crm_id}"
