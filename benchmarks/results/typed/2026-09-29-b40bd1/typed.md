# Typed-object set 2026-09-29-b40bd1

- Where: `local-cell`; 42 cases, 3 repetition(s); SDK 0.6.1 (source 8add479).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 81 of 126 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 55.6% (10/18) [34, 75] | 50.0% (9/18) [29, 71] | 44.4% (8/18) [25, 66] | 44.4% (8/18) [25, 66] |
| quote_expiry | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 38.9% (7/18) [20, 61] |
| deadline_revision | 72.2% (13/18) [49, 88] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 88.9% (16/18) [67, 97] | 94.4% (17/18) [74, 99] | 100.0% (18/18) [82, 100] | 83.3% (15/18) [61, 94] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 16.7% (3/18) [6, 39] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 33.3% (6/18) [16, 56] | 50.0% (9/18) [29, 71] | 0.0% (0/18) [0, 18] | 88.9% (16/18) [67, 97] |
| all | 35.7% (45/126) [28, 44] | 72.2% (91/126) [64, 79] | 20.6% (26/126) [14, 29] | 79.4% (100/126) [71, 86] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 88.9% (16/18) [67, 97] | 100.0% (18/18) [82, 100] | 100.0% (18/18) [82, 100] | 88.9% (16/18) [67, 97] |
| quote_expiry | 0.0% (0/18) [0, 18] | 61.1% (11/18) [39, 80] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] |
| deadline_revision | 66.7% (12/18) [44, 84] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 94.4% (17/18) [74, 99] | 88.9% (16/18) [67, 97] | 88.9% (16/18) [67, 97] | 88.9% (16/18) [67, 97] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 16.7% (3/18) [6, 39] | 16.7% (3/18) [6, 39] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 11.1% (2/18) [3, 33] | 5.6% (1/18) [1, 26] | 0.0% (0/18) [0, 18] | 61.1% (11/18) [39, 80] |
| all | 39.7% (50/126) [32, 48] | 66.7% (84/126) [58, 74] | 27.0% (34/126) [20, 35] | 77.0% (97/126) [69, 83] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 75.0% (3/4) [30, 95] | 50.0% (2/4) [15, 85] | 0.0% (0/4) [0, 49] | 100.0% (4/4) [51, 100] |
| quote_expiry | 0.0% (0/7) [0, 35] | 100.0% (7/7) [65, 100] | 0.0% (0/7) [0, 35] | 100.0% (7/7) [65, 100] |
| deadline_revision | 72.2% (13/18) [49, 88] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 16.7% (3/18) [6, 39] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 31.2% (5/16) [14, 56] | 50.0% (8/16) [28, 72] | 0.0% (0/16) [0, 19] | 100.0% (16/16) [81, 100] |
| all | 25.9% (21/81) [18, 36] | 67.9% (55/81) [57, 77] | 0.0% (0/81) [0, 5] | 100.0% (81/81) [95, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (4/4) [51, 100] | 100.0% (4/4) [51, 100] | 100.0% (4/4) [51, 100] | 100.0% (4/4) [51, 100] |
| quote_expiry | 0.0% (0/7) [0, 35] | 57.1% (4/7) [25, 84] | 0.0% (0/7) [0, 35] | 0.0% (0/7) [0, 35] |
| deadline_revision | 66.7% (12/18) [44, 84] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 16.7% (3/18) [6, 39] | 16.7% (3/18) [6, 39] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 12.5% (2/16) [4, 36] | 6.2% (1/16) [1, 28] | 0.0% (0/16) [0, 19] | 68.8% (11/16) [44, 86] |
| all | 25.9% (21/81) [18, 36] | 58.0% (47/81) [47, 68] | 4.9% (4/81) [2, 12] | 85.2% (69/81) [76, 91] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 47.0 | 100.0 | 43.5 (57) | 43.5 | 684.5 |
| chat | 90 | 139.5 | 185.0 | 51.0 (164) | 51.0 | 788.0 |

Added tokens by category (median): price_freshness 41.0, quote_expiry 51.0, deadline_revision 151.0, not_checked 157.0, changes_since_seen 53.5, hard_constraint 34.0, effect_once 0.0, all 51.0.

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 20 | unsupported 20 | block 7, warn 13 | 17 | 2 |
| with | 36 | matched 29, unsupported 7 | block 7, none 29 | 6 | 6 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 126 | 126 | 0 |
| with | 126 | 126 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V3), per side: without 126 of 126 served at the level proved, 23 with an item withheld, 0 with the sensitive value; with 126 of 126 served at the level proved, 23 with an item withheld, 0 with the sensitive value.

