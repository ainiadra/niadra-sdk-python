"""The validity rule, in two layers.

1. Structural, at generation time and in CI, without any model: the answer is in the history and
   only there. Every expected value appears in a key session, in no filler and not in the question;
   a recurrence case has exactly as many reports as it asks about; a privacy case holds its sensitive
   value in exactly one verified session.
2. Empirical, in the region, with the frozen agent model (`bench validate`): the agent must answer
   right with the whole history in its prompt and wrong with none. Cases that fail are listed in the
   run's results and left out of every accuracy score of that run.
"""

from __future__ import annotations

from niadra_bench.dataset.model import Case
from niadra_bench.text import contains, normalize


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
        if case.category == "recurrence":
            if any(contains(key_text, alt) or contains(other_text, alt) for alt in group):
                problems.append("a count word appears in the history")
        elif not any(contains(key_text, alt) for alt in group):
            problems.append(f"expected {group} is not in the key sessions")
        if any(contains(other_text, alt) for alt in group) and case.category != "recurrence":
            problems.append(f"expected {group} also appears outside the key sessions")
        if any(contains(question, alt) for alt in group):
            problems.append(f"the question gives away {group}")
    if case.category == "recurrence":
        wanted = next((int(alt) for alt in groups[0] if alt.isdigit()), None) if groups else None
        reports = sum(1 for s in case.sessions if s.role == "key")
        if wanted != reports:
            problems.append(f"asks about {wanted} reports but the history has {reports}")
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
