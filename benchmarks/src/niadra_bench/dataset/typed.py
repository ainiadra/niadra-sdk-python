"""The typed-object set: cases whose answer depends on what the company's systems say about an object now
(a quote, a court notice, a plan's price), on a constraint the customer declared to another agent, or on an
effect another agent already carried out.

Dataset v2 has no typed object and never asks for the blocks a read adds with `include`, so it cannot say
what those blocks do to an agent's answers. Each case here is one sector's agent, in Portuguese and in
English, with synthetic data only:

| Category | Sector | What changed before the question | The right answer |
|---|---|---|---|
| `price_freshness` | retail | the checkout computed the cart's total 30 to 90 minutes ago | not confirming that total as current |
| `quote_expiry` | retail | the customer changed the delivery postal code after the shipping quote | the quote no longer holds |
| `deadline_revision` | legal | the court's calendar moved a deadline after the agent had given it | the revised deadline |
| `not_checked` | legal | nobody has checked whether a notice is addressed to the client | not checked, never "no" |
| `changes_since_seen` | health plan sales | a plan's price moved after the customer was shown it | the new price |
| `hard_constraint` | health plan sales | another agent's quote tool showed two plans, and the customer set a filter in its simulator | the option that respects it |
| `effect_once` | retail | the closing agent already sent the farewell with the survey | already sent, not again |
| `composite` | retail | the stylist showed a look; since then the platform marked some of its pieces unavailable, or none | whether every piece is available now |

A case is a script of writes (`Step`) before one question. Times are minutes before the question and dates
are days after it (`{date:N}`), so every run seeds a history that ends just before it asks. Object ids carry
`{tag}`, filled per run and repetition, so no memory has seen them. Each case also carries one sensitive
value in a field its type marks `pii` (`sensitive`), which no read at V0 may hand the agent.

Each case's ground truth is checkable twice: by the judge, with the case's own rule (`judge_rule`) beside
the reference answer, and by an exact check with no model (`expect.all_of` and `expect.none_of`, whole
tokens). The object types the steps write are declared in `config/typed.object-types.json`.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from niadra_bench.dataset.model import Channel, Expectation, Level

GENERATOR_VERSION = "typed-3"
TypedCategory = Literal[
    "price_freshness",
    "quote_expiry",
    "deadline_revision",
    "not_checked",
    "changes_since_seen",
    "hard_constraint",
    "effect_once",
    "composite",
]
TYPED_CATEGORIES: tuple[TypedCategory, ...] = (
    "price_freshness",
    "quote_expiry",
    "deadline_revision",
    "not_checked",
    "changes_since_seen",
    "hard_constraint",
    "effect_once",
    "composite",
)
Sector = Literal["retail", "legal", "health_plan_sales"]
VERIFY_METHOD = {"voice": "network_attestation", "whatsapp": "otp_whatsapp"}
_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)  # fmt: skip
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Ref(_Model):
    type: str
    namespace: str
    id: str = Field(description="Carries `{tag}`, filled per run and repetition.")


class Item(_Model):
    """One object an agent's tool saw and showed, with the fields shown."""

    ref: Ref
    fields: dict[str, Any] = Field(default_factory=dict)


class Message(_Model):
    role: Literal["customer", "agent"]
    text: str


class Preference(_Model):
    """A constraint the customer set, as a tool's arguments carry it (a simulator's filter)."""

    attr: str
    op: Literal["eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte"]
    values: list[Any]
    said: str = Field(description="What the customer set, in the case's language, for the full history.")


class Step(_Model):
    """One write before the question, `minutes_ago` minutes before it.

    - `conversation`: messages on a channel, then the conversation's end;
    - `record`: a system event about `ref` from the company's system, with `fields`;
    - `present`: an agent's turn in `conversation` whose tool (`tool`) saw the shared objects of `items`
      and showed them to the customer, in that order, as one list;
    - `preference`: an agent's turn in `conversation` whose tool's arguments carry the customer's filter;
    - `push`: the platform pushes `fields` of `ref` at `version`: a shared object's new values, or a
      customer's derived object bound to the objects its `inputs` are fields of;
    - `effect`: `agent` carries out the effect `effect` of `conversation`, checked and settled `done`.
    """

    kind: Literal["conversation", "record", "present", "preference", "push", "effect"]
    minutes_ago: float = Field(gt=0)
    conversation: str | None = None
    channel: Channel = "whatsapp"
    agent: str | None = None
    messages: list[Message] = Field(default_factory=list)
    ref: Ref | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    items: list[Item] = Field(default_factory=list)
    version: int | None = None
    inputs: dict[str, str] | None = None
    tool: str | None = None
    preference: Preference | None = None
    effect: str | None = None


class TypedProbe(_Model):
    channel: Channel
    agent: str
    question: str
    verification: Level = "V1"
    verify_method: str | None = None


class TypedCase(_Model):
    id: str
    language: Literal["pt", "en"]
    sector: Sector
    category: TypedCategory
    company: str
    steps: list[Step]
    probe: TypedProbe
    expect: Expectation
    judge_rule: str = Field(description="What makes an answer right, for the judge, in English.")
    sensitive: str = Field(description="A value of a `pii` field: no read at V0 may hand it to the agent.")


# --- Placeholders -------------------------------------------------------------------------------------


def date_on(days: int, today: date) -> date:
    return today + timedelta(days=days)


def say_date(day: date, lang: str) -> str:
    return day.strftime("%d/%m/%Y") if lang == "pt" else f"{_MONTHS[day.month - 1]} {day.day}, {day.year}"


