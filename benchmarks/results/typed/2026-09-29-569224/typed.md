# Typed-object set 2026-09-29-569224

- Where: `local-cell`; 42 cases, 3 repetition(s); SDK 0.6.1 (source ba187e8).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 83 of 126 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 33.3% (6/18) [16, 56] | 50.0% (9/18) [29, 71] | 50.0% (9/18) [29, 71] | 27.8% (5/18) [12, 51] |
| quote_expiry | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 66.7% (12/18) [44, 84] |
| deadline_revision | 72.2% (13/18) [49, 88] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 94.4% (17/18) [74, 99] | 94.4% (17/18) [74, 99] | 100.0% (18/18) [82, 100] | 94.4% (17/18) [74, 99] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 50.0% (9/18) [29, 71] | 55.6% (10/18) [34, 75] | 0.0% (0/18) [0, 18] | 88.9% (16/18) [67, 97] |
| all | 35.7% (45/126) [28, 44] | 74.6% (94/126) [66, 81] | 21.4% (27/126) [15, 29] | 82.5% (104/126) [75, 88] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 88.9% (16/18) [67, 97] | 100.0% (18/18) [82, 100] | 100.0% (18/18) [82, 100] | 94.4% (17/18) [74, 99] |
| quote_expiry | 0.0% (0/18) [0, 18] | 72.2% (13/18) [49, 88] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] |
| deadline_revision | 66.7% (12/18) [44, 84] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 88.9% (16/18) [67, 97] | 88.9% (16/18) [67, 97] | 83.3% (15/18) [61, 94] | 88.9% (16/18) [67, 97] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 77.8% (14/18) [55, 91] |
| hard_constraint | 44.4% (8/18) [25, 66] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 11.1% (2/18) [3, 33] | 33.3% (6/18) [16, 56] | 0.0% (0/18) [0, 18] | 61.1% (11/18) [39, 80] |
| all | 42.9% (54/126) [35, 52] | 73.8% (93/126) [66, 81] | 26.2% (33/126) [19, 34] | 74.6% (94/126) [66, 81] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (1/1) [21, 100] | 0.0% (0/1) [0, 79] | 0.0% (0/1) [0, 79] | 100.0% (1/1) [21, 100] |
| quote_expiry | 0.0% (0/12) [0, 24] | 100.0% (12/12) [76, 100] | 0.0% (0/12) [0, 24] | 100.0% (12/12) [76, 100] |
| deadline_revision | 72.2% (13/18) [49, 88] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 56.2% (9/16) [33, 77] | 62.5% (10/16) [39, 82] | 0.0% (0/16) [0, 19] | 100.0% (16/16) [81, 100] |
| all | 27.7% (23/83) [19, 38] | 74.7% (62/83) [64, 83] | 0.0% (0/83) [0, 4] | 100.0% (83/83) [96, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] | 100.0% (1/1) [21, 100] |
| quote_expiry | 0.0% (0/12) [0, 24] | 91.7% (11/12) [65, 99] | 0.0% (0/12) [0, 24] | 0.0% (0/12) [0, 24] |
| deadline_revision | 66.7% (12/18) [44, 84] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 77.8% (14/18) [55, 91] |
| hard_constraint | 44.4% (8/18) [25, 66] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 12.5% (2/16) [4, 36] | 37.5% (6/16) [18, 61] | 0.0% (0/16) [0, 19] | 68.8% (11/16) [44, 86] |
| all | 27.7% (23/83) [19, 38] | 69.9% (58/83) [59, 79] | 1.2% (1/83) [0, 7] | 74.7% (62/83) [64, 83] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 98.5 | 150.0 | 43.0 (56) | 43.0 | 681.0 |
| chat | 90 | 139.0 | 181.5 | 50.0 (157) | 50.0 | 784.0 |

