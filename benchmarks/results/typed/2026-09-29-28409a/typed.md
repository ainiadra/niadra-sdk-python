# Typed-object set 2026-09-29-28409a

- Where: `local-cell`; 42 cases, 2 repetition(s); SDK 0.6.1 (source d10af9c).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer.
- Validity rule (right with the whole history, wrong with no memory): 53 of 84 cases.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 33.3% (4/12) | 83.3% (10/12) | 50.0% (6/12) | 58.3% (7/12) |
| quote_expiry | 0.0% (0/12) | 91.7% (11/12) | 0.0% (0/12) | 25.0% (3/12) |
| deadline_revision | 83.3% (10/12) | 91.7% (11/12) | 0.0% (0/12) | 100.0% (12/12) |
| not_checked | 75.0% (9/12) | 83.3% (10/12) | 100.0% (12/12) | 91.7% (11/12) |
| changes_since_seen | 0.0% (0/12) | 8.3% (1/12) | 0.0% (0/12) | 100.0% (12/12) |
| hard_constraint | 0.0% (0/12) | 25.0% (3/12) | 0.0% (0/12) | 100.0% (12/12) |
| effect_once | 33.3% (4/12) | 58.3% (7/12) | 0.0% (0/12) | 100.0% (12/12) |
| all | 32.1% (27/84) | 63.1% (53/84) | 21.4% (18/84) | 82.1% (69/84) |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 83.3% (10/12) | 100.0% (12/12) | 100.0% (12/12) | 91.7% (11/12) |
| quote_expiry | 0.0% (0/12) | 83.3% (10/12) | 0.0% (0/12) | 0.0% (0/12) |
| deadline_revision | 83.3% (10/12) | 91.7% (11/12) | 0.0% (0/12) | 100.0% (12/12) |
| not_checked | 83.3% (10/12) | 91.7% (11/12) | 75.0% (9/12) | 100.0% (12/12) |
| changes_since_seen | 0.0% (0/12) | 8.3% (1/12) | 0.0% (0/12) | 83.3% (10/12) |
| hard_constraint | 50.0% (6/12) | 50.0% (6/12) | 0.0% (0/12) | 100.0% (12/12) |
| effect_once | 16.7% (2/12) | 33.3% (4/12) | 0.0% (0/12) | 58.3% (7/12) |
| all | 45.2% (38/84) | 65.5% (55/84) | 25.0% (21/84) | 76.2% (64/84) |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 0.0% (0/2) | 100.0% (2/2) | 0.0% (0/2) | 100.0% (2/2) |
| quote_expiry | 0.0% (0/3) | 100.0% (3/3) | 0.0% (0/3) | 100.0% (3/3) |
| deadline_revision | 83.3% (10/12) | 91.7% (11/12) | 0.0% (0/12) | 100.0% (12/12) |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/12) | 8.3% (1/12) | 0.0% (0/12) | 100.0% (12/12) |
| hard_constraint | 0.0% (0/12) | 25.0% (3/12) | 0.0% (0/12) | 100.0% (12/12) |
| effect_once | 33.3% (4/12) | 58.3% (7/12) | 0.0% (0/12) | 100.0% (12/12) |
| all | 26.4% (14/53) | 50.9% (27/53) | 0.0% (0/53) | 100.0% (53/53) |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| price_freshness | 50.0% (1/2) | 100.0% (2/2) | 100.0% (2/2) | 100.0% (2/2) |
| quote_expiry | 0.0% (0/3) | 100.0% (3/3) | 0.0% (0/3) | 0.0% (0/3) |
| deadline_revision | 83.3% (10/12) | 91.7% (11/12) | 0.0% (0/12) | 100.0% (12/12) |
| not_checked | - | - | - | - |
| changes_since_seen | 0.0% (0/12) | 8.3% (1/12) | 0.0% (0/12) | 83.3% (10/12) |
| hard_constraint | 50.0% (6/12) | 50.0% (6/12) | 0.0% (0/12) | 100.0% (12/12) |
| effect_once | 16.7% (2/12) | 33.3% (4/12) | 0.0% (0/12) | 58.3% (7/12) |
| all | 35.9% (19/53) | 50.9% (27/53) | 3.8% (2/53) | 81.1% (43/53) |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| voice | 24 | 88.5 | 141.5 | 43.0 (56) | 43.0 | 645.0 |
| chat | 60 | 157.0 | 198.0 | 50.0 (165) | 50.0 | 779.5 |

Added tokens by category (median): price_freshness 40.0, quote_expiry 50.0, deadline_revision 148.0, not_checked 158.0, changes_since_seen 52.5, hard_constraint 33.0, effect_once 0.0, all 50.0.

## A turn that asks for no block (dataset v2 sample)

| View | median | p95 | today | within 5% |
|---|---|---|---|---|
| voice | 92.0 | 136 | 89 | yes |
| chat | 94.5 | 162 | 98 | yes |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 41 | unsupported 41 | block 14, warn 27 | 31 | 5 |
| with | 41 | no_evidence 10, stale 10, unsupported 21 | block 21, warn 20 | 34 | 27 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 84 | 84 | 0 |
| with | 84 | 84 | 0 |

Coordination: 12 of 12 second attempts refused as done.

## Cases that changed verdict (judge)

- `typed-pt-003` (price_freshness): without False, with True (not valid)
- `typed-pt-004` (quote_expiry): without False, with True
- `typed-pt-005` (quote_expiry): without False, with True (not valid)
- `typed-pt-010` (not_checked): without False, with True (not valid)
- `typed-pt-011` (not_checked): without False, with True (not valid)
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-pt-019` (effect_once): without False, with True
- `typed-pt-021` (effect_once): without False, with True
- `typed-en-002` (price_freshness): without False, with True
- `typed-en-003` (price_freshness): without False, with True (not valid)
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True (not valid)
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-008` (deadline_revision): without False, with True
- `typed-en-011` (not_checked): without True, with False (not valid)
- `typed-en-019` (effect_once): without False, with True
- `typed-pt-004` (quote_expiry): without False, with True (not valid)
- `typed-pt-005` (quote_expiry): without False, with True
- `typed-pt-006` (quote_expiry): without False, with True (not valid)
- `typed-pt-015` (changes_since_seen): without False, with True
- `typed-pt-017` (hard_constraint): without False, with True
- `typed-en-001` (price_freshness): without False, with True (not valid)
- `typed-en-002` (price_freshness): without False, with True (not valid)
- `typed-en-003` (price_freshness): without False, with True
- `typed-en-004` (quote_expiry): without False, with True (not valid)
- `typed-en-005` (quote_expiry): without False, with True
- `typed-en-006` (quote_expiry): without False, with True (not valid)
- `typed-en-017` (hard_constraint): without False, with True

## Cost

- Agent and judge: US$ 0.0312 (672 calls).
- The cell's models (ledger): US$ 0.0198.
