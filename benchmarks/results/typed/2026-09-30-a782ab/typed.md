# Typed-object set 2026-09-30-a782ab

- Where: `local-cell`; 6 cases, 3 repetition(s); SDK 0.7.0 (source release).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer. The state lines say derived fields: no.
- Validity rule (right with the whole history, wrong with no memory): 18 of 18 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 55.6% (10/18) [34, 75] | 55.6% (10/18) [34, 75] | 38.9% (7/18) [20, 61] | 100.0% (18/18) [82, 100] |
| all | 55.6% (10/18) [34, 75] | 55.6% (10/18) [34, 75] | 38.9% (7/18) [20, 61] | 100.0% (18/18) [82, 100] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 55.6% (10/18) [34, 75] | 55.6% (10/18) [34, 75] | 38.9% (7/18) [20, 61] | 100.0% (18/18) [82, 100] |
| all | 55.6% (10/18) [34, 75] | 55.6% (10/18) [34, 75] | 38.9% (7/18) [20, 61] | 100.0% (18/18) [82, 100] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| chat | 18 | 94.5 | 94.5 | 0.0 (0) | 0.0 | 845.0 |

Added tokens by category (median): composite 0.0, all 0.0.

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 0 | none | none | 0 | 0 |
| with | 0 | none | none | 0 | 0 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 18 | 18 | 0 |
| with | 18 | 18 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 18 of 18 served at the level proved, 0 with an item withheld, 0 with the sensitive value; with 18 of 18 served at the level proved, 0 with an item withheld, 0 with the sensitive value.

## Cases that changed verdict (judge)

- none

## Cost

- Agent and judge: US$ 0.0059 (144 calls).
- The cell's models (ledger): US$ 0.0026.