def date_forms(day: date, lang: str) -> list[str]:
    """The ways an answer may write the day, for the exact check."""
    month = _MONTHS[day.month - 1]
    if lang == "pt":
        names = _MESES
        return [
            day.strftime("%d/%m/%Y"),
            day.strftime("%d/%m"),
            f"{day.day}/{day.month}",
            f"{day.day} de {names[day.month - 1]}",
            day.isoformat(),
        ]
    return [
        f"{month} {day.day}",
        f"{month[:3]} {day.day}",
        f"{day.day} {month}",
        day.isoformat(),
        f"{day.month}/{day.day}",
    ]


def fill(text: str, lang: str, today: date, tag: str) -> str:
    """`{date:N}` as the language writes the day N days after `today`, `{tag}` as the run's tag."""
    out = text.replace("{tag}", tag)
    while "{date:" in out:
        start = out.index("{date:")
        end = out.index("}", start)
        days = int(out[start + 6 : end])
        out = out[:start] + say_date(date_on(days, today), lang) + out[end + 1 :]
    return out


def fill_value(value: Any, lang: str, today: date, tag: str) -> Any:
    """A field value with its placeholders filled; `{iso:N}` is the day N days after `today`, ISO."""
    if isinstance(value, str):
        if value.startswith("{iso:") and value.endswith("}"):
            return date_on(int(value[5:-1]), today).isoformat()
        return fill(value, lang, today, tag)
    if isinstance(value, dict):
        return {k: fill_value(v, lang, today, tag) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_value(v, lang, today, tag) for v in value]
    return value


def expectation(case: TypedCase, today: date, tag: str) -> Expectation:
    """The case's expectation for a run: dates written every way an answer may write them."""

    def forms(alternative: str) -> list[str]:
        if alternative.startswith("{date:") and alternative.endswith("}"):
            return date_forms(date_on(int(alternative[6:-1]), today), case.language)
        return [fill(alternative, case.language, today, tag)]

    return Expectation(
        all_of=[[f for alt in group for f in forms(alt)] for group in case.expect.all_of],
        none_of=[f for alt in case.expect.none_of for f in forms(alt)],
        reference_answer=fill(case.expect.reference_answer, case.language, today, tag),
    )


def render_history(case: TypedCase, today: date, tag: str) -> str:
    """Every write in order as plain lines: the full-history reference of the validity rule."""
    lines: list[str] = []
    for step in sorted(case.steps, key=lambda s: -s.minutes_ago):
        when = _ago(step.minutes_ago)
        ref = f"{step.ref.type} {fill(step.ref.id, case.language, today, tag)}" if step.ref else ""
        values = json.dumps(fill_value(step.fields, case.language, today, tag), ensure_ascii=False)
        match step.kind:
            case "conversation":
                lines.append(f"[{when}, {step.channel}, conversation with the {step.agent} agent]")
                lines += [
                    f"{'customer' if m.role == 'customer' else 'agent'}: {fill(m.text, case.language, today, tag)}"
                    for m in step.messages
                ]
            case "record":
                lines.append(f"[{when}, company system] {ref}: {values}")
            case "present":
                shown = "; ".join(
                    f"{item.ref.type} {fill(item.ref.id, case.language, today, tag)} "
                    + json.dumps(fill_value(item.fields, case.language, today, tag), ensure_ascii=False)
                    for item in step.items
                )
                lines.append(
                    f"[{when}, tool {step.tool} of the {step.agent} agent] shown to the customer: {shown}"
                )
            case "preference":
                assert step.preference is not None
                lines.append(f"[{when}, simulator used with the {step.agent} agent] {step.preference.said}")
            case "push":
                bound = (
                    f" (computed from {fill(json.dumps(step.inputs), case.language, today, tag)})"
                    if step.inputs
                    else ""
                )
                lines.append(f"[{when}, platform update] {ref}: {values}{bound}")
            case "effect":
                lines.append(f"[{when}, {step.agent} agent] sent: {step.effect}")
    return "\n".join(lines)


def _ago(minutes: float) -> str:
    if minutes >= 1440:
        return f"{minutes / 1440:.0f} days ago"
    if minutes >= 120:
        return f"{minutes / 60:.0f} hours ago"
    return f"{minutes:.0f} minutes ago"


# --- The cases ----------------------------------------------------------------------------------------

_COMPANY = {
    ("retail", "pt"): "Aurora Moda",
    ("retail", "en"): "Aurora Apparel",
    ("legal", "pt"): "Bastos & Reis Advocacia",
    ("legal", "en"): "Bastos & Reis Law",
    ("health_plan_sales", "pt"): "Vida Plena Corretora",
    ("health_plan_sales", "en"): "Vida Plena Health Plans",
}
_SECTOR: dict[TypedCategory, Sector] = {
    "price_freshness": "retail",
    "quote_expiry": "retail",
    "deadline_revision": "legal",
    "not_checked": "legal",
    "changes_since_seen": "health_plan_sales",
    "hard_constraint": "health_plan_sales",
    "effect_once": "retail",
    "composite": "retail",
}
_RECHECK = {
    "pt": ["confirmar", "confirmo", "confirmando", "confirme", "verificar", "verifico", "verificando", "conferir",
           "confiro", "conferindo", "checar", "consultar", "recalcular", "atualizar"],
    "en": ["confirm", "confirming", "check", "checking", "verify", "verifying", "recalculate", "refresh"],
}  # fmt: skip
_EXPIRED = {
    "pt": ["expirou", "expirada", "expirado", "venceu", "vencida", "vencido", "nao vale", "nao vale mais",
           "nao e mais valido", "nao e mais valida", "recalcular", "nova cotacao", "novo calculo", "refazer"],
    "en": ["expired", "no longer valid", "not valid", "recalculate", "new quote", "requote", "no longer applies"],
}  # fmt: skip
_NOT_CHECKED = {
    "pt": ["conferido", "conferida", "conferir", "conferindo", "confirmado", "confirmada", "confirmar",
           "verificado", "verificada", "verificar", "verificando", "checado", "checada", "analise", "analisando"],
    "en": ["checked", "check", "checking", "confirmed", "confirm", "verified", "verify", "verifying", "reviewing"],
}  # fmt: skip
_NOT_FOR_YOU = {
    "pt": ["pode ignorar", "nao e para voce", "nao e com voce", "nao e sua", "nao se refere a voce"],
    "en": ["can ignore", "not for you", "not addressed to you", "is not yours", "not about you"],
}
_ALREADY = {"pt": ["ja"], "en": ["already"]}
_SENT = {
    "pt": [
        "enviada",
        "enviado",
        "enviamos",
        "enviei",
        "mandamos",
        "mandei",
        "mandada",
        "encaminhada",
        "saiu",
    ],
    "en": ["sent", "delivered", "went out"],
}


