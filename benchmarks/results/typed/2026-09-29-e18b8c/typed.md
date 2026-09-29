# Typed-object set 2026-09-29-e18b8c

- Where: `local-cell`; 1 cases, 1 repetition(s); SDK 0.6.1 (source b198e14).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 1 of 1 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| effect_once | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |
| all | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| effect_once | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |
| all | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| effect_once | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |
| all | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| effect_once | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |
| all | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| chat | 1 | 119 | 119 | 0 (0) | 0 | 428 |

Added tokens by category (median): effect_once 0, all 0.

## A turn that asks for no block (dataset v2 sample)

| View | median | p95 | today | within 5% |
|---|---|---|---|---|
| voice | 83.0 | 142 | 89 | no |
| chat | 83.0 | 165 | 98 | no |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 0 | none | none | 0 | 0 |
| with | 0 | none | none | 0 | 0 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 1 | 1 | 0 |
| with | 1 | 1 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 1 of 1 served at the level proved, 0 with an item withheld, 0 with the sensitive value; with 1 of 1 served at the level proved, 0 with an item withheld, 0 with the sensitive value.

Coordination: 1 of 1 second attempts refused as done.

## Cases that changed verdict (judge)

- none

## Cost

- Agent and judge: US$ 0.0003 (8 calls).
- The cell's models (ledger): US$ 0.0197.
