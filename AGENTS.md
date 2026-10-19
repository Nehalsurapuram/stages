# AutoImmune: rules for AI coding assistants

## Project
B.Tech research prototype. A hybrid self-healing pipeline for a Java Spring Boot service ("order-service").
Flow: observe -> detect -> diagnose -> 3 candidate patches -> validate V0-V3 in Docker -> risk tier -> decide
(REJECT / ESCALATE / AUTO_APPLY) -> deploy -> health window -> promote or roll back -> record evidence.
Research question: how much execution evidence lets an AI patch deploy without human review, and what does each gate catch?

## Stack
- Python 3.12 orchestrator in pipeline/, CLI: `python -m pipeline run --fault F06`.
- Java 17, Spring Boot 3, Maven for app/ only.
- Docker for sandbox and deployment. Semgrep for V3.
- JSON artifacts per run in runs/<run_id>/ and one row per candidate in evidence.jsonl.

## Layout
- app/ order-service (healthy code + tests)
- faults/F01, F04, F05, F06/: fault.json, inject.diff, target_test/, hidden_tests/, candidates/ (scripted: correct, overfitting, regressive, insecure)
- pipeline/: observe.py, detector.py, providers.py, sandbox.py, decide.py, deploy.py, record.py, orchestrator.py, __main__.py
- config/decision.yaml: all thresholds
- cache/: replay cache. runs/: run artifacts. analyse.py at root. tests/: pytest.

## LLM boundary
- A model is used ONLY for diagnose() and draft_patches(). Everything else is ordinary deterministic code.
- No agent loops, no tool calling. Fixed pipeline. Retries are bounded and logged.
- One provider interface with modes: live, replay, scripted, rule. Every artifact records provider_mode
  (diag_mode and patch_mode separately). Scripted and rule results are never reported as LLM results.
- Keys only from environment variables. Never commit secrets.

## Gates (one definition, run inside Docker with --network none)
- V0: patch applies (git apply --check) and mvn compile passes.
- V1: visible target test for the fault passes.
- V2: full existing test suite passes.
- V3: Semgrep; count only findings that are NEW compared with the unpatched code, with severity.
- A timeout or tool error is NOT a pass.

## Decision (rule-based tier, not a probability)
- REJECT (high): V0, V1 or V2 fails, or a new high-severity finding.
- ESCALATE (medium): all gates pass but a new low-severity finding, diff larger than the size limit, a file outside the
  diagnosed file, a config/security-sensitive path, or diagnosis samples disagree.
- AUTO_APPLY (low): all gates pass, no new findings, diff inside the diagnosed file and within the size limit.
- Every decision records tier, decision and reasons. Thresholds in config/decision.yaml.

## Deploy and rollback
- Build a new image from the patched source; keep the previous image tag.
- Health window: /actuator/health UP, response time under the limit, probe responses match expected values.
- Breach -> swap back to the previous image and write a rollback event. Never use git reset/checkout to roll back.
- REJECT happens before deploy; rollback happens after deploy. Record them separately.

## Integrity rules
- hidden_tests/ and labels are never mounted in the sandbox and never shown to the provider or decision engine.
- Never invent results. Unexecuted checks are not passes. Report real errors.
- One module per prompt; add pytest tests; show the commands you ran and their real output.
- Do not commit, push or rewrite history unless asked.