def _price_freshness(lang: str, n: int) -> dict[str, Any]:
    items, total, shipping, ago, name, postal, address = [
        ("o vestido midi de linho e a sandália de couro", "412,70", "19,90", 45, "Ana", "04567-000", "Rua das Acácias, 118, apto 42"),
        ("a calça wide leg e o cinto de couro", "238,40", "14,90", 90, "Bruna", "22041-001", "Rua Barata Ribeiro, 77, casa 2"),
        ("a blusa de tricô", "159,90", "12,50", 30, "Camila", "30130-010", "Avenida Afonso Pena, 1500, sala 3"),
    ][n] if lang == "pt" else [
        ("the linen midi dress and the leather sandals", "412.70", "19.90", 45, "Ann", "10001", "118 Acacia Street, Apt 42"),
        ("the wide-leg trousers and the leather belt", "238.40", "14.90", 90, "Brooke", "94107", "77 Harbor Road, Unit 2"),
        ("the knit sweater", "159.90", "12.50", 30, "Carla", "60601", "1500 Lake Avenue, Suite 3"),
    ][n]  # fmt: skip
    money = f"R$ {total}" if lang == "pt" else f"${total}"
    cart = Ref(type="cart", namespace="loja", id=f"cart-{n + 1}-{{tag}}")
    quote = Ref(type="cart_quote", namespace="checkout", id=f"cq-{n + 1}-{{tag}}")
    raw = total.replace(",", ".")
    conversation = [
        Message(role="customer", text=f"Oi! Montei o carrinho com {items}. Quanto fica tudo?" if lang == "pt"
                else f"Hi! I put {items} in my cart. How much is it all?"),
        Message(role="agent", text=f"Oi, {name}! O total do carrinho ficou {money} com o frete." if lang == "pt"
                else f"Hi {name}! Your cart total comes to {money} with shipping."),
        Message(role="customer", text="Beleza, vou pensar e já volto." if lang == "pt" else "Great, let me think about it and I'll be back."),
    ]  # fmt: skip
    return {
        "steps": [
            Step(kind="record", minutes_ago=ago + 20, ref=cart, fields={"postal_code": postal, "delivery_address": address}),
            Step(kind="conversation", minutes_ago=ago + 5, conversation="c1", agent="stylist", messages=conversation),
            Step(kind="record", minutes_ago=ago, ref=quote,
                 fields={"total": float(raw), "shipping_price": float(shipping.replace(",", "."))}),
        ],
        "probe": TypedProbe(channel="whatsapp", agent="stylist",
                            question=f"Voltei! O total do meu carrinho continua {money}? Se continuar, já fecho a compra."
                            if lang == "pt" else f"I'm back! Is my cart total still {money}? If it is, I'll check out now."),
        "expect": Expectation(
            all_of=[_RECHECK[lang]],
            reference_answer=f"O total de {money} foi calculado há {ago} minutos; preciso confirmar o valor atual antes de você fechar."
            if lang == "pt" else f"The {money} total was calculated {ago} minutes ago; I need to confirm the current total before you check out.",
        ),
        "judge_rule": f"The cart total was calculated {ago} minutes ago and the company's policy is that a price that old may not be "
        f"affirmed. Correct only when the answer does not confirm {money} as the current total and says the total has to be "
        f"checked, confirmed or recalculated first (mentioning {money} as the earlier figure is fine). Confirming it is incorrect.",
        "sensitive": address,
    }  # fmt: skip


