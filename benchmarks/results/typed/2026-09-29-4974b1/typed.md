# Typed-object set 2026-09-29-4974b1

- Where: `local-cell`; 42 cases, 3 repetition(s); SDK 0.6.1 (source 8583af4).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 85 of 126 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 50.0% (9/18) [29, 71] | 83.3% (15/18) [61, 94] | 50.0% (9/18) [29, 71] | 50.0% (9/18) [29, 71] |
| quote_expiry | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 55.6% (10/18) [34, 75] |
| deadline_revision | 77.8% (14/18) [55, 91] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 100.0% (18/18) [82, 100] | 88.9% (16/18) [67, 97] | 100.0% (18/18) [82, 100] | 83.3% (15/18) [61, 94] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 22.2% (4/18) [9, 45] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 88.9% (16/18) [67, 97] |
| all | 35.7% (45/126) [28, 44] | 58.7% (74/126) [50, 67] | 21.4% (27/126) [15, 29] | 82.5% (104/126) [75, 88] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 83.3% (15/18) [61, 94] | 100.0% (18/18) [82, 100] | 100.0% (18/18) [82, 100] | 77.8% (14/18) [55, 91] |
| quote_expiry | 0.0% (0/18) [0, 18] | 66.7% (12/18) [44, 84] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] |
| deadline_revision | 72.2% (13/18) [49, 88] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 100.0% (18/18) [82, 100] | 83.3% (15/18) [61, 94] | 88.9% (16/18) [67, 97] | 83.3% (15/18) [61, 94] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 22.2% (4/18) [9, 45] | 22.2% (4/18) [9, 45] | 0.0% (0/18) [0, 18] | 72.2% (13/18) [49, 88] |
| all | 41.3% (52/126) [33, 50] | 53.2% (67/126) [44, 62] | 27.0% (34/126) [20, 35] | 73.8% (93/126) [66, 81] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 40.0% (2/5) [12, 77] | 80.0% (4/5) [38, 96] | 0.0% (0/5) [0, 43] | 100.0% (5/5) [57, 100] |
| quote_expiry | 0.0% (0/10) [0, 28] | 100.0% (10/10) [72, 100] | 0.0% (0/10) [0, 28] | 100.0% (10/10) [72, 100] |
| deadline_revision | 77.8% (14/18) [55, 91] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 25.0% (4/16) [10, 50] | 37.5% (6/16) [18, 61] | 0.0% (0/16) [0, 19] | 100.0% (16/16) [81, 100] |
| all | 23.5% (20/85) [16, 34] | 44.7% (38/85) [35, 55] | 0.0% (0/85) [0, 4] | 100.0% (85/85) [96, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (5/5) [57, 100] | 100.0% (5/5) [57, 100] | 100.0% (5/5) [57, 100] | 100.0% (5/5) [57, 100] |
| quote_expiry | 0.0% (0/10) [0, 28] | 80.0% (8/10) [49, 94] | 0.0% (0/10) [0, 28] | 0.0% (0/10) [0, 28] |
| deadline_revision | 72.2% (13/18) [49, 88] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 25.0% (4/16) [10, 50] | 25.0% (4/16) [10, 50] | 0.0% (0/16) [0, 19] | 81.2% (13/16) [57, 93] |
| all | 28.2% (24/85) [20, 39] | 41.2% (35/85) [31, 52] | 5.9% (5/85) [3, 13] | 81.2% (69/85) [72, 88] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 72.0 | 79.0 | 0.0 (68) | 0.0 | 426.0 |
| chat | 90 | 139.5 | 188.0 | 52.0 (161) | 52.0 | 783.0 |

Added tokens by category (median): price_freshness 42.0, quote_expiry 52.0, deadline_revision 148.5, not_checked 152.5, changes_since_seen 0.0, hard_constraint 29.0, effect_once 0.0, all 42.0.

## A turn that asks for no block (dataset v2 sample, 20 cases)

| View | median | p95 |
|---|---|---|
| voice | 91.5 | 136 |
| chat | 88.0 | 152 |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 42 | unsupported 42 | block 5, warn 37 | 40 | 10 |
| with | 49 | matched 8, no_evidence 19, stale 15, unsupported 7 | block 7, none 8, warn 34 | 40 | 31 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 126 | 126 | 0 |
| with | 126 | 126 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 126 of 126 served at the level proved, 23 with an item withheld, 0 with the sensitive value; with 126 of 126 served at the level proved, 23 with an item withheld, 0 with the sensitive value.

Coordination: 18 of 18 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-001` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-012` (not_checked): without True, with False (not valid)
- `typed-pt-019` (effect_once): without True, with False
- `typed-pt-021` (effect_once): without False, with True
- `typed-en-002` (price_freshness): without False, with True
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-020` (effect_once): without False, with True (not valid)
- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-008` (deadline_revision): without False, with True
- `typed-pt-020` (effect_once): without True, with False
- `typed-pt-021` (effect_once): without False, with True
- `typed-en-003` (price_freshness): without False, with True
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-008` (deadline_revision): without False, with True
- `typed-en-010` (not_checked): without True, with False (not valid)
- `typed-pt-001` (price_freshness): without True, with False (not valid)
- `typed-pt-002` (price_freshness): without False, with True (not valid)
- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-019` (effect_once): without False, with True
- `typed-pt-020` (effect_once): without False, with True
- `typed-en-003` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True

## Cost

- Agent and judge: US$ 0.0465 (1008 calls).
- The cell's models (ledger): US$ 0.0252.
