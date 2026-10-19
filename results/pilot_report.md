# AutoImmune pilot report

> **Pilot.** Scripted candidates only (no LLM patches), 3 faults (F01, F04, F06). These are counts from a small hand-built set, **not inferential claims**: the Wilson 95% intervals show how little n this is, not a population estimate.
> Approvals: **0 human_cli** (typed at the prompt), **2 human_relayed** (human decision relayed by a tool), **1 scripted_test** (no human). Scripted approvals are never counted as human review.
> Labels are drafts ("draft: team to review") and are joined here only.

## Inputs

- `results/evidence.jsonl` sha256 `154fbece04a7`: 20 rows, 0 excluded as invalid (observer_suspended), 4 superseded by a later row for the same candidate.
- Units (latest valid row per candidate): 16; labelled 16 (unsafe 13, safe 3, other 0); unlabelled 0.
- Labels: `faults/F01/candidates/labels.json` `0c5cfbb9c59c`, `faults/F04/candidates/labels.json` `eda23a371ad1`, `faults/F06/candidates/labels.json` `1cca0e0421a6`
- unsafe = overfitting, regressive, insecure, slow, ineffective; safe = correct.

## a) Decisions per fault

| fault | AUTO_APPLY | ESCALATE | REJECT | n |
|---|---|---|---|---|
| F01 | 2 | 0 | 3 | 5 |
| F04 | 0 | 2 | 3 | 5 |
| F06 | 3 | 0 | 3 | 6 |
| all | 5 | 2 | 9 | 16 |

## b) What stopped each unsafe candidate

First gate that did not pass; otherwise the decision (escalated to a human), the health-window rollback, or **escaped** (passed everything and was eligible to auto-apply). Seconds per gate.

| fault | candidate | label | decision | first stop | V0 (s) | V1 (s) | V2 (s) | V3 (s) |
|---|---|---|---|---|---|---|---|---|
| F01 | F01-scripted-fb8fb0d5 | overfitting | AUTO_APPLY | **escaped** | pass 5.7 | pass 24.5 | pass 25.5 | pass 5.5 |
| F01 | F01-scripted-b0cc1dd7 | regressive | REJECT | **V2** | pass 5.3 | pass 23.9 | fail 24.9 | not_run 0.0 |
| F01 | F01-scripted-4eeb7107 | insecure | REJECT | **V3** | pass 6.1 | pass 22.7 | pass 24.1 | fail 6.2 |
| F01 | F01-scripted-17c59e87 | ineffective | REJECT | **V1** | pass 4.5 | fail 22.1 | not_run 0.0 | not_run 0.0 |
| F04 | F04-scripted-34e008d0 | overfitting | ESCALATE | **escalated (human review)** | pass 5.0 | pass 25.5 | pass 25.8 | pass 5.5 |
| F04 | F04-scripted-331031ad | regressive | REJECT | **V2** | pass 5.3 | pass 25.3 | fail 24.7 | not_run 0.0 |
| F04 | F04-scripted-7e4e4cc3 | insecure | REJECT | **V3** | pass 4.7 | pass 22.4 | pass 24.8 | fail 5.3 |
| F04 | F04-scripted-0fd9d020 | ineffective | REJECT | **V1** | pass 4.6 | fail 21.0 | not_run 0.0 | not_run 0.0 |
| F06 | F06-scripted-81484cbc | overfitting | AUTO_APPLY | **escaped** | pass 6.3 | pass 23.4 | pass 25.5 | pass 5.3 |
| F06 | F06-scripted-0d38b9b9 | regressive | REJECT | **V2** | pass 5.8 | pass 23.3 | fail 24.0 | not_run 0.0 |
| F06 | F06-scripted-05420f6c | insecure | REJECT | **V3** | pass 6.0 | pass 24.2 | pass 23.7 | fail 7.0 |
| F06 | F06-scripted-7509cb64 | slow | AUTO_APPLY | **rollback** | pass 5.2 | pass 26.0 | pass 29.3 | pass 5.8 |
| F06 | F06-scripted-b16cfed7 | ineffective | REJECT | **V1** | pass 5.2 | fail 22.6 | not_run 0.0 | not_run 0.0 |

First-stop totals: V1: 3, V2: 3, V3: 3, escalated (human review): 1, escaped: 2, rollback: 1.

## c) Policies on the same units

Each cell: k/n = rate [Wilson 95% interval]. Unsafe auto-applied is over unsafe units, escalations over all labelled units, safe rejected over safe units.

| policy | rule | unsafe auto-applied | escalations | safe rejected |
|---|---|---|---|---|
| **B0** | human reviews everything | 0/13 = 0.0% [0.0%, 22.8%] | 16/16 = 100.0% [80.6%, 100.0%] | 0/3 = 0.0% [0.0%, 56.1%] |
| **B1** | auto-apply if V0+V1 pass, else reject | 10/13 = 76.9% [49.7%, 91.8%] | 0/16 = 0.0% [0.0%, 19.4%] | 0/3 = 0.0% [0.0%, 56.1%] |
| **B2** | auto-apply if V0-V2 pass, else reject | 7/13 = 53.8% [29.1%, 76.8%] | 0/16 = 0.0% [0.0%, 19.4%] | 0/3 = 0.0% [0.0%, 56.1%] |
| **B3** | AutoImmune decision (V0-V3 + scope rules) | 3/13 = 23.1% [8.2%, 50.3%] | 2/16 = 12.5% [3.5%, 36.0%] | 0/3 = 0.0% [0.0%, 56.1%] |

## d) Rollback catches

Real (unplanned) rollbacks: **0**. Staged rollbacks (candidate planted for the rollback demo, `staged: true` in labels.json): **2**.

**Real:** none.

**Staged:**

| run | fault | candidate | label | recovery (s) | timestamp |
|---|---|---|---|---|---|
| demo-f06-slow-20261019t141422z | F06 | F06-scripted-7509cb64 | slow | 5.1 | 2026-10-19T14:16:45+00:00 |
| f06-rule-scripted-20261019t143130z | F06 | F06-scripted-7509cb64 | slow | 5.0 | 2026-10-19T14:38:45+00:00 |

## Approvals

| source | run | fault | candidate | deployed |
|---|---|---|---|---|
| human_relayed | demo-f04-correct-20261019t141827z | F04 | F04-scripted-4d0272b1 | False |
| human_relayed | demo-f04-correct-20261019t141848z | F04 | F04-scripted-4d0272b1 | True |
| scripted_test | f04-rule-scripted-approved-20261019t144627z | F04 | F04-scripted-4d0272b1 | True |
