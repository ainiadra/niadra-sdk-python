"""niadra-expr: the language of the type registry's conditions, timers, keys and readings.

Small, deterministic and total: literals, names, comparison, `in`, `and`, `or`, `not`, `+`, `-` and a fixed
set of functions; no loop, no I/O, and bounded text, tokens and nesting. Every value carries one of the four
logical values (`niadra.state.logic`), so a comparison with a value nobody observed is itself unobserved,
never false. The server and both SDKs implement the same language: the Object Type spec fixes it (section 5)
with the vectors in `spec/vectors/niadra-expr.v0.json`.

`parse` reads the text, `compile_expression` also resolves its names against a type declaration (the
registry's validation), and `evaluate` computes it over an `Environment`, the object's slots at one instant.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from niadra.state.logic import Logic, unknown_of
from niadra.vocabulary import _StrEnum

MAX_LENGTH = 1024
MAX_TOKENS = 256
MAX_NESTING = 16
MAX_NAME = 64
MAX_STRING = 256
MAX_LIST_LITERAL = 64
MAX_LIST = 1000
MAX_DURATION_DIGITS = 6
MAX_BUSINESS_DAYS = 1000
MAX_SHA256_ARGS = 8
MAX_EXACT_INTEGER = 2**53 - 1

SECOND_MS = 1000
MINUTE_MS = 60 * SECOND_MS
DAY_MS = 24 * 60 * MINUTE_MS
UNITS = {"ms": 1, "s": SECOND_MS, "min": MINUTE_MS, "h": 60 * MINUTE_MS, "d": DAY_MS}


class ExprError(ValueError):
    """`expr_invalid`: the text is not a valid expression, or names what the type does not declare;
    `expr_type`: a value of the wrong kind, or out of its domain, met during evaluation;
    `expr_limit`: a bound of the language was exceeded."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _invalid(detail: str) -> ExprError:
    return ExprError("expr_invalid", detail)


def _type(detail: str) -> ExprError:
    return ExprError("expr_type", detail)


def _limit(detail: str) -> ExprError:
    return ExprError("expr_limit", detail)


class Kind(_StrEnum):
    BOOL = "bool"
    NUMBER = "number"
    STRING = "string"
    DURATION = "duration"
    DATE = "date"
    DATETIME = "datetime"
    LIST = "list"
    BUSINESS_DAYS = "business_days"
    QUOTE = "quote"


@dataclass(frozen=True, slots=True)
class Calendar:
    """A named list of holidays and the weekdays that are never business days (ISO: 1 is Monday)."""

    holidays: frozenset[date] = frozenset()
    weekend: frozenset[int] = frozenset({6, 7})


@dataclass(frozen=True, slots=True)
class Value:
    """A value and its logical value. Known values are present (`yes`, or a boolean) or absent (`no`, with
    the declared name of the absence when there is one); unknown ones carry nothing.

    Data: a boolean, a float, a string, milliseconds for a duration, a `date`, epoch milliseconds for a
    datetime, a tuple of values for a list, `(count, calendar)` for business days, and a mapping of slots
    for a quote."""

    logic: Logic
    kind: Kind | None = None
    datum: object = None
    absent: str | None = None

    @property
    def is_absent(self) -> bool:
        return self.logic is Logic.NO and self.kind is None


TRUE = Value(Logic.YES, Kind.BOOL, True)
FALSE = Value(Logic.NO, Kind.BOOL, False)
ABSENT = Value(Logic.NO)
UNOBSERVED = Value(Logic.UNOBSERVED)


def boolean(flag: bool) -> Value:
    return TRUE if flag else FALSE


def unknown(logic: Logic) -> Value:
    return Value(logic)


def absent(name: str | None = None) -> Value:
    return ABSENT if name is None else Value(Logic.NO, absent=name)


@dataclass(frozen=True, slots=True)
class Slot:
    """One field, computed value, time axis or input of an object as a reading sees it: the value, when it was
    observed at the source (`at`, epoch milliseconds), by whom, its value before the latest change, and the
    completeness level of a content field."""

    value: Value
    at: int | None = None
    observer: str | None = None
    was: Value | None = None
    completeness: str | None = None


@dataclass(frozen=True, slots=True)
class Environment:
    now: int
    utc_offset_min: int = 0
    fields: Mapping[str, Slot] = field(default_factory=dict)
    inputs: Mapping[str, Slot] = field(default_factory=dict)
    absent_names: frozenset[str] = frozenset()
    config: Mapping[str, Value] = field(default_factory=dict)
    quotes: Mapping[str, Mapping[str, Slot]] = field(default_factory=dict)
    calendars: Mapping[str, Calendar] = field(default_factory=dict)
    state: str | None = None
    derived_status: str | None = None
    watch_count: int = 0
    purpose: str = "display"
    presented_rank: int | None = None


# Syntax


class Node:
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Lit(Node):
    value: Value


@dataclass(frozen=True, slots=True)
class LogicWord(Node):
    logic: Logic


@dataclass(frozen=True, slots=True)
class Name(Node):
    parts: tuple[str, ...]

    @property
    def dotted(self) -> str:
        return ".".join(self.parts)


@dataclass(frozen=True, slots=True)
class Member(Node):
    target: Node
    name: str


@dataclass(frozen=True, slots=True)
class Call(Node):
    function: str
    args: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class ListLit(Node):
    items: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Not(Node):
    operand: Node


