# Typed-object set 2026-09-29-c474e5

- Where: `local-cell`; 42 cases, 3 repetition(s); SDK 0.6.1 (source b198e14).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 84 of 126 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 50.0% (9/18) [29, 71] | 61.1% (11/18) [39, 80] | 44.4% (8/18) [25, 66] | 33.3% (6/18) [16, 56] |
| quote_expiry | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 55.6% (10/18) [34, 75] |
| deadline_revision | 50.0% (9/18) [29, 71] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 100.0% (18/18) [82, 100] | 94.4% (17/18) [74, 99] | 100.0% (18/18) [82, 100] | 88.9% (16/18) [67, 97] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] |
| hard_constraint | 0.0% (0/18) [0, 18] | 5.6% (1/18) [1, 26] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 50.0% (9/18) [29, 71] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 35.7% (45/126) [28, 44] | 69.0% (87/126) [61, 76] | 20.6% (26/126) [14, 29] | 81.8% (103/126) [74, 88] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 88.9% (16/18) [67, 97] | 100.0% (18/18) [82, 100] | 88.9% (16/18) [67, 97] | 94.4% (17/18) [74, 99] |
| quote_expiry | 0.0% (0/18) [0, 18] | 66.7% (12/18) [44, 84] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] |
| deadline_revision | 44.4% (8/18) [25, 66] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 94.4% (17/18) [74, 99] | 94.4% (17/18) [74, 99] | 83.3% (15/18) [61, 94] | 94.4% (17/18) [74, 99] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 16.7% (3/18) [6, 39] | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 27.8% (5/18) [12, 51] | 16.7% (3/18) [6, 39] | 0.0% (0/18) [0, 18] | 66.7% (12/18) [44, 84] |
| all | 38.9% (49/126) [31, 48] | 66.7% (84/126) [58, 74] | 24.6% (31/126) [18, 33] | 77.0% (97/126) [69, 83] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (3/3) [44, 100] | 66.7% (2/3) [21, 94] | 0.0% (0/3) [0, 56] | 100.0% (3/3) [44, 100] |
| quote_expiry | 0.0% (0/10) [0, 28] | 100.0% (10/10) [72, 100] | 0.0% (0/10) [0, 28] | 100.0% (10/10) [72, 100] |
| deadline_revision | 50.0% (9/18) [29, 71] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/17) [0, 18] | 100.0% (17/17) [82, 100] | 0.0% (0/17) [0, 18] | 100.0% (17/17) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 5.6% (1/18) [1, 26] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 50.0% (9/18) [29, 71] | 38.9% (7/18) [20, 61] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 25.0% (21/84) [17, 35] | 61.9% (52/84) [51, 72] | 0.0% (0/84) [0, 4] | 100.0% (84/84) [96, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (3/3) [44, 100] | 100.0% (3/3) [44, 100] | 100.0% (3/3) [44, 100] | 100.0% (3/3) [44, 100] |
| quote_expiry | 0.0% (0/10) [0, 28] | 60.0% (6/10) [31, 83] | 0.0% (0/10) [0, 28] | 0.0% (0/10) [0, 28] |
| deadline_revision | 44.4% (8/18) [25, 66] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/17) [0, 18] | 94.1% (16/17) [73, 99] | 0.0% (0/17) [0, 18] | 88.2% (15/17) [66, 97] |
| hard_constraint | 16.7% (3/18) [6, 39] | 11.1% (2/18) [3, 33] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 27.8% (5/18) [12, 51] | 16.7% (3/18) [6, 39] | 0.0% (0/18) [0, 18] | 66.7% (12/18) [44, 84] |
| all | 22.6% (19/84) [15, 33] | 53.6% (45/84) [43, 64] | 3.6% (3/84) [1, 10] | 78.6% (66/84) [69, 86] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 47.0 | 100.0 | 43.5 (57) | 43.5 | 683.0 |
| chat | 90 | 139.5 | 185.0 | 51.0 (158) | 51.0 | 781.0 |

Added tokens by category (median): price_freshness 41.0, quote_expiry 51.0, deadline_revision 149.0, not_checked 154.0, changes_since_seen 53.5, hard_constraint 34.0, effect_once 0.0, all 51.0.

## A turn that asks for no block (dataset v2 sample)

| View | median | p95 | today | within 5% |
|---|---|---|---|---|
| voice | 96.0 | 130 | 89 | no |
| chat | 94.0 | 160 | 98 | yes |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 43 | unsupported 43 | block 6, warn 37 | 41 | 9 |
| with | 84 | matched 10, no_evidence 21, stale 13, unsupported 40 | block 40, none 10, warn 34 | 55 | 46 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 126 | 126 | 0 |
| with | 126 | 126 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V3), per side: without 126 of 126 served at the level proved, 22 with an item withheld, 0 with the sensitive value; with 126 of 126 served at the level proved, 22 with an item withheld, 0 with the sensitive value.

