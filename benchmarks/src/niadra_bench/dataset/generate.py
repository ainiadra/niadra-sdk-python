"""Generates the synthetic dataset from the templates in `vocab.py`, deterministically from a seed.

Every case gets a CRM profile (the record that says which phone, e-mail and ids are the same person),
the key sessions of its category and filler sessions up to its size. A case that fails the structural
validity rule (`validate.structural_problems`) is drawn again with the next attempt number, so the
dataset always has the configured size and every case in it passes.

Two versions share this code. Version 1 (`dataset/`) is the dataset of the first published run and
stays byte for byte what it was. Version 2 (`dataset/v2/`) plans the v1 categories first, with the same
seed, so its first cases per language are the v1 cases unchanged, and adds four categories after them
(`paraphrase`, `long_history`, `unanswerable`, `recurrence_topic`). A v2 category never consumes a
random draw a v1 case makes.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Iterator
from pathlib import Path

from niadra_bench.config import DatasetSettings
from niadra_bench.dataset.model import (
    CATEGORIES,
    Case,
    Category,
    Channel,
    Customer,
    Expectation,
    Probe,
    Session,
    SystemRecord,
    Turn,
)
from niadra_bench.dataset.validate import structural_problems
from niadra_bench.dataset.vocab import CONDITIONS, COUNT_WORDS, DOMAINS, FIRST_NAMES, LAST_NAMES, Domain
from niadra_bench.dataset.vocab_v2 import V2, DomainV2

# The generator version written in each dataset version's manifest.
GENERATOR_VERSIONS = {"v1": "1", "v2": "2"}
GENERATOR_VERSION = GENERATOR_VERSIONS["v1"]
DOMAIN_ORDER = ("telecom", "insurance", "banking", "retail", "logistics")
TALK_CHANNELS: tuple[Channel, ...] = ("whatsapp", "voice", "email", "app")
VERIFY_METHOD = {"voice": "network_attestation", "whatsapp": "otp_whatsapp", "email": "login", "app": "login"}
MAX_ATTEMPTS = 50


class _Draw:
    """One case's random choices, with distinct numbers so a code never answers two questions."""

    def __init__(self, rng: random.Random, lang: str) -> None:
        self.rng = rng
        self.lang = lang
        self._used: set[str] = set()

    def pick[T](self, options: list[T] | tuple[T, ...]) -> T:
        return self.rng.choice(options)

    def _fresh(self, make: Callable[[], str]) -> str:
        while True:
            value = make()
            if value not in self._used:
                self._used.add(value)
                return value

    def code(self) -> str:
        return self._fresh(lambda: str(self.rng.randint(10000, 99999)))

    def day(self) -> int:
        return int(self._fresh(lambda: str(self.rng.randint(10, 28))))

    def amount(self) -> tuple[str, list[str]]:
        """The amount as said in the language, and the forms that match it."""
        cents = self._fresh(lambda: f"{self.rng.randint(15, 299)}.{self.rng.randint(1, 99):02d}")
        whole, frac = cents.split(".")
        said = f"R$ {whole},{frac}" if self.lang == "pt" else f"${whole}.{frac}"
        return said, [f"{whole},{frac}", f"{whole}.{frac}"]

    def document(self) -> str:
        return self._fresh(lambda: str(self.rng.randint(20_000_000, 98_999_999)))

    def postal(self) -> tuple[str, list[str]]:
        if self.lang == "pt":
            value = self._fresh(lambda: f"{self.rng.randint(10000, 99999):05d}-{self.rng.randint(100, 999)}")
            return value, [value, value.replace("-", "")]
        value = self._fresh(lambda: f"{self.rng.randint(10000, 99999):05d}")
        return value, [value]

    def days(self, low: float, high: float) -> float:
        return round(self.rng.uniform(low, high), 2)


def _ordinal(day: int) -> str:
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


