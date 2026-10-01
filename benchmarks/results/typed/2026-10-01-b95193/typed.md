# Typed-object set 2026-10-01-b95193

- Where: `local-cell`; 6 cases, 3 repetition(s); SDK 0.7.0 (source release).
- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same customer. The state lines say derived fields: yes.
- Validity rule (right with the whole history, wrong with no memory): 18 of 18 cases.
- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent draws, so it is narrower than new cases would give.

## Accuracy by category, judge, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |

## Accuracy by category, exact, every case

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 38.9% (7/18) [20, 61] | 94.4% (17/18) [74, 99] |
| all | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 38.9% (7/18) [20, 61] | 94.4% (17/18) [74, 99] |

## Accuracy by category, judge, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |
| all | 0.0% (0/18) [0, 18] | 94.4% (17/18) [74, 99] | 0.0% (0/18) [0, 18] | 100.0% (18/18) [82, 100] |

## Accuracy by category, exact, valid cases

| Category | without | with | no_memory | full_history |
|---|---|---|---|---|
| composite | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 38.9% (7/18) [20, 61] | 94.4% (17/18) [74, 99] |
| all | 55.6% (10/18) [34, 75] | 94.4% (17/18) [74, 99] | 38.9% (7/18) [20, 61] | 94.4% (17/18) [74, 99] |

## Tokens per turn (o200k_base)

| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median | same data as a tool's JSON, median |
|---|---|---|---|---|---|---|
| chat | 18 | 94.0 | 155.0 | 60.5 (76) | 60.5 | 904.5 |

Added tokens by category (median): composite 60.5, all 60.5.

## A turn that asks for no block, against the baseline's code (paired)

The same subjects read by both: each pair's change in tokens, the median change with its 95% interval. The criterion: the median within 5%.

| Reads | View | pairs | baseline, median | now, median | pairs that changed | median change | within |
|---|---|---|---|---|---|---|---|
| typed cases, `without` | chat | 18 | 95.0 | 94.0 | 17 | -0.5% [-4.3, +3.1] | yes |

## Claim guard

| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |
|---|---|---|---|---|---|
| without | 0 | none | none | 0 | 0 |
| with | 3 | no_evidence 3 | warn 3 | 3 | 3 |

## Reads at V0

| Side | reads | served at V0 | sensitive value in the block |
|---|---|---|---|
| without | 18 | 18 | 0 |
| with | 18 | 18 | 0 |

The answered reads, at the level each case proved (retail V1, legal V1, health_plan_sales V1), per side: without 18 of 18 served at the level proved, 0 with an item withheld, 0 with the sensitive value; with 18 of 18 served at the level proved, 0 with an item withheld, 0 with the sensitive value.

## Cases that changed verdict (judge)

- `typed-pt-022` (composite): without False, with True
- `typed-pt-023` (composite): without False, with True
- `typed-pt-024` (composite): without False, with True
- `typed-en-022` (composite): without False, with True
- `typed-en-023` (composite): without False, with True
- `typed-en-024` (composite): without False, with True
- `typed-pt-022` (composite): without False, with True
- `typed-pt-023` (composite): without False, with True
- `typed-pt-024` (composite): without False, with True
- `typed-en-022` (composite): without False, with True
- `typed-en-023` (composite): without False, with True
- `typed-en-024` (composite): without False, with True
- `typed-pt-022` (composite): without False, with True
- `typed-pt-023` (composite): without False, with True
- `typed-pt-024` (composite): without False, with True
- `typed-en-022` (composite): without False, with True
- `typed-en-024` (composite): without False, with True

## Against 2026-09-30-3ea01b (3 repetition(s))

Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval that crosses 0 does not show a change.

### every case

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| composite | +0.0 [-17.6, +17.6] | +27.8 [+1.3, +51.1] | +5.6 [-24.7, +34.4] | +5.6 [-16.2, +27.7] |
| all | +0.0 [-17.6, +17.6] | +27.8 [+1.3, +51.1] | +5.6 [-24.7, +34.4] | +5.6 [-16.2, +27.7] |

### valid cases

| Category | without, judge | with, judge | without, exact | with, exact |
|---|---|---|---|---|
| composite | +0.0 [-17.6, +17.6] | +27.8 [+1.3, +51.1] | +5.6 [-24.7, +34.4] | +5.6 [-16.2, +27.7] |
| all | +0.0 [-17.6, +17.6] | +27.8 [+1.3, +51.1] | +5.6 [-24.7, +34.4] | +5.6 [-16.2, +27.7] |

## Cost

- Agent and judge: US$ 0.0061 (144 calls).
- The cell's models (ledger): US$ 0.0023.