Coordination: 18 of 18 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-020` (effect_once): without True, with False
- `typed-en-001` (price_freshness): without False, with True (not valid)
- `typed-en-002` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-008` (deadline_revision): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True (not valid)
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-pt-001` (price_freshness): without False, with True (not valid)
- `typed-pt-003` (price_freshness): without True, with False (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-en-001` (price_freshness): without False, with True (not valid)
- `typed-en-002` (price_freshness): without True, with False
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-009` (deadline_revision): without False, with True
- `typed-en-010` (not_checked): without True, with False (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-019` (effect_once): without False, with True
- `typed-en-021` (effect_once): without True, with False
- `typed-pt-001` (price_freshness): without True, with False (not valid)
- `typed-pt-002` (price_freshness): without True, with False (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-008` (deadline_revision): without True, with False
- `typed-pt-009` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-021` (effect_once): without True, with False
- `typed-en-001` (price_freshness): without False, with True (not valid)
- `typed-en-002` (price_freshness): without False, with True (not valid)
- `typed-en-003` (price_freshness): without True, with False (not valid)
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-008` (deadline_revision): without False, with True
- `typed-en-009` (deadline_revision): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True

## Against 2026-09-29-28409a (2 repetition(s))

Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval that crosses 0 does not show a change.

### every case

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +16.7 [-18.0, +45.3] | -22.2 [-47.7, +11.5] | +5.6 [-19.2, +34.8] | +0.0 [-17.6, +24.2] |
| quote_expiry | +0.0 [-24.2, +17.6] | +8.3 [-10.5, +35.4] | +0.0 [-24.2, +17.6] | -16.7 [-42.5, +16.2] |
| deadline_revision | -33.3 [-57.5, +1.8] | -8.3 [-31.9, +20.8] | -38.9 [-62.1, -3.3] | -8.3 [-31.9, +20.8] |
| not_checked | +25.0 [+1.1, +53.2] | +11.1 [-12.4, +39.6] | +11.1 [-12.4, +39.6] | +2.8 [-18.6, +30.2] |
| changes_since_seen | +0.0 [-24.2, +17.6] | +91.7 [+59.4, +98.5] | +0.0 [-24.2, +17.6] | +86.1 [+52.3, +94.3] |
| hard_constraint | +0.0 [-24.2, +17.6] | -19.4 [-48.0, +6.4] | -33.3 [-60.2, +0.1] | -38.9 [-64.8, -6.1] |
| effect_once | +16.7 [-18.0, +45.3] | -19.4 [-48.5, +15.2] | +11.1 [-20.9, +37.1] | -16.7 [-46.3, +13.2] |
| all | +3.6 [-9.6, +16.1] | +5.9 [-6.8, +18.9] | -6.3 [-19.7, +7.1] | +1.2 [-11.5, +14.3] |

### valid cases

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +100.0 [+13.5, +100.0] | -33.3 [-79.2, +37.8] | +50.0 [-19.3, +90.5] | +0.0 [-56.1, +65.8] |
| quote_expiry | +0.0 [-56.1, +27.8] | +0.0 [-27.8, +56.1] | +0.0 [-56.1, +27.8] | -40.0 [-68.7, +20.8] |
| deadline_revision | -33.3 [-57.5, +1.8] | -8.3 [-31.9, +20.8] | -38.9 [-62.1, -3.3] | -8.3 [-31.9, +20.8] |
| changes_since_seen | +0.0 [-24.2, +18.4] | +91.7 [+58.9, +98.5] | +0.0 [-24.2, +18.4] | +85.8 [+51.5, +94.2] |
| hard_constraint | +0.0 [-24.2, +17.6] | -19.4 [-48.0, +6.4] | -33.3 [-60.2, +0.1] | -38.9 [-64.8, -6.1] |
| effect_once | +16.7 [-18.0, +45.3] | -19.4 [-48.5, +15.2] | +11.1 [-20.9, +37.1] | -16.7 [-46.3, +13.2] |
| all | -1.4 [-16.8, +12.9] | +11.0 [-5.8, +27.2] | -13.2 [-28.7, +2.1] | +2.6 [-14.1, +19.2] |

## Cost

- Agent and judge: US$ 0.0458 (1008 calls).
- The cell's models (ledger): US$ 0.0249.