Added tokens by category (median): price_freshness 40.0, quote_expiry 50.0, deadline_revision 150.0, not_checked 156.0, changes_since_seen 52.5, hard_constraint 34.0, effect_once 0.0, all 50.0.

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 36 | unsupported 36 | block 20, warn 16 | 29 | 3 |
| with | 44 | matched 32, no_evidence 2, unsupported 10 | block 10, none 32, warn 2 | 10 | 8 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 126 | 126 | 0 |
| with | 126 | 126 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V3), per side: without 126 of 126 served at the level proved, 0 with an item withheld, 0 with the sensitive value; with 126 of 126 served at the level proved, 0 with an item withheld, 0 with the sensitive value.

Coordination: 18 of 18 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-002` (price_freshness): without True, with False (not valid)
- `typed-pt-003` (price_freshness): without True, with False (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-en-001` (price_freshness): without False, with True (not valid)
- `typed-en-002` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-en-001` (price_freshness): without False, with True (not valid)
- `typed-en-002` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-021` (effect_once): without False, with True
- `typed-pt-002` (price_freshness): without True, with False (not valid)
- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-010` (not_checked): without True, with False (not valid)
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-en-002` (price_freshness): without True, with False
- `typed-en-003` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-010` (not_checked): without False, with True (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True

## Against 2026-09-29-c474e5 (3 repetition(s))

Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval that crosses 0 does not show a change.

### every case

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | -16.7 [-43.7, +14.4] | -11.1 [-39.1, +19.6] | +0.0 [-23.1, +23.1] | +0.0 [-17.6, +17.6] |
| quote_expiry | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +5.6 [-23.2, +33.1] |
| deadline_revision | +22.2 [-9.0, +48.2] | +5.6 [-18.7, +29.5] | +22.2 [-9.4, +48.4] | +0.0 [-25.0, +25.0] |
| not_checked | -5.6 [-25.8, +12.6] | +0.0 [-20.7, +20.7] | -5.6 [-27.7, +16.2] | -5.6 [-27.7, +16.2] |
| changes_since_seen | +0.0 [-17.6, +17.6] | -5.6 [-25.8, +12.6] | +0.0 [-17.6, +17.6] | +5.6 [-12.6, +25.8] |
| hard_constraint | +0.0 [-17.6, +17.6] | +33.3 [+5.9, +56.3] | +27.8 [-2.3, +52.1] | +27.8 [-0.8, +51.6] |
| effect_once | +0.0 [-29.6, +29.6] | +16.7 [-14.7, +43.9] | -16.7 [-41.1, +9.9] | +16.7 [-11.6, +42.0] |
| all | +0.0 [-11.7, +11.7] | +5.6 [-5.5, +16.5] | +4.0 [-8.1, +15.8] | +7.1 [-4.1, +18.2] |

### valid cases

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +0.0 [-79.3, +56.1] | -66.7 [-93.8, +25.0] | +0.0 [-79.3, +56.1] | +0.0 [-79.3, +56.1] |
| quote_expiry | +0.0 [-27.8, +24.2] | +0.0 [-24.2, +27.8] | +0.0 [-27.8, +24.2] | +31.7 [-4.0, +61.2] |
| deadline_revision | +22.2 [-9.0, +48.2] | +5.6 [-18.7, +29.5] | +22.2 [-9.4, +48.4] | +0.0 [-25.0, +25.0] |
| changes_since_seen | +0.0 [-18.4, +17.6] | -5.6 [-25.8, +13.4] | +0.0 [-18.4, +17.6] | +5.9 [-12.4, +27.0] |
| hard_constraint | +0.0 [-17.6, +17.6] | +33.3 [+5.9, +56.3] | +27.8 [-2.3, +52.1] | +27.8 [-0.8, +51.6] |
| effect_once | +6.2 [-24.9, +35.7] | +23.6 [-9.2, +50.2] | -15.3 [-40.1, +12.8] | +20.8 [-8.7, +47.0] |
| all | +2.7 [-10.6, +15.9] | +12.8 [-1.3, +26.2] | +5.1 [-8.0, +18.0] | +16.3 [+1.6, +30.1] |

## Cost

- Agent and judge: US$ 0.0467 (1008 calls).
- The cell's models (ledger): US$ 0.0158.