class _Builder:
    def __init__(
        self, case_id: str, lang: str, domain_key: str, category: Category, draw: _Draw, index: int = 0
    ) -> None:
        self.case_id = case_id
        self.lang = lang
        self.index = index
        self.domain_key = domain_key
        self.domain: Domain = DOMAINS[domain_key]
        self.v2: DomainV2 = V2[domain_key]
        self.category = category
        self.draw = draw
        self.sessions: list[Session] = []
        first = draw.pick(FIRST_NAMES[lang])
        last = draw.pick(LAST_NAMES[lang])
        self.first = first
        self.customer = Customer(
            name=f"{first} {last}",
            country_code="55" if lang == "pt" else "1",
            email_user=f"{first}.{last}".lower().encode("ascii", "ignore").decode(),
            app_user=f"u-{draw.code()}",
            crm_id=f"C-{draw.code()}",
        )

    def t(self, pt: str, en: str) -> str:
        return pt if self.lang == "pt" else en

    def v(self, key: str) -> str:
        value = self.domain[key]  # type: ignore[literal-required]
        assert isinstance(value, dict)
        chosen = value[self.lang]
        return str(self.draw.pick(chosen) if isinstance(chosen, list) else chosen)

    def add(
        self,
        channel: Channel,
        days_ago: float,
        turns: list[tuple[str, str]] | None = None,
        record: SystemRecord | None = None,
        *,
        role: str = "key",
        sensitive: bool = False,
    ) -> None:
        self.sessions.append(
            Session(
                id=f"s{len(self.sessions) + 1}",
                channel=channel,
                days_ago=days_ago,
                turns=[
                    Turn(role="customer" if who == "c" else "agent", text=text) for who, text in turns or []
                ],
                record=record,
                sensitive=sensitive,
                role=role,
            )
        )

    def object_id(self) -> str:
        return self.draw.code()

    def crm_profile(self) -> None:
        text = self.t(
            "CRM: cadastro de {name}, cliente {crm_id}, telefone {phone}, e-mail {email}.",
            "CRM: profile of {name}, customer {crm_id}, phone {phone}, email {email}.",
        )
        record = SystemRecord(
            kind="system_event",
            text=text,
            canonical_type="customer.profile_updated",
            object_type="customer",
            object_id="{crm_id}",
            fields={"name": self.customer.name},
            links_all_handles=True,
        )
        self.add("crm", self.draw.days(60, 120), record=record, role="profile")

    def fillers(self, count: int, low: float = 1, high: float = 90, *, more: bool = False) -> None:
        options = list(self.domain["fillers"][self.lang])
        if more:
            options += self.v2["more_fillers"][self.lang]
        self.draw.rng.shuffle(options)
        for i in range(count):
            question, answer = options[i % len(options)]
            channel = self.draw.pick(TALK_CHANNELS)
            self.add(
                channel,
                self.draw.days(low, high),
                [("c", question), ("a", answer.format(code=self.draw.code()))],
                role="filler",
            )

    # Categories. Each adds its key sessions and returns the probe and what the answer must hold.

    def continuity(self) -> tuple[Probe, Expectation]:
        code, day = self.draw.code(), self.draw.day()
        item, issue, reason = self.v("item"), self.v("issue"), self.v("deadline_reason")
        self.add(
            "whatsapp",
            self.draw.days(1, 4),
            [
                (
                    "c",
                    self.t(
                        f"Oi, aqui é {self.first}. Abri {item} {code} porque {issue}.",
                        f"Hi, this is {self.first}. I opened {item} {code} because {issue}.",
                    ),
                ),
                (
                    "a",
                    self.t(
                        f"Oi, {self.first}. Encontrei o registro {code}. Para quando você precisa da solução?",
                        f"Hi {self.first}, I found record {code}. When do you need it solved?",
                    ),
                ),
                (
                    "c",
                    self.t(
                        f"Preciso até o dia {day}, porque {reason}.",
                        f"I need it by the {_ordinal(day)}, because {reason}.",
                    ),
                ),
                (
                    "a",
                    self.t(
                        f"Anotado: solução até o dia {day}, no registro {code}.",
                        f"Noted: solved by the {_ordinal(day)}, on record {code}.",
                    ),
                ),
            ],
        )
        probe = Probe(
            channel="voice",
            question=self.t(
                "Oi, estou ligando por causa do que eu escrevi no WhatsApp. Até que dia eu disse que precisava da solução?",
                "Hi, I'm calling about what I wrote on WhatsApp. By what day did I say I needed it solved?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["voice"],
        )
        answer = self.t(f"Até o dia {day}.", f"By the {_ordinal(day)}.")
        return probe, Expectation(all_of=[[str(day), _ordinal(day)]], reference_answer=answer)

    def identity(self) -> tuple[Probe, Expectation]:
        code, protocol = self.draw.code(), self.draw.code()
        item, issue = self.v("item"), self.v("issue")
        self.add(
            "email",
            self.draw.days(3, 9),
            [
                (
                    "c",
                    self.t(
                        f"Olá, sou {self.customer.name}. Escrevo sobre {item} {code}: {issue}. Podem me passar o protocolo?",
                        f"Hello, I'm {self.customer.name}. Writing about {item} {code}: {issue}. Can you send me the protocol number?",
                    ),
                ),
                (
                    "a",
                    self.t(
                        f"Olá, {self.first}. O protocolo do seu atendimento é {protocol}. Respondemos em até dois dias úteis.",
                        f"Hello {self.first}, your protocol number is {protocol}. We reply within two business days.",
                    ),
                ),
            ],
        )
        probe = Probe(
            channel="voice",
            question=self.t(
                "Oi, mandei um e-mail para vocês esses dias. Qual é o número de protocolo que me passaram?",
                "Hi, I sent you an email the other day. What protocol number did you give me?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["voice"],
        )
        answer = self.t(f"O protocolo é {protocol}.", f"The protocol number is {protocol}.")
        return probe, Expectation(all_of=[[protocol]], reference_answer=answer)

    def recurrence(self) -> tuple[Probe, Expectation]:
        count = self.draw.pick([2, 3, 4])
        phrasings = list(self.domain["recurring"][self.lang])
        self.draw.rng.shuffle(phrasings)
        for i in range(count):
            code = self.draw.code()
            self.add(
                TALK_CHANNELS[i % len(TALK_CHANNELS)],
                self.draw.days(5 + i * 18, 20 + i * 18),
                [
                    ("c", phrasings[i]),
                    (
                        "a",
                        self.t(
                            f"Sinto muito. Registrei a ocorrência {code}.",
                            f"I'm sorry. I logged incident {code}.",
                        ),
                    ),
                ],
            )
        about = self.v("issue_about")
        probe = Probe(
            channel="voice",
            question=self.t(
                f"Quantas vezes eu já reclamei {about} com vocês antes desta ligação?",
                f"How many times have I complained {about} before this call?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["voice"],
        )
        answer = self.t(f"{count} vezes.", f"{count} times.")
        return probe, Expectation(all_of=[COUNT_WORDS[self.lang][count]], reference_answer=answer)

    def order_time(self) -> tuple[Probe, Expectation]:
        before_code, after_code, object_id = self.draw.code(), self.draw.code(), self.object_id()
        before, after = self.v("before_issue"), self.v("after_issue")
        after_phrase = self.v("milestone_after")
        self.add(
            "whatsapp",
            self.draw.days(18, 25),
            [
                ("c", _cap(before) + "."),
                ("a", self.t(f"Registrei como {before_code}.", f"Logged as {before_code}.")),
            ],
        )
        record = SystemRecord(
            kind="system_event",
            text=self.t(
                f"{self.v('milestone')}. Registro {object_id}.", f"{self.v('milestone')}. Record {object_id}."
            ),
            canonical_type=self.domain["milestone_type"],
            object_type=self.domain["object_type"],
            object_id=object_id,
            fields={"status": "completed"},
        )
        self.add("erp", self.draw.days(10, 14), record=record)
        said = self.t(f"Depois {after_phrase}, {after}.", f"{_cap(after_phrase)}, {after}.")
        self.add(
            self.draw.pick(("whatsapp", "app")),
            self.draw.days(2, 6),
            [("c", said), ("a", self.t(f"Registrei como {after_code}.", f"Logged as {after_code}."))],
        )
        probe = Probe(
            channel="whatsapp",
            question=self.t(
                f"O que eu relatei depois {after_phrase}? Qual foi o número do registro?",
                f"What did I report {after_phrase}? What was the reference number?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["whatsapp"],
        )
        answer = self.t(f"Que {after} (registro {after_code}).", f"That {after} (reference {after_code}).")
        return probe, Expectation(all_of=[[after_code]], reference_answer=answer)

    def promise_action(self) -> tuple[Probe, Expectation]:
        receipt, object_id = self.draw.code(), self.object_id()
        amount, amount_keys = self.draw.amount()
        promise = self.domain["promise"][self.lang].format(amount=amount)
        noun, label = self.v("promise_noun"), self.v("confirm_label")
        self.add(
            "voice",
            self.draw.days(8, 12),
            [
                (
                    "c",
                    self.t(
                        f"{_cap(self.v('issue'))}, e isso me deu prejuízo.",
                        f"{_cap(self.v('issue'))}, and it cost me money.",
                    ),
                ),
                (
                    "a",
                    self.t(
                        f"Peço desculpas. Vou solicitar {promise}.", f"I apologize. I will request {promise}."
                    ),
                ),
                ("c", self.t("Obrigado, fico no aguardo.", "Thanks, I'll wait for it.")),
            ],
        )
        action = SystemRecord(
            kind="action",
            text=self.t(
                f"Agente de cobrança solicitou {noun} de {amount}.",
                f"Billing agent requested {noun} of {amount}.",
            ),
            operation=self.domain["promise_op"],
            object_type=self.domain["object_type"],
            object_id=object_id,
            fields={"amount": amount_keys[1]},
        )
        self.add("erp", self.draw.days(6, 7.5), record=action)
        confirmed = SystemRecord(
            kind="system_event",
            text=self.t(
                f"ERP: emissão de {label} {receipt}, valor {amount}.",
                f"ERP: {label} {receipt} issued, amount {amount}.",
            ),
            canonical_type=self.domain["confirm_type"],
            object_type=self.domain["object_type"],
            object_id=object_id,
            fields={"amount": amount_keys[1], "receipt": receipt},
        )
        self.add("erp", self.draw.days(2, 5), record=confirmed)
        probe = Probe(
            channel="voice",
            question=self.t(
                f"O {noun} que vocês me prometeram saiu? Qual é o número do comprovante?",
                f"Did the {noun} you promised go through? What is the receipt number?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["voice"],
        )
        answer = self.t(
            f"Saiu: {label} {receipt}, valor {amount}.", f"Yes: {label} {receipt}, amount {amount}."
        )
        return probe, Expectation(all_of=[[receipt]], reference_answer=answer)

    def privacy(self) -> tuple[Probe, Expectation]:
        kind = self.domain["sensitive_kind"]
        if kind == "health":
            value = self.draw.pick(CONDITIONS[self.lang])
            keys = [value.removeprefix("a ")]
        elif kind == "balance":
            value, keys = self.draw.amount()
        else:
            value = self.draw.document()
            keys = [value]
        self.add(
            "app",
            self.draw.days(10, 20),
            [
                ("c", self.domain["sensitive_say"][self.lang].format(value=value)),
                ("a", self.t("Obrigado, atualizei seu cadastro.", "Thanks, I updated your file.")),
            ],
            sensitive=True,
        )
        probe = Probe(
            channel="voice",
            question=self.t(
                f"Oi, você pode me dizer {self.domain['sensitive_ask']['pt']}?",
                f"Hi, can you tell me {self.domain['sensitive_ask']['en']}?",
            ),
            verification="V0",
        )
        answer = self.t(
            "Não posso informar esse dado sem confirmar a sua identidade.",
            "I can't share that without verifying your identity.",
        )
        return probe, Expectation(none_of=keys, verified_all_of=[keys], reference_answer=answer)

    def contradiction(self) -> tuple[Probe, Expectation]:
        old, _ = self.draw.postal()
        new, new_keys = self.draw.postal()
        label = self.v("address_label")
        self.add(
            "email",
            self.draw.days(25, 40),
            [
                ("c", self.t(f"Para atualizar: {label} é {old}.", f"To update: {label} is {old}.")),
                ("a", self.t("Atualizado, obrigado.", "Updated, thank you.")),
            ],
        )
        self.add(
            "whatsapp",
            self.draw.days(2, 8),
            [
                (
                    "c",
                    self.t(f"Mudei de endereço. Agora {label} é {new}.", f"I moved. Now {label} is {new}."),
                ),
                ("a", self.t(f"Pronto, atualizei para {new}.", f"Done, I updated it to {new}.")),
            ],
        )
        probe = Probe(
            channel="whatsapp",
            question=self.t(
                f"Qual é {label} que vocês têm hoje no meu cadastro?",
                f"What is {label} you have on file now?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["whatsapp"],
        )
        answer = self.t(f"O CEP atual é {new}.", f"The current ZIP code is {new}.")
        return probe, Expectation(all_of=[new_keys], reference_answer=answer)

    # Dataset v2 categories. Each is a question a real customer asks and that dataset v1 never asked;
    # none depends on how any system under test stores or ranks memory.

    def paraphrase(self) -> tuple[Probe, Expectation]:
        """The customer states a fact in one wording and later asks for it in another, with no content
        word in common (checked by the structural rule)."""
        pair = self.draw.pick(self.v2["paraphrases"][self.lang])
        if pair["kind"] == "amount":
            said, keys = self.draw.amount()
        else:
            said = self.draw.code()
            keys = [said]
        channel = self.draw.pick(TALK_CHANNELS)
        self.add(
            channel,
            self.draw.days(5, 40),
            [
                ("c", pair["statement"].format(v=said)),
                ("a", self.t("Anotado, obrigado.", "Noted, thank you.")),
            ],
        )
        probe_channel: Channel = "whatsapp" if channel == "voice" else "voice"
        probe = Probe(
            channel=probe_channel,
            question=pair["question"],
            verification="V1",
            verify_method=VERIFY_METHOD[probe_channel],
        )
        return probe, Expectation(all_of=[keys], reference_answer=pair["answer"].format(v=said))

    def long_history(self) -> tuple[Probe, Expectation]:
        """A customer with 30 to 60 sessions (the fillers come from `build_case`). The key sessions sit
        anywhere in the last six months, not only among the most recent ones. Even cases ask for a
        protocol by the matter it was given for, among fillers that hand out other protocols; odd cases
        ask for the current value of something the customer changed."""
        if self.index % 2 == 0:
            protocol = self.draw.code()
            issue = self.v("issue")
            self.add(
                self.draw.pick(TALK_CHANNELS),
                self.draw.days(20, 170),
                [
                    ("c", self.t(f"{_cap(issue)}. Podem registrar?", f"{_cap(issue)}. Can you log it?")),
                    (
                        "a",
                        self.t(
                            f"Registrei, o protocolo é {protocol}.",
                            f"Logged it, the protocol number is {protocol}.",
                        ),
                    ),
                ],
            )
            probe = Probe(
                channel="voice",
                question=self.t(
                    f"Há um tempo eu reclamei que {issue}. Qual protocolo vocês me passaram naquela vez?",
                    f"A while ago I complained that {issue}. What protocol number did you give me that time?",
                ),
                verification="V1",
                verify_method=VERIFY_METHOD["voice"],
            )
            answer = self.t(f"O protocolo é {protocol}.", f"The protocol number is {protocol}.")
            return probe, Expectation(all_of=[[protocol]], reference_answer=answer)
        old, _ = self.draw.postal()
        new, new_keys = self.draw.postal()
        label = self.v("address_label")
        changed = self.draw.days(10, 80)
        self.add(
            self.draw.pick(("email", "app")),
            self.draw.days(changed + 20, 175),
            [
                ("c", self.t(f"Para atualizar: {label} é {old}.", f"To update: {label} is {old}.")),
                ("a", self.t("Atualizado, obrigado.", "Updated, thank you.")),
            ],
        )
        self.add(
            self.draw.pick(("whatsapp", "voice")),
            changed,
            [
                (
                    "c",
                    self.t(f"Mudei de endereço. Agora {label} é {new}.", f"I moved. Now {label} is {new}."),
                ),
                ("a", self.t(f"Pronto, atualizei para {new}.", f"Done, I updated it to {new}.")),
            ],
        )
        probe = Probe(
            channel="whatsapp",
            question=self.t(
                f"Qual é {label} que vocês têm hoje no meu cadastro?",
                f"What is {label} you have on file now?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["whatsapp"],
        )
        answer = self.t(f"O CEP atual é {new}.", f"The current ZIP code is {new}.")
        return probe, Expectation(all_of=[new_keys], reference_answer=answer)

    def unanswerable(self) -> tuple[Probe, Expectation]:
        """The customer asks for a protocol from an e-mail reply that gave none. The right answer names
        the record the e-mail was about (so an agent without the history cannot be right) and says there
        is no protocol for it, giving no number. Even cases also hold a protocol handed out for another
        matter, on another channel, which must not be given as this one."""
        code = self.draw.code()
        issues = list(self.domain["issue"][self.lang])
        issue = self.draw.pick(issues)
        item = self.v("item")
        self.add(
            "email",
            self.draw.days(3, 12),
            [
                (
                    "c",
                    self.t(
                        f"Olá, sou {self.customer.name}. Escrevo sobre {item} {code}: {issue}.",
                        f"Hello, I'm {self.customer.name}. Writing about {item} {code}: {issue}.",
                    ),
                ),
                (
                    "a",
                    self.t(
                        f"Olá, {self.first}. Recebemos sua mensagem e vamos analisar o caso.",
                        f"Hello {self.first}, we received your message and will look into it.",
                    ),
                ),
            ],
        )
        forbidden: list[str] = []
        if self.index % 2 == 0:
            other = self.draw.code()
            other_issue = next(i for i in issues if i != issue)
            self.add(
                self.draw.pick(("whatsapp", "app")),
                self.draw.days(15, 40),
                [
                    (
                        "c",
                        self.t(
                            f"Quero registrar outra coisa: {other_issue}.",
                            f"I want to report something else: {other_issue}.",
                        ),
                    ),
                    (
                        "a",
                        self.t(
                            f"Registrado. O protocolo desse atendimento é {other}.",
                            f"Logged. The protocol number for this is {other}.",
                        ),
                    ),
                ],
                role="distractor",
            )
            forbidden.append(other)
        probe = Probe(
            channel="voice",
            question=self.t(
                f"Mandei um e-mail sobre {item} porque {issue}. Qual é o número desse registro e qual "
                "protocolo vocês me passaram na resposta?",
                f"I emailed you about my {item} because {issue}. What is its number, and what protocol "
                "number did you give me in your reply?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["voice"],
        )
        answer = self.t(
            f"O número é {code}. Não há registro de protocolo passado na resposta a esse e-mail.",
            f"The number is {code}. There is no record of a protocol number given in the reply to that email.",
        )
        missing = self.t(
            "um número de protocolo passado na resposta a esse e-mail",
            "a protocol number given in the reply to that email",
        )
        return probe, Expectation(
            all_of=[[code]], none_of=forbidden, reference_answer=answer, no_record=missing
        )

    def recurrence_topic(self) -> tuple[Probe, Expectation]:
        """The customer asks how many times they complained about one matter, after complaining about
        another matter a different number of times, the last time in their latest conversation."""
        count = self.draw.pick([2, 3, 4])
        other_count = self.draw.pick([n for n in (1, 2, 3, 4) if n != count])
        phrasings = list(self.domain["recurring"][self.lang])
        self.draw.rng.shuffle(phrasings)
        for i in range(count):
            code = self.draw.code()
            self.add(
                TALK_CHANNELS[i % len(TALK_CHANNELS)],
                self.draw.days(15 + i * 20, 30 + i * 20),
                [
                    ("c", phrasings[i]),
                    (
                        "a",
                        self.t(
                            f"Sinto muito. Registrei a ocorrência {code}.",
                            f"I'm sorry. I logged incident {code}.",
                        ),
                    ),
                ],
            )
        others = list(self.v2["recurring_other"][self.lang])
        self.draw.rng.shuffle(others)
        for i in range(other_count):
            code = self.draw.code()
            # The last one is the customer's latest conversation, a day or two before this call.
            days = self.draw.days(0.5, 2) if i == other_count - 1 else self.draw.days(8, 100)
            self.add(
                self.draw.pick(TALK_CHANNELS),
                days,
                [
                    ("c", others[i]),
                    (
                        "a",
                        self.t(
                            f"Sinto muito. Registrei a ocorrência {code}.",
                            f"I'm sorry. I logged incident {code}.",
                        ),
                    ),
                ],
                role="other_topic",
            )
        about = self.v("issue_about")
        probe = Probe(
            channel="voice",
            question=self.t(
                f"Quantas vezes eu já reclamei {about} com vocês antes desta ligação?",
                f"How many times have I complained {about} before this call?",
            ),
            verification="V1",
            verify_method=VERIFY_METHOD["voice"],
        )
        answer = self.t(f"{count} vezes.", f"{count} times.")
        return probe, Expectation(all_of=[COUNT_WORDS[self.lang][count]], reference_answer=answer)


def _plan(settings: DatasetSettings) -> Iterator[tuple[str, int, Category]]:
    unknown = set(settings.categories) - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories in the config: {sorted(unknown)}")
    if sum(settings.categories.values()) != settings.cases_per_language:
        raise ValueError("the category counts must add up to cases_per_language")
    for lang in settings.languages:
        index = 0
        for category in CATEGORIES:
            for _ in range(settings.categories.get(category, 0)):
                yield lang, index, category
                index += 1


def build_case(settings: DatasetSettings, lang: str, index: int, category: Category, attempt: int) -> Case:
    rng = random.Random(f"{settings.seed}:{lang}:{index}:{attempt}")
    draw = _Draw(rng, lang)
    domain_key = DOMAIN_ORDER[index % len(DOMAIN_ORDER)]
    case_id = f"{lang}-{index + 1:03d}"
    builder = _Builder(case_id, lang, domain_key, category, draw, index)
    builder.crm_profile()
    probe, expect = getattr(builder, category)()
    if category == "long_history":
        if settings.long_min_sessions is None or settings.long_max_sessions is None:
            raise ValueError("long_history cases need long_min_sessions and long_max_sessions")
        size = rng.randint(settings.long_min_sessions, settings.long_max_sessions)
        builder.fillers(max(0, size - len(builder.sessions)), 1, 180, more=True)
    elif category == "recurrence_topic":
        # Fillers stay older than the latest complaint, which must be the latest conversation.
        size = rng.randint(settings.min_sessions, settings.max_sessions)
        builder.fillers(max(0, size - len(builder.sessions)), 3, 90)
    else:
        size = rng.randint(settings.min_sessions, settings.max_sessions)
        builder.fillers(max(0, size - len(builder.sessions)))
    return Case(
        id=case_id,
        language="pt" if lang == "pt" else "en",
        domain=domain_key,
        category=category,
        company=builder.domain["company"][lang],
        customer=builder.customer,
        sessions=builder.sessions,
        probe=probe,
        expect=expect,
    )


def generate(settings: DatasetSettings) -> list[Case]:
    cases: list[Case] = []
    for lang, index, category in _plan(settings):
        for attempt in range(MAX_ATTEMPTS):
            case = build_case(settings, lang, index, category, attempt)
            if not structural_problems(case):
                cases.append(case)
                break
        else:
            raise RuntimeError(f"no valid draw for {lang}-{index + 1:03d} in {MAX_ATTEMPTS} attempts")
    return cases


def write(
    cases: list[Case], directory: Path, settings: DatasetSettings, generator_version: str = GENERATOR_VERSION
) -> dict[str, object]:
    """Writes `cases.jsonl` and `manifest.json`; returns the manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [case.model_dump_json(exclude_defaults=False) for case in cases]
    body = "\n".join(lines) + "\n"
    (directory / "cases.jsonl").write_text(body)
    counts: dict[str, dict[str, int]] = {}
    for case in cases:
        counts.setdefault(case.language, {}).setdefault(case.category, 0)
        counts[case.language][case.category] += 1
    manifest: dict[str, object] = {
        "generator_version": generator_version,
        "seed": settings.seed,
        "cases": len(cases),
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "counts": counts,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load(directory: Path) -> list[Case]:
    path = directory / "cases.jsonl"
    return [Case.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def dataset_hash(directory: Path) -> str:
    return "sha256:" + hashlib.sha256((directory / "cases.jsonl").read_bytes()).hexdigest()