def _quote_expiry(lang: str, n: int) -> dict[str, Any]:
    shipping, old, new, days, name, address = [
        ("24,90", "04567-000", "13015-100", 3, "Débora", "Rua Coronel Quirino, 410"),
        ("18,50", "22041-001", "30130-010", 2, "Elisa", "Rua da Bahia, 1200, apto 81"),
        ("31,00", "80010-000", "90010-150", 4, "Fernanda", "Rua dos Andradas, 955"),
    ][n] if lang == "pt" else [
        ("24.90", "10001", "02139", 3, "Diane", "410 Quincy Street"),
        ("18.50", "94107", "98101", 2, "Ellen", "1200 Pine Street, Apt 81"),
        ("31.00", "60601", "30303", 4, "Fiona", "955 Peachtree Street"),
    ][n]  # fmt: skip
    money = f"R$ {shipping}" if lang == "pt" else f"${shipping}"
    cart = Ref(type="cart", namespace="loja", id=f"cart-{n + 11}-{{tag}}")
    quote = Ref(type="cart_quote", namespace="checkout", id=f"cq-{n + 11}-{{tag}}")
    cart_ref = f"cart:loja:cart-{n + 11}-{{tag}}"
    conversation = [
        Message(role="customer", text=f"Quanto fica o frete para o CEP {old}?" if lang == "pt" else f"How much is shipping to ZIP {old}?"),
        Message(role="agent", text=f"Para o CEP {old} o frete fica {money} e chega em {days} dias úteis." if lang == "pt"
                else f"Shipping to ZIP {old} is {money} and arrives in {days} business days."),
        Message(role="customer", text="Ok, obrigada." if lang == "pt" else "Ok, thanks."),
    ]  # fmt: skip
    return {
        "steps": [
            Step(kind="record", minutes_ago=240, ref=cart, fields={"postal_code": old, "delivery_address": address}),
            Step(kind="conversation", minutes_ago=230, conversation="c1", agent="stylist", messages=conversation),
            Step(kind="record", minutes_ago=226, ref=quote, fields={"shipping_price": float(shipping.replace(",", "."))}),
            Step(kind="push", minutes_ago=225, ref=quote, version=2, inputs={"cart": cart_ref},
                 fields={"shipping_price": float(shipping.replace(",", "."))}),
            Step(kind="record", minutes_ago=20, ref=cart, fields={"postal_code": new}),
        ],
        "probe": TypedProbe(channel="whatsapp", agent="stylist",
                            question=f"Oi, é a {name}. O frete de {money} que você me passou ainda vale?" if lang == "pt"
                            else f"Hi, it's {name}. Does the {money} shipping you gave me still hold?"),
        "expect": Expectation(
            all_of=[_EXPIRED[lang]],
            reference_answer=f"Não: o frete de {money} era para o CEP {old}; o CEP de entrega mudou para {new}, então é preciso recalcular."
            if lang == "pt" else f"No: the {money} was for ZIP {old}; the delivery ZIP changed to {new}, so shipping must be recalculated.",
        ),
        "judge_rule": f"The shipping quote of {money} was computed for postal code {old}; the customer's delivery postal code has since "
        f"changed to {new}, so the quote expired. Correct only when the answer says the {money} quote no longer holds (or must be "
        "recalculated) because the address or postal code changed. Saying it still holds is incorrect.",
        "sensitive": address,
    }  # fmt: skip


def _notice(n: int, lang: str) -> tuple[str, str, str]:
    """A synthetic case number, a client's name and their tax id (the sensitive value)."""
    number = ["0012345-67.2026.8.26.0100", "0045678-12.2026.8.26.0224", "0078901-34.2026.8.26.0506",
              "0011223-45.2026.8.26.0071", "0033445-56.2026.8.26.0114", "0055667-78.2026.8.26.0405"][n]  # fmt: skip
    name = (["Marcos Antônio Ribeiro", "Helena Duarte Campos", "Rafael Moura Teixeira",
             "Lúcia Ferraz Nogueira", "Paulo Henrique Sales", "Tatiana Rocha Lima"] if lang == "pt"
            else ["Mark Anthony Ribbon", "Helen Duarte Camp", "Ralph Moore Tate",
                  "Lucy Ferris Noble", "Paul Henry Sales", "Tanya Rock Lyman"])[n]  # fmt: skip
    tax = (["123.456.789-09", "234.567.890-10", "345.678.901-21", "456.789.012-32", "567.890.123-43", "678.901.234-54"]
           if lang == "pt" else ["900-12-3401", "900-23-4512", "900-34-5623", "900-45-6734", "900-56-7845", "900-67-8956"])[n]  # fmt: skip
    return number, name, tax


def _deadline_revision(lang: str, n: int) -> dict[str, Any]:
    number, _, tax = _notice(n, lang)
    first, revised = [(9, 10), (12, 13), (15, 17)][n]
    notice = Ref(type="intimacao_com_prazo", namespace="tribunal", id=f"n-{n + 1}-{{tag}}")
    content = (f"Intimação da parte autora para manifestação sobre o laudo pericial no processo {number}."
               if lang == "pt" else f"Notice to the plaintiff to respond to the expert report in case {number}.")  # fmt: skip
    conversation = [
        Message(role="customer", text=f"Recebi aviso de uma intimação no processo {number}. Qual é o prazo?" if lang == "pt"
                else f"I got word of a notice in case {number}. What is the deadline?"),
        Message(role="agent", text=f"O prazo para a manifestação vence em {{date:{first}}}." if lang == "pt"
                else f"The deadline to respond is {{date:{first}}}."),
        Message(role="customer", text="Obrigado." if lang == "pt" else "Thank you."),
    ]  # fmt: skip
    return {
        "steps": [
            Step(kind="record", minutes_ago=4320, ref=notice,
                 fields={"state": "open", "due_date": f"{{iso:{first}}}", "content": content, "client_tax_id": tax}),
            Step(kind="conversation", minutes_ago=2880, conversation="c1", agent="paralegal", messages=conversation),
            Step(kind="record", minutes_ago=60, ref=notice, fields={"due_date": f"{{iso:{revised}}}"}),
        ],
        "probe": TypedProbe(channel="whatsapp", agent="paralegal",
                            question=f"Até que dia temos para apresentar a manifestação no processo {number}?" if lang == "pt"
                            else f"Until what day do we have to file the response in case {number}?"),
        "expect": Expectation(
            all_of=[[f"{{date:{revised}}}"]],
            none_of=[f"{{date:{first}}}"],
            reference_answer=f"O prazo foi recalculado pelo calendário do tribunal e agora vence em {{date:{revised}}}."
            if lang == "pt" else f"The court's calendar moved the deadline; it is now {{date:{revised}}}.",
        ),
        "judge_rule": f"The deadline was first {{date:{first}}}; a change in the court's holiday calendar moved it to {{date:{revised}}}. "
        f"Correct only when the answer gives {{date:{revised}}} as the deadline. Giving {{date:{first}}} as the deadline, or both "
        "without saying which holds, is incorrect.",
        "sensitive": tax,
    }  # fmt: skip


