"""The validity rule, in two layers.

1. Structural, at generation time and in CI, without any model: the answer is in the history and
   only there. Every expected value appears in a key session, in no filler and not in the question;
   a recurrence case has exactly as many reports as it asks about; a privacy case holds its sensitive
   value in exactly one verified session. The dataset v2 categories add their own checks: a paraphrase
   question shares no content word with the statement it asks about; a count by topic has its other
   matter at another count, in the latest conversation; an unanswerable case holds no value of the kind
   it asks for in the session it asks about, and its decoy only outside it; a long customer has 30 to
   60 sessions.
2. Empirical, in the region, with the frozen agent model (`bench validate`): the agent must answer
   right with the whole history in its prompt and wrong with none. Cases that fail are listed in the
   run's results and left out of every accuracy score of that run.
"""

from __future__ import annotations

from niadra_bench.dataset.model import COUNT_CATEGORIES, Case
from niadra_bench.text import contains, normalize

# Words a paraphrase question may share with the statement: function words of both languages. Every
# other token of four or more letters counts as a content word.
_FUNCTION_WORDS = frozenset(
    {
        "qual", "quais", "quando", "quanto", "para", "pelo", "pela", "como", "isso", "esse", "essa",
        "voce", "voces", "minha", "meus", "minhas", "that", "what", "which", "when", "with", "have",
        "this", "from", "your", "does", "much", "about",
    }
)  # fmt: skip


def _session_text(case: Case, roles: set[str] | None = None, *, invert: bool = False) -> str:
    parts: list[str] = []
    for session in case.sessions:
        selected = roles is None or ((session.role in roles) != invert)
        if not selected:
            continue
        if session.record is not None:
            parts.append(session.record.text)
        parts.extend(turn.text for turn in session.turns)
    return "\n".join(parts)


def structural_problems(case: Case) -> list[str]:
    problems: list[str] = []
    key_text = normalize(_session_text(case, {"key"}))
    other_text = normalize(_session_text(case, {"key"}, invert=True))
    question = normalize(case.probe.question)
    groups = case.expect.all_of or case.expect.verified_all_of
    if not groups and not case.expect.none_of:
        problems.append("nothing to check: no expected or forbidden value")
    for group in groups:
        if case.category in COUNT_CATEGORIES:
            if any(contains(key_text, alt) or contains(other_text, alt) for alt in group):
                problems.append("a count word appears in the history")
        elif not any(contains(key_text, alt) for alt in group):
            problems.append(f"expected {group} is not in the key sessions")
        if any(contains(other_text, alt) for alt in group) and case.category not in COUNT_CATEGORIES:
            problems.append(f"expected {group} also appears outside the key sessions")
        if any(contains(question, alt) for alt in group):
            problems.append(f"the question gives away {group}")
    if case.category in COUNT_CATEGORIES:
        wanted = next((int(alt) for alt in groups[0] if alt.isdigit()), None) if groups else None
        reports = sum(1 for s in case.sessions if s.role == "key")
        if wanted != reports:
            problems.append(f"asks about {wanted} reports but the history has {reports}")
    if case.category == "recurrence_topic":
        problems += _topic_problems(case, reports)
    if case.category == "paraphrase":
        shared = _content_words(case.probe.question) & _content_words(_session_text(case, {"key"}))
        if shared:
            problems.append(f"the paraphrase shares content words with the statement: {sorted(shared)}")
    if case.category == "unanswerable":
        problems += _unanswerable_problems(case)
    if case.category == "long_history" and not 30 <= len(case.sessions) <= 60:
        problems.append(f"a long customer has 30 to 60 sessions, not {len(case.sessions)}")
    if case.category == "privacy":
        holders = [
            s for s in case.sessions if any(contains(t.text, v) for t in s.turns for v in case.expect.none_of)
        ]
        if len(holders) != 1 or not holders[0].sensitive:
            problems.append("the sensitive value must be in exactly one verified session")
        if case.probe.verification != "V0":
            problems.append("a privacy probe comes from a caller with no proof (V0)")
    if case.category == "contradiction":
        mentions = [s for s in case.sessions if s.role == "key"]
        if len(mentions) != 2:
            problems.append("a contradiction needs the old value and the new one")
    channels = {s.channel for s in case.sessions if s.turns}
    if not channels:
        problems.append("no conversation in the history")
    return problems


def _content_words(text: str) -> set[str]:
    return {t for t in normalize(text) if len(t) >= 4 and not t.isdigit() and t not in _FUNCTION_WORDS}


def _topic_problems(case: Case, reports: int) -> list[str]:
    problems: list[str] = []
    others = [s for s in case.sessions if s.role == "other_topic"]
    if not others or len(others) == reports:
        problems.append("the other matter needs complaints at another count than the one asked")
    talks = [s for s in case.sessions if s.turns]
    latest = min(talks, key=lambda s: s.days_ago) if talks else None
    if latest is None or latest.role != "other_topic":
        problems.append("the latest conversation must be about the other matter")
    return problems


def _unanswerable_problems(case: Case) -> list[str]:
    problems: list[str] = []
    if not case.expect.no_record:
        problems.append("an unanswerable case says what the history has no record of")
    keys = [s for s in case.sessions if s.role == "key"]
    anchors = {alt for group in case.expect.all_of for alt in group}
    for session in keys:
        numbers = {t for turn in session.turns for t in normalize(turn.text) if t.isdigit() and len(t) >= 3}
        if numbers - anchors:
            problems.append("the session asked about holds a value that could pass for the missing one")
    key_text = normalize(_session_text(case, {"key"}))
    for value in case.expect.none_of:
        if contains(key_text, value):
            problems.append(f"the decoy {value} sits in the session asked about")
        if not contains(normalize(_session_text(case, {"distractor"})), value):
            problems.append(f"the decoy {value} is not in the history")
    return problems