Coordination: 18 of 18 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-001` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-020` (effect_once): without False, with True
- `typed-pt-021` (effect_once): without False, with True
- `typed-en-003` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-009` (deadline_revision): without False, with True
- `typed-en-012` (not_checked): without False, with True (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-020` (effect_once): without False, with True
- `typed-pt-001` (price_freshness): without True, with False (not valid)
- `typed-pt-002` (price_freshness): without True, with False (not valid)
- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-en-003` (price_freshness): without True, with False
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-011` (not_checked): without False, with True (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-pt-001` (price_freshness): without True, with False (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-019` (effect_once): without True, with False
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-008` (deadline_revision): without False, with True
- `typed-en-011` (not_checked): without True, with False (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-020` (effect_once): without False, with True

## Against 2026-09-29-c474e5 (3 repetition(s))

Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval that crosses 0 does not show a change.

### every case

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +5.6 [-24.7, +34.4] | -11.1 [-39.1, +19.6] | +0.0 [-23.1, +23.1] | +0.0 [-17.6, +17.6] |
| quote_expiry | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | -5.6 [-33.8, +23.9] |
| deadline_revision | +22.2 [-9.0, +48.2] | +11.1 [-11.8, +34.1] | +22.2 [-9.4, +48.4] | +11.1 [-11.8, +34.1] |
| not_checked | -11.1 [-32.8, +8.2] | +0.0 [-20.7, +20.7] | +0.0 [-20.7, +20.7] | -5.6 [-27.7, +16.2] |
| changes_since_seen | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +5.6 [-12.6, +25.8] |
| hard_constraint | +0.0 [-17.6, +17.6] | +11.1 [-11.8, +34.1] | +0.0 [-25.0, +25.0] | +5.6 [-18.7, +29.5] |
| effect_once | -16.7 [-43.7, +14.4] | +11.1 [-19.6, +39.1] | -16.7 [-41.1, +9.9] | -11.1 [-34.1, +11.8] |
| all | +0.0 [-11.7, +11.7] | +3.2 [-8.0, +14.3] | +0.8 [-11.1, +12.7] | +0.0 [-11.5, +11.5] |

### valid cases

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | -25.0 [-69.9, +34.8] | -16.7 [-61.0, +41.0] | +0.0 [-49.0, +56.1] | +0.0 [-49.0, +56.1] |
| quote_expiry | +0.0 [-27.8, +35.4] | +0.0 [-35.4, +27.8] | +0.0 [-27.8, +35.4] | -2.9 [-42.4, +36.6] |
| deadline_revision | +22.2 [-9.0, +48.2] | +11.1 [-11.8, +34.1] | +22.2 [-9.4, +48.4] | +11.1 [-11.8, +34.1] |
| changes_since_seen | +0.0 [-18.4, +17.6] | +0.0 [-17.6, +18.4] | +0.0 [-18.4, +17.6] | +5.9 [-12.4, +27.0] |
| hard_constraint | +0.0 [-17.6, +17.6] | +11.1 [-11.8, +34.1] | +0.0 [-25.0, +25.0] | +5.6 [-18.7, +29.5] |
| effect_once | -18.8 [-45.8, +13.4] | +11.1 [-20.3, +39.9] | -15.3 [-40.1, +12.8] | -10.4 [-33.6, +14.2] |
| all | +0.9 [-12.2, +14.1] | +6.0 [-8.5, +20.1] | +3.3 [-9.7, +16.3] | +4.5 [-10.5, +19.1] |

## Cost

- Agent and judge: US$ 0.0464 (1008 calls).
- The cell's models (ledger): US$ 0.0157.