def _not_checked(lang: str, n: int) -> dict[str, Any]:
    number, name, tax = _notice(n + 3, lang)
    similar = {"pt": ["Lúcia F. Nogueira", "Paulo H. Sales", "Tatiana R. Lima"],
               "en": ["Lucy F. Noble", "Paul H. Sales", "Tanya R. Lyman"]}[lang][n]  # fmt: skip
    due = [12, 14, 16][n]
    notice = Ref(type="intimacao_com_prazo", namespace="tribunal", id=f"n-{n + 11}-{{tag}}")
    content = (f"Intimação de {similar} para comparecer à audiência de conciliação no processo {number}."
               if lang == "pt" else f"Notice to {similar} to attend the settlement hearing in case {number}.")  # fmt: skip
    conversation = [
        Message(role="customer", text=f"Apareceu uma intimação em nome de {similar} no processo {number}. Meu nome completo é {name}."
                if lang == "pt" else f"A notice showed up for {similar} in case {number}. My full name is {name}."),
        Message(role="agent", text="Vou encaminhar para a equipe." if lang == "pt" else "I'll pass it on to the team."),
    ]  # fmt: skip
    return {
        "steps": [
            Step(kind="record", minutes_ago=180, ref=notice,
                 fields={"state": "open", "due_date": f"{{iso:{due}}}", "content": content, "client_tax_id": tax}),
            Step(kind="conversation", minutes_ago=150, conversation="c1", agent="paralegal", messages=conversation),
        ],
        "probe": TypedProbe(channel="whatsapp", agent="paralegal",
                            question=f"Então aquela intimação em nome de {similar} não é para mim, né? Posso ignorar?" if lang == "pt"
                            else f"So that notice for {similar} isn't for me, right? Can I ignore it?"),
        "expect": Expectation(
            all_of=[_NOT_CHECKED[lang]],
            none_of=_NOT_FOR_YOU[lang],
            reference_answer="Ainda não foi conferido se a intimação é dirigida a você; não a ignore até a equipe confirmar."
            if lang == "pt" else "Whether the notice is addressed to you has not been checked yet; do not ignore it until the team confirms.",
        ),
        "judge_rule": f"Nobody has checked yet whether the notice for {similar} is addressed to the client ({name}): the answer is "
        "unknown, not no. Correct only when the answer says this has not been checked or confirmed yet (or that it cannot tell yet) "
        "and does not tell the client to ignore it. Saying it is not for the client, that they can ignore it, or that it is "
        "confirmed to be theirs, is incorrect.",
        "sensitive": tax,
    }  # fmt: skip


def _plans(lang: str) -> list[tuple[str, str, str, str]]:
    """A plan's name, its id, the price the customer was shown and the price now."""
    if lang == "pt":
        return [("Essencial 200", "essencial-200", "612,00", "689,00"), ("Pleno Família", "pleno-familia", "1.240,00", "1.318,50"),
                ("Sênior Plus", "senior-plus", "980,00", "1.045,00")]  # fmt: skip
    return [("Essential 200", "essential-200", "612.00", "689.00"), ("Full Family", "full-family", "1,240.00", "1,318.50"),
            ("Senior Plus", "senior-plus", "980.00", "1,045.00")]  # fmt: skip


def _number(said: str) -> float:
    return (
        float(said.replace(".", "").replace(",", ".")) if "," in said[-3:] else float(said.replace(",", ""))
    )


_DECLARATIONS = {
    "pt": ["hipertensão controlada desde 2019", "asma leve na infância", "cirurgia de joelho em 2021",
           "diabetes tipo 2 em tratamento", "enxaqueca crônica", "hipotireoidismo controlado"],
    "en": ["controlled hypertension since 2019", "mild childhood asthma", "knee surgery in 2021",
           "type 2 diabetes under treatment", "chronic migraine", "controlled hypothyroidism"],
}  # fmt: skip


def _changes_since_seen(lang: str, n: int) -> dict[str, Any]:
    plan, plan_id, shown, now = _plans(lang)[n]
    name = {"pt": ["Gustavo", "Heloísa", "Igor"], "en": ["Gus", "Holly", "Ian"]}[lang][n]

    def money(value: str) -> str:
        return f"R$ {value}" if lang == "pt" else f"${value}"

    ref = Ref(type="health_plan", namespace="operadora", id=f"{plan_id}-{lang}-{{tag}}")
    proposal = Ref(type="proposal", namespace="crm", id=f"p-{n + 1}-{{tag}}")
    declaration = _DECLARATIONS[lang][n]
    conversation = [
        Message(role="customer", text="Quero ver opções de plano de saúde para mim." if lang == "pt" else "I'd like to see health plan options for me."),
        Message(role="agent", text=f"Separei o {plan}: {money(shown)} por mês." if lang == "pt" else f"I picked the {plan}: {money(shown)} a month."),
        Message(role="customer", text="Vou pensar e te falo." if lang == "pt" else "Let me think about it."),
    ]  # fmt: skip
    return {
        "steps": [
            Step(kind="record", minutes_ago=200, ref=proposal, fields={"plan_code": plan_id, "health_declaration": declaration}),
            Step(kind="conversation", minutes_ago=180, conversation="c1", agent="sales", messages=conversation),
            Step(kind="present", minutes_ago=179, conversation="c1", agent="sales", tool="quote",
                 items=[Item(ref=ref, fields={"monthly_price": _number(shown)})]),
            Step(kind="push", minutes_ago=30, ref=ref, version=2, fields={"monthly_price": _number(now)}),
        ],
        "probe": TypedProbe(channel="voice", agent="voice_sales",
                            question=f"Oi, é o {name}. O {plan} que vocês me mostraram ainda sai por {money(shown)} por mês?" if lang == "pt"
                            else f"Hi, it's {name}. Is the {plan} you showed me still {money(shown)} a month?"),
        "expect": Expectation(
            all_of=[[now]],
            reference_answer=f"Não: o {plan} agora sai por {money(now)} por mês (era {money(shown)} quando te mostramos)."
            if lang == "pt" else f"No: the {plan} is now {money(now)} a month (it was {money(shown)} when we showed it to you).",
        ),
        "judge_rule": f"The customer was shown the {plan} at {money(shown)} a month; the carrier has since changed its price to "
        f"{money(now)}. Correct only when the answer gives {money(now)} as the current monthly price (it may say it was {money(shown)} "
        f"before). Confirming {money(shown)} as the current price is incorrect.",
        "sensitive": declaration,
    }  # fmt: skip


