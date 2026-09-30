# Typed-object set 2026-09-30-b59a5d

- Where: `local-cell`; 42 cases, 3 repetition(s); SDK 0.6.1 (source 576d426).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 81 of 126 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 50.0% (9/18) [29, 71] | 66.7% (12/18) [44, 84] | 50.0% (9/18) [29, 71] | 50.0% (9/18) [29, 71] |
| quote_expiry | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 33.3% (6/18) [16, 56] |
| deadline_revision | 72.2% (13/18) [49, 88] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 100.0% (18/18) [82, 100] | 100.0% (18/18) [82, 100] | 100.0% (18/18) [82, 100] | 94.4% (17/18) [74, 99] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 33.3% (6/18) [16, 56] | 44.4% (8/18) [25, 66] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 36.5% (46/126) [29, 45] | 84.9% (107/126) [78, 90] | 21.4% (27/126) [15, 29] | 82.5% (104/126) [75, 88] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 77.8% (14/18) [55, 91] | 94.4% (17/18) [74, 99] | 94.4% (17/18) [74, 99] | 88.9% (16/18) [67, 97] |
| quote_expiry | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] | 0.0% (0/18) [0, 18] | 0.0% (0/18) [0, 18] |
| deadline_revision | 66.7% (12/18) [44, 84] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | 83.3% (15/18) [61, 94] | 83.3% (15/18) [61, 94] | 72.2% (13/18) [49, 88] | 83.3% (15/18) [61, 94] |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 33.3% (6/18) [16, 56] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 22.2% (4/18) [9, 45] | 27.8% (5/18) [12, 51] | 0.0% (0/18) [0, 18] | 61.1% (11/18) [39, 80] |
| all | 40.5% (51/126) [32, 49] | 82.5% (104/126) [75, 88] | 23.8% (30/126) [17, 32] | 73.8% (93/126) [66, 81] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 66.7% (2/3) [21, 94] | 100.0% (3/3) [44, 100] | 0.0% (0/3) [0, 56] | 100.0% (3/3) [44, 100] |
| quote_expiry | 0.0% (0/6) [0, 39] | 100.0% (6/6) [61, 100] | 0.0% (0/6) [0, 39] | 100.0% (6/6) [61, 100] |
| deadline_revision | 72.2% (13/18) [49, 88] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| hard_constraint | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 33.3% (6/18) [16, 56] | 44.4% (8/18) [25, 66] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 25.9% (21/81) [18, 36] | 84.0% (68/81) [74, 90] | 0.0% (0/81) [0, 5] | 100.0% (81/81) [95, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 100.0% (3/3) [44, 100] | 100.0% (3/3) [44, 100] | 100.0% (3/3) [44, 100] | 100.0% (3/3) [44, 100] |
| quote_expiry | 0.0% (0/6) [0, 39] | 66.7% (4/6) [30, 90] | 0.0% (0/6) [0, 39] | 0.0% (0/6) [0, 39] |
| deadline_revision | 66.7% (12/18) [44, 84] | 88.9% (16/18) [67, 97] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 83.3% (15/18) [61, 94] |
| hard_constraint | 33.3% (6/18) [16, 56] | 100.0% (18/18) [82, 100] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| effect_once | 22.2% (4/18) [9, 45] | 27.8% (5/18) [12, 51] | 0.0% (0/18) [0, 18] | 61.1% (11/18) [39, 80] |
| all | 30.9% (25/81) [22, 42] | 79.0% (64/81) [69, 86] | 3.7% (3/81) [1, 10] | 80.2% (65/81) [70, 87] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 36 | 93.0 | 145.0 | 58.0 (70) | 58.0 | 1038.0 |
| chat | 90 | 139.0 | 185.0 | 51.0 (158) | 51.0 | 777.0 |

Added tokens by category (median): price_freshness 41.0, quote_expiry 51.0, deadline_revision 147.5, not_checked 152.0, changes_since_seen 53.0, hard_constraint 64.5, effect_once 0.0, all 53.0.

## A turn that asks for no block (dataset v2 sample, 20 cases)

| View | median | p95 |
|---|---|---|
| voice | 91.5 | 136 |
| chat | 92.5 | 147 |

## A turn that asks for no block, against the baseline's code (paired)

The same subjects read by both: each pair's change in tokens, the median change with its 95% interval. The criterion: the median within 5%.

| Reads | View | pairs | baseline, median | now, median | pairs that changed | median change | within |
|---|---|---|---|---|---|---|---|
| dataset v2 sample | voice | 20 | 91.5 | 91.5 | 13 | +0.0% [-2.8, +1.4] | yes |
| dataset v2 sample | chat | 20 | 88.0 | 92.5 | 14 | +0.0% [-9.3, +4.1] | yes |
| typed cases, `without` | voice | 36 | 72.0 | 93.0 | 36 | +29.2% [+27.8, +40.9] | no |
| typed cases, `without` | chat | 90 | 139.5 | 139.0 | 82 | -0.8% [-0.9, -0.7] | yes |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 72 | unsupported 72 | block 32, warn 40 | 62 | 10 |
| with | 92 | matched 8, no_evidence 15, stale 21, unsupported 48 | block 48, none 8, warn 36 | 61 | 54 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 126 | 126 | 0 |
| with | 126 | 126 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 126 of 126 served at the level proved, 0 with an item withheld, 0 with the sensitive value; with 126 of 126 served at the level proved, 0 with an item withheld, 0 with the sensitive value.

Coordination: 18 of 18 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-001` (price_freshness): without True, with False (not valid)
- `typed-pt-002` (price_freshness): without False, with True (not valid)
- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-008` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-018` (hard_constraint): without False, with True
- `typed-pt-021` (effect_once): without False, with True
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-018` (hard_constraint): without False, with True
- `typed-pt-001` (price_freshness): without False, with True (not valid)
- `typed-pt-002` (price_freshness): without True, with False (not valid)
- `typed-pt-003` (price_freshness): without True, with False (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-007` (deadline_revision): without False, with True
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-020` (effect_once): without True, with False
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True
- `typed-en-009` (deadline_revision): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-018` (hard_constraint): without False, with True
- `typed-pt-001` (price_freshness): without False, with True (not valid)
- `typed-pt-002` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-009` (deadline_revision): without True, with False
- `typed-pt-013` (changes_since_seen): without False, with True
- `typed-pt-014` (changes_since_seen): without False, with True
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-016` (hard_constraint): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-018` (hard_constraint): without False, with True
- `typed-pt-021` (effect_once): without False, with True
- `typed-en-003` (price_freshness): without False, with True
- `typed-en-004` (quote_expiry): without False, with True
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-007` (deadline_revision): without False, with True
- `typed-en-013` (changes_since_seen): without False, with True
- `typed-en-014` (changes_since_seen): without False, with True
- `typed-en-015` (changes_since_seen): without False, with True
- `typed-en-016` (hard_constraint): without False, with True
- `typed-en-017` (hard_constraint): without False, with True
- `typed-en-018` (hard_constraint): without False, with True
- `typed-en-019` (effect_once): without False, with True

## Against 2026-09-29-4974b1 (3 repetition(s))

Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval that crosses 0 does not show a change.

### every case

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +0.0 [-29.6, +29.6] | -16.7 [-42.0, +11.6] | -5.6 [-31.0, +20.6] | -5.6 [-25.8, +12.6] |
| quote_expiry | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +0.0 [-17.6, +17.6] | +16.7 [-11.6, +42.0] |
| deadline_revision | -5.6 [-32.2, +22.1] | -11.1 [-32.8, +8.2] | -5.6 [-33.1, +23.2] | -11.1 [-32.8, +8.2] |
| not_checked | +0.0 [-17.6, +17.6] | +11.1 [-8.2, +32.8] | -16.7 [-39.2, +4.0] | +0.0 [-25.0, +25.0] |
| changes_since_seen | +0.0 [-17.6, +17.6] | +100.0 [+75.1, +100.0] | +0.0 [-17.6, +17.6] | +100.0 [+75.1, +100.0] |
| hard_constraint | +0.0 [-17.6, +17.6] | +94.4 [+67.7, +99.0] | +22.2 [-5.4, +46.5] | +100.0 [+75.1, +100.0] |
| effect_once | +11.1 [-17.5, +37.6] | +5.6 [-24.5, +34.2] | +0.0 [-26.5, +26.5] | +5.6 [-22.1, +32.2] |
| all | +0.8 [-10.9, +12.5] | +26.2 [+15.2, +36.4] | -0.8 [-12.8, +11.2] | +29.4 [+18.0, +39.7] |

### valid cases

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| price_freshness | +26.7 [-32.2, +65.9] | +20.0 [-38.5, +62.5] | +0.0 [-56.1, +43.5] | +0.0 [-56.1, +43.5] |
| quote_expiry | +0.0 [-27.8, +39.0] | +0.0 [-39.0, +27.8] | +0.0 [-27.8, +39.0] | -13.3 [-52.7, +25.7] |
| deadline_revision | -5.6 [-32.2, +22.1] | -11.1 [-32.8, +8.2] | -5.6 [-33.1, +23.2] | -11.1 [-32.8, +8.2] |
| changes_since_seen | +0.0 [-17.6, +17.6] | +100.0 [+75.1, +100.0] | +0.0 [-17.6, +17.6] | +100.0 [+75.1, +100.0] |
| hard_constraint | +0.0 [-17.6, +17.6] | +94.4 [+67.7, +99.0] | +22.2 [-5.4, +46.5] | +100.0 [+75.1, +100.0] |
| effect_once | +8.3 [-21.5, +35.6] | +6.9 [-24.1, +35.9] | -2.8 [-30.6, +24.6] | +2.8 [-26.1, +30.2] |
| all | +2.4 [-10.6, +15.4] | +39.2 [+25.0, +51.2] | +2.6 [-11.1, +16.3] | +37.8 [+23.2, +50.2] |

## Cost

- Agent and judge: US$ 0.0457 (1008 calls).
- The cell's models (ledger): US$ 0.0247.