@dataclass(frozen=True, slots=True)
class BoolOp(Node):
    op: str
    operands: tuple[Node, ...]


@dataclass(frozen=True, slots=True)
class Compare(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Arith(Node):
    op: str
    left: Node
    right: Node


LOGIC_WORDS = {logic.value: logic for logic in Logic}
LITERAL_WORDS = {"true": TRUE, "false": FALSE, "none": ABSENT}
OPERATOR_WORDS = frozenset({"and", "or", "not", "in"})
KEYWORDS = frozenset({*LOGIC_WORDS, *LITERAL_WORDS, *OPERATOR_WORDS})
META = {
    "state": Kind.STRING,
    "derived_status": Kind.STRING,
    "watch_count": Kind.NUMBER,
    "purpose": Kind.STRING,
}
RESERVED = frozenset({*KEYWORDS, *META, "config"})
"""Names a type may not give a field, a value, a time axis or an absence."""
FUNCTIONS = frozenset(
    {"now", "age", "observer", "changed", "was", "count", "business_days", "quote", "presented_in_top"}
    | {"sha256"}
)
COMPARISONS = frozenset({"==", "!=", "<", "<=", ">", ">=", "in"})
_TWO_CHAR = ("==", "!=", "<=", ">=")
_ONE_CHAR = frozenset("<>+-()[],.")
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyz_")
_NAME_CHARS = _NAME_START | frozenset("0123456789")
_DIGITS = frozenset("0123456789")
_SPACE = frozenset(" \t\r\n")


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # num, dur, str, name, op, end
    text: str
    value: object = None


def _tokens(text: str) -> list[_Token]:
    if len(text) > MAX_LENGTH:
        raise _limit(f"an expression has at most {MAX_LENGTH} characters")
    out: list[_Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in _SPACE:
            i += 1
            continue
        if c in _DIGITS:
            token, i = _number(text, i)
        elif c == "'":
            token, i = _string(text, i)
        elif c in _NAME_START:
            j = i
            while j < n and text[j] in _NAME_CHARS:
                j += 1
            if j < n and text[j].isalpha():
                raise _invalid(f"unexpected character {text[j]!r}: names are lowercase ASCII")
            if j - i > MAX_NAME:
                raise _limit(f"a name has at most {MAX_NAME} characters")
            token, i = _Token("name", text[i:j]), j
        elif text.startswith(_TWO_CHAR, i):
            token, i = _Token("op", text[i : i + 2]), i + 2
        elif c in _ONE_CHAR:
            token, i = _Token("op", c), i + 1
        else:
            raise _invalid(f"unexpected character {c!r}")
        out.append(token)
        if len(out) > MAX_TOKENS:
            raise _limit(f"an expression has at most {MAX_TOKENS} tokens")
    if not out:
        raise _invalid("the expression is empty")
    out.append(_Token("end", ""))
    return out


def _number(text: str, i: int) -> tuple[_Token, int]:
    j = i
    while j < len(text) and text[j] in _DIGITS:
        j += 1
    whole = text[i:j]
    fraction = False
    if j < len(text) and text[j] == ".":
        k = j + 1
        while k < len(text) and text[k] in _DIGITS:
            k += 1
        if k == j + 1:
            raise _invalid(f"a number needs digits after the point: {text[i : j + 1]!r}")
        fraction, j = True, k
    if j < len(text) and (text[j] in _NAME_CHARS or text[j].isalpha()):
        k = j
        while k < len(text) and (text[k] in _NAME_CHARS or text[k].isalpha()):
            k += 1
        unit = text[j:k]
        if unit not in UNITS:
            raise _invalid(f"unknown duration unit {unit!r}: use ms, s, min, h or d")
        if fraction:
            raise _invalid(f"a duration is a whole number of units: {text[i:k]!r}")
        if len(whole) > MAX_DURATION_DIGITS:
            raise _limit(f"a duration has at most {MAX_DURATION_DIGITS} digits")
        return _Token("dur", text[i:k], int(whole) * UNITS[unit]), k
    return _Token("num", text[i:j], float(text[i:j])), j


def _string(text: str, i: int) -> tuple[_Token, int]:
    chars: list[str] = []
    j = i + 1
    while True:
        if j >= len(text) or text[j] in "\r\n":
            raise _invalid("a string is not closed")
        c = text[j]
        if c == "'":
            break
        if c == "\\":
            escaped = text[j + 1] if j + 1 < len(text) else ""
            if escaped not in ("'", "\\"):
                raise _invalid("a string escapes only a quote and a backslash")
            chars.append(escaped)
            j += 2
            continue
        chars.append(c)
        j += 1
    value = "".join(chars)
    if len(value) > MAX_STRING:
        raise _limit(f"a string has at most {MAX_STRING} characters")
    return _Token("str", text[i : j + 1], value), j + 1


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.nesting = 0

    @property
    def token(self) -> _Token:
        return self.tokens[self.pos]

    def advance(self) -> _Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def at(self, kind: str, text: str | None = None) -> bool:
        token = self.token
        return token.kind == kind and (text is None or token.text == text)

    def at_word(self, word: str) -> bool:
        return self.at("name", word)

    def expect(self, text: str, context: str) -> None:
        if not self.at("op", text):
            raise _invalid(f"expected {text!r} {context}, found {self.describe()}")
        self.advance()

    def unexpected(self) -> ExprError:
        if self.token.kind == "end":
            return _invalid("the expression ends too early")
        return _invalid(f"unexpected {self.token.text!r}")

    def describe(self) -> str:
        token = self.token
        return "the end of the expression" if token.kind == "end" else repr(token.text)

    def enter(self) -> None:
        self.nesting += 1
        if self.nesting > MAX_NESTING:
            raise _limit(f"an expression nests at most {MAX_NESTING} groups")

    def leave(self) -> None:
        self.nesting -= 1

    def expression(self) -> Node:
        node = self.disjunction()
        if not self.at("end"):
            raise self.unexpected()
        return node

    def disjunction(self) -> Node:
        operands = [self.conjunction()]
        while self.at_word("or"):
            self.advance()
            operands.append(self.conjunction())
        return operands[0] if len(operands) == 1 else BoolOp("or", tuple(operands))

    def conjunction(self) -> Node:
        operands = [self.negation()]
        while self.at_word("and"):
            self.advance()
            operands.append(self.negation())
        return operands[0] if len(operands) == 1 else BoolOp("and", tuple(operands))

    def negation(self) -> Node:
        if self.at_word("not"):
            self.advance()
            return Not(self.negation())
        return self.comparison()

    def comparison(self) -> Node:
        left = self.sum()
        op = self.comparison_op()
        if op is None:
            return left
        self.advance()
        right = self.sum()
        if self.comparison_op() is not None:
            raise _invalid("comparisons do not chain: use and")
        return Compare(op, left, right)

    def comparison_op(self) -> str | None:
        token = self.token
        if token.kind == "op" and token.text in COMPARISONS:
            return token.text
        if token.kind == "name" and token.text == "in":
            return token.text
        return None

    def sum(self) -> Node:
        node = self.postfix()
        while self.at("op", "+") or self.at("op", "-"):
            op = self.advance().text
            node = Arith(op, node, self.postfix())
        return node

    def postfix(self) -> Node:
        node, grouped = self.primary()
        while self.at("op", "."):
            self.advance()
            name = self.member_name()
            if isinstance(node, Name) and not grouped:
                node = Name((*node.parts, name))
            else:
                node, grouped = Member(node, name), False
        return node

    def member_name(self) -> str:
        token = self.token
        if token.kind != "name" or token.text in KEYWORDS:
            raise _invalid(f"expected a name after '.', found {self.describe()}")
        self.advance()
        return token.text

    def primary(self) -> tuple[Node, bool]:
        token = self.token
        if token.kind == "num":
            self.advance()
            return Lit(Value(Logic.YES, Kind.NUMBER, token.value)), False
        if token.kind == "dur":
            self.advance()
            return Lit(Value(Logic.YES, Kind.DURATION, token.value)), False
        if token.kind == "str":
            self.advance()
            return Lit(Value(Logic.YES, Kind.STRING, token.value)), False
        if token.kind == "name":
            word = token.text
            if word in LITERAL_WORDS:
                self.advance()
                return Lit(LITERAL_WORDS[word]), False
            if word in LOGIC_WORDS:
                self.advance()
                return LogicWord(LOGIC_WORDS[word]), False
            if word in OPERATOR_WORDS:
                raise _invalid(f"unexpected {word!r}")
            self.advance()
            if self.at("op", "("):
                return self.call(word), False
            return Name((word,)), False
        if self.at("op", "("):
            self.advance()
            self.enter()
            node = self.disjunction()
            self.expect(")", "to close the group")
            self.leave()
            return node, True
        if self.at("op", "["):
            return self.list_literal(), False
        raise self.unexpected()

    def list_literal(self) -> Node:
        self.advance()
        self.enter()
        items: list[Node] = []
        if not self.at("op", "]"):
            items.append(self.disjunction())
            while self.at("op", ","):
                self.advance()
                items.append(self.disjunction())
        self.expect("]", "to close the list")
        self.leave()
        if len(items) > MAX_LIST_LITERAL:
            raise _limit(f"a list literal has at most {MAX_LIST_LITERAL} items")
        return ListLit(tuple(items))

    def call(self, function: str) -> Node:
        if function not in FUNCTIONS:
            raise _invalid(f"unknown function {function!r}")
        self.advance()
        self.enter()
        args: list[Node] = []
        if function == "now":
            if not self.at("op", ")"):
                raise _invalid("now() takes no argument")
        elif function in ("age", "observer", "changed"):
            args.append(self.reference(function))
        elif function == "quote":
            args.append(Name((self.bare_name(function, "the name of a quote source"),)))
        elif function == "presented_in_top":
            args.append(self.position())
        elif function == "business_days":
            args.append(self.disjunction())
            if not self.at("op", ","):
                raise _invalid("business_days() takes a count and a calendar")
            self.advance()
            args.append(Name((self.bare_name(function, "the name of a calendar"),)))
        elif function == "sha256":
            args.append(self.disjunction())
            while self.at("op", ","):
                self.advance()
                args.append(self.disjunction())
            if len(args) > MAX_SHA256_ARGS:
                raise _invalid(f"sha256() takes at most {MAX_SHA256_ARGS} arguments")
        else:
            args.append(self.disjunction())
        if self.at("op", ",") and function != "sha256":
            count = "two arguments" if function == "business_days" else "one argument"
            raise _invalid(f"{function}() takes {count}")
        self.expect(")", f"to close {function}()")
        self.leave()
        return Call(function, tuple(args))

    def reference(self, function: str) -> Node:
        parts = [self.bare_name(function, "the name of a field, a value, a time axis or an input")]
        while self.at("op", "."):
            self.advance()
            parts.append(self.member_name())
        return Name(tuple(parts))

    def bare_name(self, function: str, what: str) -> str:
        token = self.token
        if token.kind != "name" or token.text in KEYWORDS:
            raise _invalid(f"{function}() takes {what}, found {self.describe()}")
        self.advance()
        return token.text

    def position(self) -> Node:
        token = self.token
        value = token.value
        if token.kind != "num" or not isinstance(value, float) or not value.is_integer() or value < 1:
            raise _invalid(f"presented_in_top() takes a whole position from 1, found {self.describe()}")
        self.advance()
        return Lit(Value(Logic.YES, Kind.NUMBER, value))


def parse(text: str) -> Node:
    """The syntax tree of an expression, or `ExprError` (`expr_invalid`, `expr_limit`)."""
    node = _Parser(_tokens(text)).expression()
    _placement(node, inside_was=False)
    return node


def _placement(node: Node, inside_was: bool) -> None:
    """The rules a grammar alone does not say: where the logical words go, and what `was()` may hold."""
    match node:
        case LogicWord():
            raise _invalid("yes, no, unobserved and known_defect only appear in == or != comparisons")
        case Compare(op=op, left=left, right=right):
            words = [side for side in (left, right) if isinstance(side, LogicWord)]
            if words and op not in ("==", "!="):
                raise _invalid(f"a logical value compares only with == or !=, not {op}")
            if len(words) == 2:
                raise _invalid("a comparison needs a value on one side of the logical value")
            for side in (left, right):
                if not isinstance(side, LogicWord):
                    _placement(side, inside_was)
        case Call(function=function, args=args):
            if inside_was and function in ("was", "changed", "age", "observer"):
                raise _invalid(f"was() reads previous values: {function}() is not allowed inside it")
            for arg in args:
                _placement(arg, inside_was or function == "was")
        case Member(target=target):
            _placement(target, inside_was)
        case ListLit(items=items):
            for item in items:
                _placement(item, inside_was)
        case Not(operand=operand):
            _placement(operand, inside_was)
        case BoolOp(operands=operands):
            for operand in operands:
                _placement(operand, inside_was)
        case Arith(left=left, right=right):
            _placement(left, inside_was)
            _placement(right, inside_was)


# Evaluation


def evaluate(node: Node, env: Environment) -> Value:
    """The value of a parsed expression, or `ExprError` (`expr_type`, `expr_limit`). A business-day count or a
    quote is a step, never a result."""
    value = _Evaluator(env).eval(node, previous=False)
    if value.kind in (Kind.BUSINESS_DAYS, Kind.QUOTE):
        raise _type(f"an expression cannot end in a {value.kind.value.replace('_', ' ')}")
    return value


class _Evaluator:
    def __init__(self, env: Environment) -> None:
        self.env = env

    def eval(self, node: Node, previous: bool) -> Value:
        match node:
            case Lit(value=value):
                return value
            case Name():
                return self.name(node, previous)
            case Member(target=target, name=name):
                return self.member(self.eval(target, previous), name)
            case Call():
                return self.call(node, previous)
            case ListLit(items=items):
                return Value(Logic.YES, Kind.LIST, tuple(self.eval(item, previous) for item in items))
            case Not(operand=operand):
                truth = self.truth(self.eval(operand, previous), "not")
                return unknown(truth) if not truth.known else boolean(truth is Logic.NO)
            case BoolOp(op=op, operands=operands):
                return self.bool_op(op, operands, previous)
            case Compare():
                return self.compare(node, previous)
            case Arith(op=op, left=left, right=right):
                return _arith(op, self.eval(left, previous), self.eval(right, previous), self.env)
        raise _invalid("unknown syntax")

    def name(self, node: Name, previous: bool) -> Value:
        env, parts = self.env, node.parts
        if len(parts) == 1:
            word = parts[0]
            if word in META:
                return self.meta(word)
            slot = env.fields.get(word)
            if slot is not None:
                return _slot_value(slot, previous)
            if word in env.absent_names:
                return absent(word)
            return UNOBSERVED
        slot = env.inputs.get(node.dotted)
        if slot is not None:
            return _slot_value(slot, previous)
        if parts[0] == "config":
            return env.config.get(".".join(parts[1:]), ABSENT)
        owner = env.fields.get(parts[0])
        if owner is not None and parts[1:] == ("completeness",):
            if previous or owner.completeness is None:
                return ABSENT
            return Value(Logic.YES, Kind.STRING, owner.completeness)
        return UNOBSERVED

    def meta(self, word: str) -> Value:
        env = self.env
        if word == "watch_count":
            return Value(Logic.YES, Kind.NUMBER, float(env.watch_count))
        text = {"state": env.state, "derived_status": env.derived_status, "purpose": env.purpose}[word]
        return ABSENT if text is None else Value(Logic.YES, Kind.STRING, text)

    def slot(self, node: Node) -> Slot | None:
        assert isinstance(node, Name)
        if len(node.parts) == 1:
            return self.env.fields.get(node.parts[0])
        return self.env.inputs.get(node.dotted)

    def member(self, target: Value, name: str) -> Value:
        if not target.logic.known:
            return target
        if target.kind is not Kind.QUOTE:
            raise _type(f"only a quote has fields: '.{name}'")
        assert isinstance(target.datum, Mapping)
        slot = target.datum.get(name)
        return UNOBSERVED if slot is None else slot.value

    def call(self, node: Call, previous: bool) -> Value:
        env, args = self.env, node.args
        match node.function:
            case "now":
                return Value(Logic.YES, Kind.DATETIME, env.now)
            case "age":
                slot = self.slot(args[0])
                if slot is None or slot.at is None:
                    return UNOBSERVED
                return Value(Logic.YES, Kind.DURATION, max(0, env.now - slot.at))
            case "observer":
                slot = self.slot(args[0])
                if slot is None or slot.observer is None:
                    return ABSENT
                return Value(Logic.YES, Kind.STRING, slot.observer)
            case "changed":
                slot = self.slot(args[0])
                return boolean(slot is not None and slot.was is not None and slot.was != slot.value)
            case "was":
                return self.eval(args[0], previous=True)
            case "count":
                return self.count(self.eval(args[0], previous))
            case "business_days":
                return self.business_days(self.eval(args[0], previous), args[1])
            case "quote":
                assert isinstance(args[0], Name)
                quote = env.quotes.get(args[0].parts[0])
                return UNOBSERVED if quote is None else Value(Logic.YES, Kind.QUOTE, quote)
            case "presented_in_top":
                assert isinstance(args[0], Lit)
                assert isinstance(args[0].value.datum, float)
                rank = env.presented_rank
                return boolean(rank is not None and rank <= args[0].value.datum)
            case "sha256":
                return _sha256([self.eval(arg, previous) for arg in args])
        raise _invalid(f"unknown function {node.function!r}")

    def count(self, value: Value) -> Value:
        if not value.logic.known:
            return value
        if value.is_absent:
            return Value(Logic.YES, Kind.NUMBER, 0.0)
        if value.kind is not Kind.LIST:
            raise _type(f"count() takes a list, not a {_kind_name(value)}")
        return Value(Logic.YES, Kind.NUMBER, float(len(_items(value))))

    def business_days(self, count: Value, calendar: Node) -> Value:
        if not count.logic.known:
            return count
        if count.is_absent:
            return ABSENT
        n = count.datum
        if count.kind is not Kind.NUMBER or not isinstance(n, float) or not n.is_integer() or n < 0:
            raise _type("business_days() counts a whole number of days from 0")
        if n > MAX_BUSINESS_DAYS:
            raise _limit(f"business_days() counts at most {MAX_BUSINESS_DAYS} days")
        assert isinstance(calendar, Name)
        found = self.env.calendars.get(calendar.parts[0])
        if found is None:
            raise _type(f"unknown calendar {calendar.parts[0]!r}")
        if len(found.weekend) >= 7:
            raise _type(f"calendar {calendar.parts[0]!r} has no business day")
        return Value(Logic.YES, Kind.BUSINESS_DAYS, (int(n), found))

    def truth(self, value: Value, op: str) -> Logic:
        if not value.logic.known:
            return value.logic
        if value.kind is not Kind.BOOL:
            raise _type(f"{op} takes true or false, not a {_kind_name(value)}")
        return value.logic

    def bool_op(self, op: str, operands: tuple[Node, ...], previous: bool) -> Value:
        # Left to right, and the first operand that decides stops the evaluation: `false and x` never
        # evaluates `x`, so it never fails on it.
        decisive = Logic.NO if op == "and" else Logic.YES
        seen: list[Logic] = []
        for operand in operands:
            truth = self.truth(self.eval(operand, previous), op)
            if truth is decisive:
                return boolean(truth is Logic.YES)
            seen.append(truth)
        pending = unknown_of(*seen)
        return unknown(pending) if pending else boolean(decisive is Logic.NO)

    def compare(self, node: Compare, previous: bool) -> Value:
        op = node.op
        if isinstance(node.left, LogicWord) or isinstance(node.right, LogicWord):
            word, other = (
                (node.left, node.right) if isinstance(node.left, LogicWord) else (node.right, node.left)
            )
            assert isinstance(word, LogicWord)
            same = self.eval(other, previous).logic is word.logic
            return boolean(same if op == "==" else not same)
        left, right = self.eval(node.left, previous), self.eval(node.right, previous)
        pending = unknown_of(left.logic, right.logic)
        if pending:
            return unknown(pending)
        if op in ("==", "!="):
            equal = _equal(left, right, self.env)
            if not equal.known:
                return unknown(equal)
            return boolean((equal is Logic.YES) == (op == "=="))
        if op == "in":
            return _contains(right, left, self.env)
        if left.is_absent or right.is_absent:
            return FALSE
        order = _order(left, right, self.env)
        return boolean(
            {"<": order < 0, "<=": order <= 0, ">": order > 0, ">=": order >= 0}[op],
        )


def _slot_value(slot: Slot, previous: bool) -> Value:
    if not previous:
        return slot.value
    return UNOBSERVED if slot.was is None else slot.was


def _kind_name(value: Value) -> str:
    if value.is_absent:
        return "absent value"
    return (value.kind or Kind.STRING).value.replace("_", " ")


def _items(value: Value) -> tuple[Value, ...]:
    assert isinstance(value.datum, tuple)
    if len(value.datum) > MAX_LIST:
        raise _limit(f"a list has at most {MAX_LIST} items")
    return value.datum


def _day_start(day: date, env: Environment) -> int:
    """A date where an instant is needed: the start of that day at the environment's UTC offset."""
    return (day.toordinal() - _EPOCH_ORDINAL) * DAY_MS - env.utc_offset_min * MINUTE_MS


_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_MIN_MS = (date(1, 1, 1).toordinal() - _EPOCH_ORDINAL) * DAY_MS
_MAX_MS = (date(9999, 12, 31).toordinal() + 1 - _EPOCH_ORDINAL) * DAY_MS - 1


def _instant(ms: int) -> Value:
    if not _MIN_MS <= ms <= _MAX_MS:
        raise _type("a time outside the years 1 to 9999")
    return Value(Logic.YES, Kind.DATETIME, ms)


def _as_ms(value: Value, env: Environment) -> int | None:
    if value.kind is Kind.DATETIME:
        assert isinstance(value.datum, int)
        return value.datum
    if value.kind is Kind.DATE:
        assert isinstance(value.datum, date)
        return _day_start(value.datum, env)
    return None


def _arith(op: str, left: Value, right: Value, env: Environment) -> Value:
    pending = unknown_of(left.logic, right.logic)
    if pending:
        return unknown(pending)
    if left.is_absent or right.is_absent:
        return ABSENT
    kinds = (left.kind, right.kind)
    a, b = left.datum, right.datum
    if kinds == (Kind.NUMBER, Kind.NUMBER):
        assert isinstance(a, float)
        assert isinstance(b, float)
        return Value(Logic.YES, Kind.NUMBER, a + b if op == "+" else a - b)
    if kinds == (Kind.DURATION, Kind.DURATION):
        assert isinstance(a, int)
        assert isinstance(b, int)
        return Value(Logic.YES, Kind.DURATION, a + b if op == "+" else a - b)
    if op == "+" and left.kind is Kind.DURATION and right.kind in (Kind.DATE, Kind.DATETIME):
        return _arith("+", right, left, env)
    if op == "+" and left.kind is Kind.BUSINESS_DAYS and right.kind is Kind.DATE:
        return _arith("+", right, left, env)
    if left.kind in (Kind.DATE, Kind.DATETIME) and right.kind is Kind.DURATION:
        start = _as_ms(left, env)
        assert start is not None
        assert isinstance(b, int)
        return _instant(start + b if op == "+" else start - b)
    if left.kind is Kind.DATE and right.kind is Kind.BUSINESS_DAYS:
        assert isinstance(a, date)
        assert isinstance(b, tuple)
        return Value(Logic.YES, Kind.DATE, _shift_business_days(a, b[0], b[1], 1 if op == "+" else -1))
    if op == "-" and left.kind in (Kind.DATE, Kind.DATETIME) and right.kind in (Kind.DATE, Kind.DATETIME):
        start, end = _as_ms(right, env), _as_ms(left, env)
        assert start is not None
        assert end is not None
        return Value(Logic.YES, Kind.DURATION, end - start)
    raise _type(f"{op} does not apply to a {_kind_name(left)} and a {_kind_name(right)}")


def _shift_business_days(day: date, count: int, calendar: Calendar, step: int) -> date:
    """The `count`-th business day after `day` (before it, for a negative step); `day` itself for 0."""
    moved = 0
    try:
        while moved < count:
            day += timedelta(days=step)
            if day.isoweekday() not in calendar.weekend and day not in calendar.holidays:
                moved += 1
    except OverflowError:
        raise _type("a date outside the years 1 to 9999") from None
    return day


def _equal(left: Value, right: Value, env: Environment) -> Logic:
    """Equality of two known values: absences are equal when their names agree (`none` is any absence)."""
    if left.is_absent or right.is_absent:
        if not (left.is_absent and right.is_absent):
            return Logic.NO
        agree = left.absent is None or right.absent is None or left.absent == right.absent
        return Logic.YES if agree else Logic.NO
    if left.kind is Kind.LIST and right.kind is Kind.LIST:
        a, b = _items(left), _items(right)
        if len(a) != len(b):
            return Logic.NO
        results: list[Logic] = []
        for x, y in zip(a, b, strict=True):
            pending = unknown_of(x.logic, y.logic)
            results.append(pending if pending else _equal(x, y, env))
        if Logic.NO in results:
            return Logic.NO
        return unknown_of(*results) or Logic.YES
    if left.kind is Kind.BOOL and right.kind is Kind.BOOL:
        return Logic.YES if left.datum == right.datum else Logic.NO
    return Logic.YES if _order(left, right, env) == 0 else Logic.NO


def _order(left: Value, right: Value, env: Environment) -> int:
    a: object
    b: object
    if {left.kind, right.kind} == {Kind.DATE, Kind.DATETIME}:
        a, b = _as_ms(left, env), _as_ms(right, env)
    elif left.kind is right.kind and left.kind in _ORDERED:
        a, b = left.datum, right.datum
    else:
        raise _type(f"cannot compare a {_kind_name(left)} with a {_kind_name(right)}")
    # Strings order by Unicode code point, which Python's comparison already is.
    return int((a > b) - (a < b))  # type: ignore[operator]


_ORDERED = frozenset({Kind.NUMBER, Kind.STRING, Kind.DURATION, Kind.DATE, Kind.DATETIME})


def _contains(container: Value, item: Value, env: Environment) -> Value:
    if container.is_absent or item.is_absent:
        return FALSE
    if container.kind is not Kind.LIST:
        raise _type(f"in takes a list on its right, not a {_kind_name(container)}")
    pending: list[Logic] = []
    for element in _items(container):
        if not element.logic.known:
            pending.append(element.logic)
            continue
        found = _equal(item, element, env)
        if found is Logic.YES:
            return TRUE
        if not found.known:
            pending.append(found)
    unresolved = unknown_of(*pending)
    return unknown(unresolved) if unresolved else FALSE


def _sha256(values: list[Value]) -> Value:
    pending = unknown_of(*(value.logic for value in values))
    if pending:
        return unknown(pending)
    if any(value.is_absent for value in values):
        return ABSENT
    text = "\x1f".join(_canonical(value) for value in values)
    return Value(Logic.YES, Kind.STRING, "sha256:" + hashlib.sha256(text.encode()).hexdigest())


def _canonical(value: Value) -> str:
    datum = value.datum
    match value.kind:
        case Kind.STRING:
            assert isinstance(datum, str)
            return datum
        case Kind.BOOL:
            return "true" if datum else "false"
        case Kind.NUMBER:
            assert isinstance(datum, float)
            if not datum.is_integer() or abs(datum) > MAX_EXACT_INTEGER:
                raise _type("sha256() takes whole numbers up to 2^53 - 1")
            return str(int(datum))
        case Kind.DURATION:
            return str(datum)
        case Kind.DATE:
            assert isinstance(datum, date)
            return datum.isoformat()
        case Kind.DATETIME:
            assert isinstance(datum, int)
            return format_datetime(datum)
    raise _type(f"sha256() does not take a {_kind_name(value)}")


def format_datetime(ms: int) -> str:
    """The canonical form of an instant: UTC, with milliseconds (`2026-09-29T12:00:00.000Z`)."""
    # `isoformat`, not `strftime`: the C library's `%Y` does not pad a year below 1000 on every platform.
    moment = datetime(1970, 1, 1) + timedelta(milliseconds=ms)
    return moment.isoformat(timespec="milliseconds") + "Z"


def epoch_ms(moment: datetime) -> int:
    """An aware datetime as epoch milliseconds; digits below the millisecond are dropped."""
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * DAY_MS) + delta.seconds * SECOND_MS + delta.microseconds // 1000


# Resolution against a type declaration


@dataclass(frozen=True, slots=True)
class Scope:
    """What a type declares, as the names of its expressions resolve: fields with their kind, which fields
    have completeness levels, computed values, time axes, absence names, inputs, states and sources."""

    fields: Mapping[str, Kind | None] = field(default_factory=dict)
    completeness: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    axes: frozenset[str] = frozenset()
    absent_names: frozenset[str] = frozenset()
    inputs: frozenset[str] = frozenset()
    states: frozenset[str] = frozenset()
    sources: Mapping[str, str] = field(default_factory=dict)
    """Source name -> its kind (`pull`, `quote`...)."""


class Expect(_StrEnum):
    ANY = "any"
    CONDITION = "condition"
    TIME = "time"


def compile_expression(text: str, scope: Scope, expect: Expect = Expect.ANY) -> Node:
    """Parses and resolves an expression of a type declaration; `expr_invalid` names what is wrong."""
    node = parse(text)
    kind = _Checker(scope).kind(node)
    if kind in (Kind.BUSINESS_DAYS, Kind.QUOTE):
        raise _invalid(f"an expression cannot end in a {kind.value.replace('_', ' ')}")
    if expect is Expect.CONDITION and kind not in (None, Kind.BOOL):
        raise _invalid(f"a condition must be true or false, and this gives a {kind.value}")
    if expect is Expect.TIME and kind not in (None, Kind.DATE, Kind.DATETIME):
        raise _invalid(f"a time must be a date or a datetime, and this gives a {kind.value}")
    return node


_ARITH: dict[tuple[str, Kind, Kind], Kind] = {
    ("+", Kind.NUMBER, Kind.NUMBER): Kind.NUMBER,
    ("-", Kind.NUMBER, Kind.NUMBER): Kind.NUMBER,
    ("+", Kind.DURATION, Kind.DURATION): Kind.DURATION,
    ("-", Kind.DURATION, Kind.DURATION): Kind.DURATION,
    ("+", Kind.DATETIME, Kind.DURATION): Kind.DATETIME,
    ("-", Kind.DATETIME, Kind.DURATION): Kind.DATETIME,
    ("+", Kind.DURATION, Kind.DATETIME): Kind.DATETIME,
    ("+", Kind.DATE, Kind.DURATION): Kind.DATETIME,
    ("-", Kind.DATE, Kind.DURATION): Kind.DATETIME,
    ("+", Kind.DURATION, Kind.DATE): Kind.DATETIME,
    ("+", Kind.DATE, Kind.BUSINESS_DAYS): Kind.DATE,
    ("-", Kind.DATE, Kind.BUSINESS_DAYS): Kind.DATE,
    ("+", Kind.BUSINESS_DAYS, Kind.DATE): Kind.DATE,
    ("-", Kind.DATE, Kind.DATE): Kind.DURATION,
    ("-", Kind.DATETIME, Kind.DATETIME): Kind.DURATION,
    ("-", Kind.DATE, Kind.DATETIME): Kind.DURATION,
    ("-", Kind.DATETIME, Kind.DATE): Kind.DURATION,
}


class _Checker:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def kind(self, node: Node) -> Kind | None:
        match node:
            case Lit(value=value):
                return value.kind
            case Name():
                return self.name(node)
            case Member(target=target, name=name):
                if self.kind(target) is not Kind.QUOTE:
                    raise _invalid(f"only quote() has fields: '.{name}'")
                return None
            case Call():
                return self.call(node)
            case ListLit(items=items):
                for item in items:
                    self.kind(item)
                return Kind.LIST
            case Not(operand=operand):
                self.condition(operand, "not")
                return Kind.BOOL
            case BoolOp(op=op, operands=operands):
                for operand in operands:
                    self.condition(operand, op)
                return Kind.BOOL
            case Compare():
                return self.compare(node)
            case Arith(op=op, left=left, right=right):
                return self.arith(op, self.kind(left), self.kind(right))
        raise _invalid("unknown syntax")

    def name(self, node: Name) -> Kind | None:
        scope, parts = self.scope, node.parts
        if len(parts) == 1:
            word = parts[0]
            if word in META:
                return META[word]
            if word in scope.fields:
                return scope.fields[word]
            if word in scope.values or word in scope.axes or word in scope.absent_names:
                return None
            if word in scope.states:
                raise _invalid(f"{word!r} is a state: write it as a string, '{word}'")
            if word in scope.sources:
                raise _invalid(f"{word!r} is a source: only quote() takes a source")
            if word == "config":
                raise _invalid("config takes a key: config.<key>")
            return None  # a field the type observes without declaring it
        if node.dotted in scope.inputs or parts[0] == "config":
            return None
        if len(parts) == 2 and parts[1] == "completeness" and parts[0] in scope.fields:
            if parts[0] not in scope.completeness:
                raise _invalid(f"field {parts[0]!r} declares no completeness levels")
            return Kind.STRING
        raise _invalid(
            f"unknown name {node.dotted!r}: not an input of the type, config.<key> or <field>.completeness"
        )

    def reference(self, function: str, node: Node) -> None:
        assert isinstance(node, Name)
        scope, word = self.scope, node.parts[0]
        if len(node.parts) > 1:
            fits = node.dotted in scope.inputs
        else:
            fits = not (
                word in META or word in scope.absent_names or word in scope.states or word in scope.sources
            )
        if not fits:
            raise _invalid(f"{function}() takes a field, a value, a time axis or an input: {node.dotted!r}")

    def call(self, node: Call) -> Kind | None:
        function, args = node.function, node.args
        match function:
            case "age" | "observer" | "changed":
                self.reference(function, args[0])
                return {"age": Kind.DURATION, "observer": Kind.STRING, "changed": Kind.BOOL}[function]
            case "quote":
                assert isinstance(args[0], Name)
                source = args[0].parts[0]
                kind = self.scope.sources.get(source)
                if kind is None:
                    raise _invalid(f"quote() names an unknown source {source!r}")
                if kind != "quote":
                    raise _invalid(f"quote() takes a source of kind quote, and {source!r} is {kind}")
                return Kind.QUOTE
            case "business_days":
                if self.kind(args[0]) not in (None, Kind.NUMBER):
                    raise _invalid("business_days() counts a number of days")
                return Kind.BUSINESS_DAYS
            case "count":
                if self.kind(args[0]) not in (None, Kind.LIST):
                    raise _invalid("count() takes a list")
                return Kind.NUMBER
            case "sha256":
                if any(self.kind(arg) in (Kind.LIST, Kind.BUSINESS_DAYS, Kind.QUOTE) for arg in args):
                    raise _invalid("sha256() takes strings, numbers, booleans, durations and times")
                return Kind.STRING
            case "was":
                return self.kind(args[0])
            case "now":
                return Kind.DATETIME
        return Kind.BOOL  # presented_in_top

    def condition(self, node: Node, op: str) -> None:
        kind = self.kind(node)
        if kind not in (None, Kind.BOOL):
            raise _invalid(f"{op} takes conditions, and one side gives a {kind.value}")

    def compare(self, node: Compare) -> Kind:
        sides = [side for side in (node.left, node.right) if not isinstance(side, LogicWord)]
        kinds = [self.kind(side) for side in sides]
        if len(kinds) == 2 and kinds[0] is not None and kinds[1] is not None:
            left, right = kinds[0], kinds[1]
            if node.op == "in":
                if right is not Kind.LIST:
                    raise _invalid(f"in takes a list on its right, and this is a {right.value}")
            elif not _comparable(node.op, left, right):
                raise _invalid(f"{node.op} compares a {left.value} with a {right.value}")
        return Kind.BOOL

    def arith(self, op: str, left: Kind | None, right: Kind | None) -> Kind | None:
        if left is None or right is None:
            return None
        result = _ARITH.get((op, left, right))
        if result is None:
            raise _invalid(f"{op} does not apply to a {left.value} and a {right.value}")
        return result


def _comparable(op: str, left: Kind, right: Kind) -> bool:
    if left is right:
        return left in _ORDERED or (op in ("==", "!=") and left in (Kind.BOOL, Kind.LIST))
    return {left, right} == {Kind.DATE, Kind.DATETIME}


def duration_ms(text: str) -> int:
    """A duration literal (`30s`, `10min`, `24h`, `7d`) in milliseconds, as the declarations write them."""
    tokens = _tokens(text)
    if len(tokens) != 2 or tokens[0].kind != "dur":
        raise _invalid(f"{text!r} is not a duration: a whole number and ms, s, min, h or d")
    assert isinstance(tokens[0].value, int)
    return tokens[0].value