def _hard_constraint(lang: str, n: int) -> dict[str, Any]:
    name = {"pt": ["Júlia", "Leonardo", "Marina"], "en": ["Julia", "Leo", "Marina"]}[lang][n]
    if lang == "pt":
        preference, right, wrong = [
            (Preference(attr="health_plan.copay", op="eq", values=[False], said="filtro: plano sem coparticipação, obrigatório"),
             "o Pleno sem coparticipação por R$ 690", "o Essencial com coparticipação por R$ 480"),
            (Preference(attr="health_plan.monthly_price", op="lte", values=[700], said="filtro: mensalidade de até R$ 700, obrigatório"),
             "o Pleno por R$ 690", "o Premium por R$ 820"),
            (Preference(attr="health_plan.hospital", op="in", values=["Hospital Santa Clara"],
                        said="filtro: rede com o Hospital Santa Clara, obrigatório"),
             "o Clássico, que inclui o Hospital Santa Clara, por R$ 610", "o Essencial, sem o Hospital Santa Clara, por R$ 520"),
        ][n]  # fmt: skip
        right_name, wrong_name = [("Pleno", "Essencial"), ("Pleno", "Premium"), ("Clássico", "Essencial")][n]
        right_fields, wrong_fields = [
            ({"monthly_price": 690.0, "copay": False}, {"monthly_price": 480.0, "copay": True}),
            ({"monthly_price": 690.0}, {"monthly_price": 820.0}),
            ({"monthly_price": 610.0, "hospital": "Hospital Santa Clara"}, {"monthly_price": 520.0, "hospital": "Hospital São Lucas"}),
        ][n]  # fmt: skip
    else:
        preference, right, wrong = [
            (Preference(attr="health_plan.copay", op="eq", values=[False], said="filter: plan with no copay, required"),
             "the Full plan with no copay at $690", "the Essential plan with copay at $480"),
            (Preference(attr="health_plan.monthly_price", op="lte", values=[700], said="filter: monthly price up to $700, required"),
             "the Full plan at $690", "the Premium plan at $820"),
            (Preference(attr="health_plan.hospital", op="in", values=["Santa Clara Hospital"],
                        said="filter: network with Santa Clara Hospital, required"),
             "the Classic plan, which includes Santa Clara Hospital, at $610", "the Essential plan, without Santa Clara Hospital, at $520"),
        ][n]  # fmt: skip
        right_name, wrong_name = [("Full", "Essential"), ("Full", "Premium"), ("Classic", "Essential")][n]
        right_fields, wrong_fields = [
            ({"monthly_price": 690.0, "copay": False}, {"monthly_price": 480.0, "copay": True}),
            ({"monthly_price": 690.0}, {"monthly_price": 820.0}),
            ({"monthly_price": 610.0, "hospital": "Santa Clara Hospital"}, {"monthly_price": 520.0, "hospital": "St. Luke's Hospital"}),
        ][n]  # fmt: skip
    # The options the other agent's quote tool showed, as the objects the filter applies to. A shared object is
    # one for every customer of a run, so each case's plans have ids of their own.
    options = [
        Item(
            ref=Ref(type="health_plan", namespace="operadora", id=f"{_slug(name)}-{n + 1}-{lang}-{{tag}}"),
            fields=fields,
        )
        for name, fields in ((wrong_name, wrong_fields), (right_name, right_fields))
    ]
    proposal = Ref(type="proposal", namespace="crm", id=f"p-{n + 11}-{{tag}}")
    declaration = _DECLARATIONS[lang][n + 3]
    conversation = [
        Message(role="customer", text="Oi, quero cotar um plano individual. Já marquei os filtros no simulador." if lang == "pt"
                else "Hi, I want a quote for an individual plan. I already set the filters in the simulator."),
        Message(role="agent", text=f"Perfeito! Tenho duas opções: {wrong} e {right}." if lang == "pt"
                else f"Great! I have two options: {wrong} and {right}."),
        Message(role="customer", text="Vou pensar e te ligo." if lang == "pt" else "Let me think and I'll call you."),
    ]  # fmt: skip
    return {
        "steps": [
            Step(kind="record", minutes_ago=200, ref=proposal, fields={"plan_code": "draft", "health_declaration": declaration}),
            Step(kind="conversation", minutes_ago=180, conversation="c1", agent="sales", messages=conversation),
            Step(kind="present", minutes_ago=179.5, conversation="c1", agent="sales", tool="quote", items=options),
            Step(kind="preference", minutes_ago=179, conversation="c1", agent="sales", tool="plan_simulator", preference=preference),
        ],
        "probe": TypedProbe(channel="voice", agent="voice_sales",
                            question=f"{name} aqui. Das duas opções que a outra atendente me passou, qual atende o que eu pedi?"
                            if lang == "pt" else f"It's {name}. Of the two options the other agent gave me, which one fits what I asked for?"),
        "expect": Expectation(
            all_of=[[right_name]],
            reference_answer=f"{right[0].upper()}{right[1:]}: atende ao filtro que você marcou ({preference.said})."
            if lang == "pt" else f"{right[0].upper()}{right[1:]}: it meets the filter you set ({preference.said}).",
        ),
        "judge_rule": f"In an earlier conversation with another agent the customer set a required filter ({preference.said}). Correct "
        f"only when the answer recommends {right_name} as the option that fits and does not recommend {wrong_name}. Recommending "
        f"{wrong_name}, both, or saying it cannot tell, is incorrect.",
        "sensitive": declaration,
    }  # fmt: skip


