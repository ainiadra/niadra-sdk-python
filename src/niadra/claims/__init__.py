"""The claim contract's checker (`spec/claim-contract.md`), pure and without a model: the number and role
parser, the hedges (a number the output does not assert), the category detection, the natures, verdicts and
actions, and the text anchor. It runs in the agent's process, on each output, with the categories of the
contract the SDK profile serves (`ClaimContractSummary`), and passes the spec's vectors
(`spec/vectors/claim-*.v0.json`).

```python
from niadra.claims import Output, Turn, check

findings = check(contract.categories, Output(text, "pt", "chat", immutable=False), Turn(values=values))
```
"""

from niadra.claims.anchor import normalize, score
from niadra.claims.check import (
    PATTERNS,
    REWRITABLE,
    STANDING,
    ActionsSpec,
    Anchor,
    AnchorEvidenceSpec,
    CategorySpec,
    DetectSpec,
    EvidenceSpec,
    Finding,
    NaturesSpec,
    Output,
    Turn,
    TurnValue,
    ValueEvidenceSpec,
    check,
    detected,
    nature_of,
    same_value,
)
from niadra.claims.hedges import hedged
from niadra.claims.numbers import LANGUAGES, Mention, mentions
from niadra.claims.roles import WINDOW, Role, roles_of
from niadra.claims.text import fold

__all__ = [
    "LANGUAGES",
    "PATTERNS",
    "REWRITABLE",
    "STANDING",
    "WINDOW",
    "ActionsSpec",
    "Anchor",
    "AnchorEvidenceSpec",
    "CategorySpec",
    "DetectSpec",
    "EvidenceSpec",
    "Finding",
    "Mention",
    "NaturesSpec",
    "Output",
    "Role",
    "Turn",
    "TurnValue",
    "ValueEvidenceSpec",
    "check",
    "detected",
    "fold",
    "hedged",
    "mentions",
    "nature_of",
    "normalize",
    "roles_of",
    "same_value",
    "score",
]
