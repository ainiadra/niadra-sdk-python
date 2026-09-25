"""The customer's identifiers for one run and repetition, and the user ids a Mem0 application would send.

Every repetition is a new person for every system: phone digits, e-mail, CRM and app ids all derive
from the case id and a run tag, so no memory has seen them before.

Mem0 does not resolve identity, so the benchmark gives it two scenarios (plan, B.2):
- `known_id`: the application already knows who the customer is and sends one user id everywhere, the
  best case for Mem0;
- `per_channel_id`: each channel sends its own identifier (the WhatsApp id, the phone number, the
  e-mail, the CRM id), which is what happens when the agents come from different vendors.
Niadra receives the same handles in both scenarios, as its SDK is meant to be used.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from niadra import Handle, app_user, email, phone, system_id, whatsapp

from niadra_bench.dataset.model import CHANNEL_HANDLE, Case, HandleKind

Scenario = Literal["known_id", "per_channel_id"]
SCENARIOS: tuple[Scenario, ...] = ("known_id", "per_channel_id")
EMAIL_DOMAIN = "bench.niadra.com"


def _digits(seed: str, count: int) -> str:
    raw = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return str(raw)[-count:].rjust(count, "0")


def _subscriber(seed: str) -> str:
    """Eight digits that are neither one repeated digit nor a sequence (the server blocks those)."""
    for attempt in range(100):
        digits = _digits(f"{seed}:{attempt}", 8)
        if len(set(digits)) > 2 and not digits.startswith("1234567"):
            return digits
    raise RuntimeError("no usable subscriber number")


@dataclass(frozen=True)
class Identities:
    case_id: str
    tag: str
    phone: str
    email: str
    app_user: str
    crm_id: str

    @classmethod
    def for_case(cls, case: Case, tag: str) -> Identities:
        seed = f"{case.id}:{tag}"
        if case.customer.country_code == "55":
            number = f"+55119{_subscriber(seed)}"
        else:
            area = 200 + int(_digits(seed + ":area", 3)) % 700
            number = f"+1{area}5{_subscriber(seed)[1:]}"
        short = hashlib.sha256(seed.encode()).hexdigest()[:6]
        return cls(
            case_id=case.id,
            tag=tag,
            phone=number,
            email=f"{case.customer.email_user}.{short}@{EMAIL_DOMAIN}",
            app_user=f"{case.customer.app_user}-{short}",
            crm_id=f"{case.customer.crm_id}-{short}",
        )

    def value(self, kind: HandleKind) -> str:
        match kind:
            case "phone":
                return self.phone
            case "wa_id":
                return self.phone.removeprefix("+")
            case "email":
                return self.email
            case "app_user":
                return self.app_user
            case "crm_id":
                return self.crm_id

    def handle(self, kind: HandleKind) -> Handle:
        match kind:
            case "phone":
                return phone(self.phone)
            case "wa_id":
                return whatsapp(self.value("wa_id"))
            case "email":
                return email(self.email)
            case "app_user":
                return app_user(self.app_user)
            case "crm_id":
                return system_id("crm", self.crm_id)

    def channel_handle(self, channel: str) -> Handle:
        return self.handle(CHANNEL_HANDLE[channel])

    def all_handles(self) -> list[Handle]:
        """What a CRM profile names: phone, e-mail, CRM id and app login."""
        return [self.handle("phone"), self.handle("email"), self.handle("crm_id"), self.handle("app_user")]

    def mem0_user(self, scenario: Scenario, channel: str) -> str:
        if scenario == "known_id":
            return f"cust-{self.case_id}-{self.tag}"
        kind = CHANNEL_HANDLE[channel]
        return f"{kind}:{self.value(kind)}"

    def fill(self, text: str, name: str) -> str:
        """Fills the placeholders of a CRM profile record."""
        return (
            text.replace("{name}", name)
            .replace("{crm_id}", self.crm_id)
            .replace("{phone}", self.phone)
            .replace("{email}", self.email)
        )