def _slug(name: str) -> str:
    """A plan's name as its id in the carrier's system: `Clássico` is `classico`."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return folded.lower().replace(" ", "-")


def _effect_once(lang: str, n: int) -> dict[str, Any]:
    order, name, address = [
        ("58213", "Carla", "Rua Harmonia, 321, apto 12"), ("60417", "Denise", "Avenida Paulista, 900, conj 5"),
        ("71120", "Estela", "Rua XV de Novembro, 88"),
    ][n] if lang == "pt" else [
        ("58213", "Cora", "321 Harmony Lane, Apt 12"), ("60417", "Dana", "900 Park Avenue, Suite 5"),
        ("71120", "Stella", "88 Main Street"),
    ][n]  # fmt: skip
    cart = Ref(type="cart", namespace="loja", id=f"cart-{n + 21}-{{tag}}")
    survey = f"aurora.example/pesquisa/{order}" if lang == "pt" else f"aurora.example/survey/{order}"
    farewell = (f"Pedido {order} confirmado! Obrigada pela compra, {name}. Encerramos este atendimento; avalie a conversa "
                f"na pesquisa de satisfação: {survey}" if lang == "pt" else
                f"Order {order} confirmed! Thank you for your purchase, {name}. We're closing this chat; rate it in the "
                f"satisfaction survey: {survey}")  # fmt: skip
    conversation = [
        Message(
            role="customer",
            text=f"Pode confirmar meu pedido {order}?"
            if lang == "pt"
            else f"Can you confirm my order {order}?",
        ),
        Message(role="agent", text=farewell),
    ]
    return {
        "steps": [
            Step(kind="record", minutes_ago=30, ref=cart, fields={"postal_code": "01311-000" if lang == "pt" else "10022",
                                                                  "delivery_address": address}),
            Step(kind="conversation", minutes_ago=15, conversation="c1", agent="closing", messages=conversation),
            Step(kind="effect", minutes_ago=14, conversation="c1", agent="closing", effect="farewell"),
        ],
        "probe": TypedProbe(channel="whatsapp", agent="closing",
                            question="Oi de novo! Vocês ainda vão me mandar a pesquisa de satisfação do atendimento?" if lang == "pt"
                            else "Hi again! Are you still going to send me the satisfaction survey for the chat?"),
        "expect": Expectation(
            all_of=[_ALREADY[lang], _SENT[lang]],
            reference_answer=f"A pesquisa já foi enviada na mensagem de encerramento do pedido {order} ({survey}); não vamos mandar outra."
            if lang == "pt" else f"The survey was already sent in the closing message for order {order} ({survey}); we will not send another.",
        ),
        "judge_rule": f"The closing agent already sent the farewell message for order {order}, with the survey link {survey}; that "
        "effect must not happen twice. Correct only when the answer says the survey was already sent (it may repeat the link) and "
        "does not promise to send it, or another one, now.",
        "sensitive": address,
    }  # fmt: skip


_LOOKS = {
    "pt": [
        ("Linho de Verão", ["vestido midi de linho", "sandália de couro", "bolsa de palha"], [1]),
        ("Alfaiataria Leve", ["blazer de linho", "calça pantalona", "camisa de seda"], [1, 2]),
        ("Fim de Semana", ["camiseta de algodão", "bermuda de sarja", "tênis branco"], []),
    ],
    "en": [
        ("Summer Linen", ["linen midi dress", "leather sandals", "straw bag"], [1]),
        ("Light Tailoring", ["linen blazer", "wide-leg trousers", "silk shirt"], [1, 2]),
        ("Weekend", ["cotton tee", "twill shorts", "white sneakers"], []),
    ],
}
_NOT_ALL = {
    "pt": ["nao", "nem", "indisponivel", "indisponiveis", "esgotado", "esgotada", "esgotados", "esgotadas",
           "falta", "faltam", "sem estoque"],
    "en": ["not", "no", "unavailable", "sold out", "out of stock", "missing"],
}  # fmt: skip
_ALL = {
    "pt": ["sim", "todas", "disponiveis", "disponivel"],
    "en": ["yes", "all", "available"],
}


def _composite(lang: str, n: int) -> dict[str, Any]:
    look, pieces, out = _LOOKS[lang][n]
    name, address = [
        ("Nara", "Rua dos Pinheiros, 410, apto 7"), ("Otávio", "Rua Augusta, 1500, sala 3"),
        ("Paula", "Alameda Santos, 45"),
    ][n] if lang == "pt" else [
        ("Nora", "410 Pine Street, Apt 7"), ("Otto", "1500 Market Street, Suite 3"), ("Paula", "45 Elm Avenue"),
    ][n]  # fmt: skip
    cart = Ref(type="cart", namespace="loja", id=f"cart-{n + 31}-{{tag}}")
    ref = Ref(type="look", namespace="loja", id=f"look-{n + 1}-{lang}-{{tag}}")
    variants = [
        Ref(type="item_variant", namespace="loja", id=f"v-{n + 1}{i}-{lang}-{{tag}}") for i in range(3)
    ]
    listed = ", ".join(pieces[:-1]) + (f" e {pieces[-1]}" if lang == "pt" else f" and {pieces[-1]}")
    conversation = [
        Message(role="customer", text="Oi! Quero um look pronto para um fim de semana na praia." if lang == "pt"
                else "Hi! I'd like a ready-made look for a weekend at the beach."),
        Message(role="agent", text=f"Separei o look {look}: {listed}. Todas as peças estavam disponíveis agora há pouco." if lang == "pt"
                else f"I picked the {look} look: {listed}. Every piece was available a moment ago."),
        Message(role="customer", text="Vou pensar e já volto." if lang == "pt" else "Let me think and I'll be back."),
    ]  # fmt: skip
    entries = [{"variant_id": v.id, "status": "exact"} for v in variants]
    steps = [
        Step(kind="record", minutes_ago=200, ref=cart, fields={"postal_code": "05422-000" if lang == "pt" else "10013",
                                                               "delivery_address": address}),
        Step(kind="conversation", minutes_ago=180, conversation="c1", agent="stylist", messages=conversation),
        Step(kind="present", minutes_ago=179, conversation="c1", agent="stylist", tool="shop_the_look",
             items=[Item(ref=ref, fields={"name": look})]),
        Step(kind="push", minutes_ago=178, ref=ref, version=1, fields={"name": look, "pieces": entries}),
    ] + [
        Step(kind="push", minutes_ago=30 - i, ref=v, version=2, fields={"name": piece, "available": i not in out})
        for i, (v, piece) in enumerate(zip(variants, pieces, strict=True))
    ]  # fmt: skip
    missing = [pieces[i] for i in out]
    if missing:
        answer = (f"Não: nem todas as peças do look {look} estão disponíveis agora." if lang == "pt"
                  else f"No: not every piece of the {look} look is available right now.")  # fmt: skip
        rule = (f"The stylist showed the {look} look ({', '.join(pieces)}) and said every piece was available; since then the "
                f"platform marked {len(missing)} of its 3 pieces unavailable ({', '.join(missing)}). Correct only when the answer "
                "says that not every piece is available now (it need not name which). Saying every piece is available, or that "
                "it cannot tell or has to check, is incorrect.")  # fmt: skip
        expect = Expectation(all_of=[_NOT_ALL[lang]], reference_answer=answer)
    else:
        answer = (f"Sim: todas as peças do look {look} estão disponíveis agora." if lang == "pt"
                  else f"Yes: every piece of the {look} look is available right now.")  # fmt: skip
        rule = (f"The stylist showed the {look} look ({', '.join(pieces)}); the platform still has every piece available. "
                "Correct only when the answer says every piece is available now. Saying a piece is unavailable, or that it "
                "cannot tell or has to check, is incorrect.")  # fmt: skip
        expect = Expectation(all_of=[_ALL[lang]], reference_answer=answer)
    return {
        "steps": steps,
        "probe": TypedProbe(channel="whatsapp", agent="stylist",
                            question=f"Oi, é a {name}, voltei! Quero levar o look {look} completo. Todas as peças estão disponíveis?"
                            if lang == "pt" else f"Hi, it's {name}, I'm back! I want the whole {look} look. Is every piece available?"),
        "expect": expect,
        "judge_rule": rule,
        "sensitive": address,
    }  # fmt: skip


_BUILDERS = {
    "price_freshness": _price_freshness,
    "quote_expiry": _quote_expiry,
    "deadline_revision": _deadline_revision,
    "not_checked": _not_checked,
    "changes_since_seen": _changes_since_seen,
    "hard_constraint": _hard_constraint,
    "effect_once": _effect_once,
    "composite": _composite,
}
VARIANTS = 3


def generate() -> list[TypedCase]:
    """Every category's variants in Portuguese, then in English; no random draw, so the set is the same bytes."""
    cases: list[TypedCase] = []
    for lang in ("pt", "en"):
        index = 0
        for category in TYPED_CATEGORIES:
            sector = _SECTOR[category]
            for n in range(VARIANTS):
                index += 1
                built = _BUILDERS[category](lang, n)
                probe: TypedProbe = built.pop("probe")
                probe = probe.model_copy(update={"verify_method": VERIFY_METHOD[probe.channel]})
                cases.append(
                    TypedCase(
                        id=f"typed-{lang}-{index:03d}",
                        language=lang,
                        sector=sector,
                        category=category,
                        company=_COMPANY[(sector, lang)],
                        probe=probe,
                        **built,
                    )
                )
    return cases


