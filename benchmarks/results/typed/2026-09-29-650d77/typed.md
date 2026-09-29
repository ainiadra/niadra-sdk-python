# Typed-object set 2026-09-29-650d77

- Where: `local-cell`; 12 cases, 3 repetition(s); SDK 0.6.1 (source b198e14).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 36 of 36 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 0.0% (0/36) [0, 10] | 5.6% (2/36) [2, 18] | 0.0% (0/36) [0, 10] | 100.0% (36/36) [90, 100] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 11.1% (2/18) [3, 33] | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 5.6% (2/36) [2, 18] | 5.6% (2/36) [2, 18] | 0.0% (0/36) [0, 10] | 91.7% (33/36) [78, 97] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 0.0% (0/36) [0, 10] | 5.6% (2/36) [2, 18] | 0.0% (0/36) [0, 10] | 100.0% (36/36) [90, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 11.1% (2/18) [3, 33] | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 5.6% (2/36) [2, 18] | 5.6% (2/36) [2, 18] | 0.0% (0/36) [0, 10] | 91.7% (33/36) [78, 97] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 77.0 | 78.5 | 0.0 (35) | 0.0 | 425.5 |

Added tokens by category (median): changes_since_seen 0.0, hard_constraint 33.0, all 0.0.

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 5 | unsupported 5 | block 5 | 4 | 0 |
| with | 4 | unsupported 4 | block 4 | 3 | 1 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 36 | 36 | 0 |
| with | 36 | 36 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 36 of 36 served at the level proved, 24 with an item withheld, 0 with the sensitive value; with 36 of 36 served at the level proved, 24 with an item withheld, 0 with the sensitive value.

## Cases that changed verdict (judge)

- `typed-en-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True

## Cost

- Agent and judge: US$ 0.0111 (288 calls).
- The cell's models (ledger): US$ 0.0047.
