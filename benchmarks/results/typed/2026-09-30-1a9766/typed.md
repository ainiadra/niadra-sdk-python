# Typed-object set 2026-09-30-1a9766

- Where: `local-cell`; 42 cases, 3 repetition(s); SDK 0.7.0 (source dfb7f8c).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 88 of 126 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 61.1% (11/18) [39, 80] | 38.9% (7/18) [20, 61] | 50.0% (9/18) [29, 71] | 55.6% (10/18) [34, 75] |
| quote_expiry | 0.0% (0/18) [0, 18] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 61.1% (11/18) [39, 80] |
| deadline_revision | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 94.4% (17/18) [74, 99] | 94.4% (17/18) [74, 99] | 100.0% (18/18) [82, 100] | 100.0% (18/18) [82, 100] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 44.4% (8/18) [25, 66] | 66.7% (12/18) [44, 84] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 36.5% (46/126) [29, 45] | 83.3% (105/126) [76, 89] | 21.4% (27/126) [15, 29] | 88.1% (111/126) [81, 93] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 83.3% (15/18) [61, 94] | 94.4% (17/18) [74, 99] | 94.4% (17/18) [74, 99] | 88.9% (16/18) [67, 97] |
| quote_expiry | 0.0% (0/18) [0, 18] | 55.6% (10/18) [34, 75] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] |
| deadline_revision | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 88.9% (16/18) [67, 97] | 100.0% (18/18) [82, 100] | 94.4% (17/18) [74, 99] | 100.0% (18/18) [82, 100] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 77.8% (14/18) [55, 91] |
| hard_constraint | 11.1% (2/18) [3, 33] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 27.8% (5/18) [12, 51] | 22.2% (4/18) [9, 45] | 0.0% (0/18) [0, 18] | 77.8% (14/18) [55, 91] |
| all | 38.1% (48/126) [30, 47] | 80.2% (101/126) [72, 86] | 27.0% (34/126) [20, 35] | 77.8% (98/126) [70, 84] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 60.0% (3/5) [23, 88] | 60.0% (3/5) [23, 88] | 0.0% (0/5) [0, 43] | 100.0% (5/5) [57, 100] |
| quote_expiry | 0.0% (0/11) [0, 26] | 90.9% (10/11) [62, 98] | 0.0% (0/11) [0, 26] | 100.0% (11/11) [74, 100] |
| deadline_revision | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 44.4% (8/18) [25, 66] | 66.7% (12/18) [44, 84] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 23.9% (21/88) [16, 34] | 88.6% (78/88) [80, 94] | 0.0% (0/88) [0, 4] | 100.0% (88/88) [96, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (5/5) [57, 100] | 100.0% (5/5) [57, 100] | 100.0% (5/5) [57, 100] | 100.0% (5/5) [57, 100] |
| quote_expiry | 0.0% (0/11) [0, 26] | 63.6% (7/11) [35, 85] | 0.0% (0/11) [0, 26] | 0.0% (0/11) [0, 26] |
| deadline_revision | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 77.8% (14/18) [55, 91] |
| hard_constraint | 11.1% (2/18) [3, 33] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 27.8% (5/18) [12, 51] | 22.2% (4/18) [9, 45] | 0.0% (0/18) [0, 18] | 77.8% (14/18) [55, 91] |
| all | 25.0% (22/88) [17, 35] | 77.3% (68/88) [67, 85] | 5.7% (5/88) [2, 13] | 78.4% (69/88) [69, 86] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 92.0 | 144.5 | 59.5 (72) | 59.5 | 1079.5 |
| chat | 90 | 139.0 | 187.5 | 52.0 (160) | 52.0 | 783.0 |

Added tokens by category (median): price_freshness 42.0, quote_expiry 52.0, deadline_revision 148.0, not_checked 154.0, changes_since_seen 54.0, hard_constraint 66.5, effect_once 0.0, all 54.0.

## A turn that asks for no block (dataset v2 sample, 20 cases)

| View | median | p95 |
|---|---|---|
| voice | 88.0 | 143 |
| chat | 83.5 | 209 |

## A turn that asks for no block, against the baseline's code (paired)

The same subjects read by both: each pair's change in tokens, the median change with its 95% interval. The criterion: the median within 5%.

| Reads | View | pairs | baseline, median | now, median | pairs that changed | median change | within |
|---|---|---|---|---|---|---|---|
| dataset v2 sample | voice | 20 | 91.5 | 88.0 | 14 | -0.4% [-5.8, +0.0] | yes |
| dataset v2 sample | chat | 20 | 92.5 | 83.5 | 17 | -0.5% [-6.2, +2.0] | yes |
| typed cases, `without` | voice | 36 | 93.0 | 92.0 | 32 | +1.3% [+0.0, +1.9] | yes |
| typed cases, `without` | chat | 90 | 139.0 | 139.0 | 77 | +0.8% [+0.7, +0.9] | yes |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 22 | unsupported 22 | block 7, warn 15 | 21 | 6 |
| with | 32 | matched 30, no_evidence 2 | none 30, warn 2 | 2 | 0 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 126 | 126 | 0 |
| with | 126 | 126 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 126 of 126 served at the level proved, 0 with an item withheld, 0 with the sensitive value; with 126 of 126 served at the level proved, 0 with an item withheld, 0 with the sensitive value.

Coordination: 18 of 18 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-001` (price_freshness): without True, with False (not valid)
- `typed-pt-002` (price_freshness): without True, with False (not valid)
- `typed-pt-003` (price_freshness): without True, with False (not valid)
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-008` (deadline_revision): without False, with True
- `typed-pt-009` (deadline_revision): without True, with False
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-018` (hard_constraint): without False, with True
- `typed-pt-019` (effect_once): without False, with True
- `typed-en-001` (price_freshness): without True, with False (not valid)
- `typed-en-003` (price_freshness): without True, with False (not valid)
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-008` (deadline_revision): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-018` (hard_constraint): without False, with True
- `typed-en-019` (effect_once): without False, with True
- `typed-en-020` (effect_once): without True, with False
- `typed-pt-002` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-018` (hard_constraint): without False, with True
- `typed-pt-019` (effect_once): without False, with True
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-010` (not_checked): without False, with True (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-018` (hard_constraint): without False, with True
- `typed-en-020` (effect_once): without False, with True
- `typed-en-021` (effect_once): without False, with True
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-018` (hard_constraint): without False, with True
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-009` (deadline_revision): without False, with True
- `typed-en-010` (not_checked): without True, with False (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-018` (hard_constraint): without False, with True

## Against 2026-09-30-b59a5d (3 repetition(s))

Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval that crosses 0 does not show a change.

### every case

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +11.1 [-19.6, +39.1] | -27.8 [-53.0, +4.3] | +5.6 [-20.6, +31.0] | +0.0 [-20.7, +20.7] |
| quote_expiry | +0.0 [-17.6, +17.6] | -11.1 [-32.8, +8.2] | +0.0 [-17.6, +17.6] | -27.8 [-52.1, +2.3] |
| deadline_revision | -16.7 [-43.3, +13.8] | +5.6 [-16.2, +27.7] | -11.1 [-38.8, +19.2] | +5.6 [-16.2, +27.7] |
| not_checked | -5.6 [-25.8, +12.6] | -5.6 [-25.8, +12.6] | +5.6 [-18.7, +29.5] | +16.7 [-4.0, +39.2] |
| changes_since_seen | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | -5.6 [-25.8, +12.6] |
| hard_constraint | +0.0 [-17.6, +17.6] | +5.6 [-12.6, +25.8] | -22.2 [-46.5, +5.4] | +0.0 [-17.6, +17.6] |
| effect_once | +11.1 [-19.2, +38.8] | +22.2 [-9.4, +48.4] | +5.6 [-22.1, +32.2] | -5.6 [-32.2, +22.1] |
| all | +0.0 [-11.7, +11.7] | -1.6 [-10.7, +7.5] | -2.4 [-14.2, +9.6] | -2.4 [-12.0, +7.3] |

### valid cases

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | -6.7 [-52.5, +47.2] | -40.0 [-76.9, +22.9] | +0.0 [-43.5, +56.1] | +0.0 [-43.5, +56.1] |
| quote_expiry | +0.0 [-39.0, +25.9] | -9.1 [-37.7, +30.6] | +0.0 [-39.0, +25.9] | -3.0 [-39.9, +39.3] |
| deadline_revision | -16.7 [-43.3, +13.8] | +5.6 [-16.2, +27.7] | -11.1 [-38.8, +19.2] | +5.6 [-16.2, +27.7] |
| changes_since_seen | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | -5.6 [-25.8, +12.6] |
| hard_constraint | +0.0 [-17.6, +17.6] | +5.6 [-12.6, +25.8] | -22.2 [-46.5, +5.4] | +0.0 [-17.6, +17.6] |
| effect_once | +11.1 [-19.2, +38.8] | +22.2 [-9.4, +48.4] | +5.6 [-22.1, +32.2] | -5.6 [-32.2, +22.1] |
| all | -2.1 [-15.1, +10.8] | +4.7 [-5.8, +15.4] | -5.9 [-19.2, +7.6] | -1.7 [-14.0, +10.8] |

## Cost

- Agent and judge: US$ 0.0457 (1008 calls).
- The cell's models (ledger): US$ 0.0255.