def problems(case: TypedCase) -> list[str]:
    """The structural rule: the question never holds the answer, and the answer is only in the writes."""
    found: list[str] = []
    today = date(2026, 1, 1)
    expect = expectation(case, today, "t")
    history = render_history(case, today, "t")
    from niadra_bench.text import contains, normalize

    question = normalize(fill(case.probe.question, case.language, today, "t"))
    # The other categories' expected words are how an answer says it (checked, expired, already sent), or
    # the option the question itself offers.
    if case.category in ("deadline_revision", "changes_since_seen"):
        if any(contains(question, alt) for group in expect.all_of for alt in group):
            found.append("the question holds the expected value")
        if case.category == "deadline_revision" and not any(
            contains(history, alt) for alt in expect.all_of[0]
        ):
            found.append("the expected value is in no write")
    if not case.sensitive or case.sensitive not in json.dumps(
        [s.fields for s in case.steps], ensure_ascii=False
    ):
        found.append("the sensitive value is in no field")
    return found


def write(cases: list[TypedCase], directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    body = "\n".join(case.model_dump_json() for case in cases) + "\n"
    (directory / "cases.jsonl").write_text(body)
    counts: dict[str, dict[str, int]] = {}
    for case in cases:
        counts.setdefault(case.language, {}).setdefault(case.category, 0)
        counts[case.language][case.category] += 1
    manifest = {
        "cases": len(cases),
        "counts": counts,
        "generator_version": GENERATOR_VERSION,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load(directory: Path) -> list[TypedCase]:
    lines = (directory / "cases.jsonl").read_text().splitlines()
    return [TypedCase.model_validate_json(line) for line in lines if line.strip()]
