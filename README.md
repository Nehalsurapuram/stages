# AutoImmune

AutoImmune is a B.Tech research prototype: a hybrid self-healing pipeline for a Java Spring Boot service (order-service). It observes the service, detects and diagnoses a fault, drafts three candidate patches, and validates each one in a Docker sandbox through four gates (V0 compile, V1 target test, V2 full test suite, V3 new Semgrep findings). A rule-based risk tier then decides to REJECT, ESCALATE or AUTO_APPLY. Auto-applied patches are deployed, watched over a health window, and either promoted or rolled back, with evidence recorded for every candidate. The research question is how much execution evidence lets an AI-generated patch deploy without human review, and what each gate catches.

## Team

- Agni M N
- Pradeep S
- S Nehal

**Guide:** Mrs. M. Shona

## Running tests

```
.\.venv\Scripts\python -m pytest
```
