"""Deployment demo driver (real Docker).

    python tools/deploy_demo.py baseline
    python tools/deploy_demo.py record-probes
    python tools/deploy_demo.py candidate F06 correct --gates-run <sandbox run_id>

`candidate` takes a scripted candidate and its saved gates.json, diagnoses with rule mode, decides,
asks for approval on ESCALATE (stdin), deploys, runs the health window, promotes or rolls back, and
appends the evidence row. Choosing the candidate by kind is the demo picking what to show; decide()
only ever sees gates, diagnosis and the diff.
"""
import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline import deploy, record, sandbox  # noqa: E402
from pipeline.decide import decide, patch_meta  # noqa: E402
from pipeline.providers import get_provider  # noqa: E402

INCIDENTS = {
    "F06": {"kind": "probe_mismatch", "fault_id": "F06", "endpoint": "/api/orders/2/discount", "status": 200,
            "expected": "900.00", "actual": "1000.00"},
    "F01": {"kind": "exception", "fault_id": "F01", "endpoint": "/api/orders/search", "status": 500},
    "F04": {"kind": "health_down", "fault_id": "F04", "endpoint": "/actuator/health", "status": 503,
            "expected": "UP", "actual": "DOWN"},
}


def show_window(run_dir: Path) -> None:
    rec = json.loads((run_dir / "deploy.json").read_text(encoding="utf-8"))
    hw = rec["health_window"]
    print(f"health window: {hw['window_s']}s every {hw['interval_s']}s, latency limit {hw['latency_limit_ms']} ms")
    for c in hw["checks"]:
        h = c.get("health", {})
        probes = "  ".join(f"{p['probe']}={p['http']}/{p['ms']}ms{'' if p['body_ok'] else '/WRONG'}"
                           for p in c.get("probes", []))
        print(f"  t={c.get('t_s', '?'):>5}s health={h.get('status')}/{h.get('ms')}ms  {probes}  "
              f"{'OK' if c['ok'] else 'BREACH: ' + '; '.join(c['breaches'])}")
    print(f"outcome: {rec['outcome']}  (new image {rec['new_image']}, previous {rec['previous_image']})")
    rb = run_dir / "rollback.json"
    if rb.exists():
        r = json.loads(rb.read_text(encoding="utf-8"))
        print(f"rollback: restored {r['restored_image']} in {r['seconds_breach_to_healthy']}s from breach to healthy; "
              f"reason: {r['reason']}")


def candidate(fault: str, kind: str, gates_run: str) -> None:
    patches = {p.candidate_kind: p for p in get_provider("scripted").draft_patches(None, {}, fault, n=None)}
    patch = patches[kind]
    index = json.loads((REPO / "runs" / gates_run / "index.json").read_text(encoding="utf-8"))
    gates = json.loads((REPO / "runs" / gates_run / index[f"{fault}/{kind}"] / "gates.json").read_text(encoding="utf-8"))
    assert gates["candidate_id"] == patch.candidate_id, "gates.json is for a different version of this diff"

    cfg = deploy.load_config()
    diagnosis = get_provider("rule").diagnose(INCIDENTS[fault], {})
    meta = patch_meta(patch.diff)
    decision = decide(gates, diagnosis, meta, cfg)
    run_id = f"demo-{fault}-{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}".lower()
    run_dir = REPO / "runs" / run_id / patch.candidate_id
    print(f"run {run_id}  candidate {patch.candidate_id}")
    print("gates: " + "  ".join(f"{g}={gates['gates'][g]['status']}" for g in sandbox.GATES))
    print(f"diagnosis: {diagnosis.causes[0].file} (rule mode, oracle_localisation={diagnosis.oracle_localisation})")
    print(f"decision: {decision.decision} (tier {decision.tier})")
    for r in decision.reasons:
        print(f"  - {r}")

    approval = None
    if decision.decision == "ESCALATE":
        approval = deploy.approve_cli(decision, patch.diff, run_dir=run_dir)
        print(f"approval: {approval['answer']} by {approval['user']} at {approval['time']}")

    with tempfile.TemporaryDirectory(prefix="autoimmune-deploy-") as tmp:
        src = sandbox.prepare_patched_app(fault, patch.diff, Path(tmp))
        result = deploy.release(decision, src, run_id, approval=approval, run_dir=run_dir, cfg=cfg)
    print(f"release: {json.dumps(result)}")
    if result["deployed"]:
        show_window(run_dir)

    row = record.build_row(run_id=run_id, fault_id=fault, gates=gates, diagnosis=diagnosis, patch=patch,
                           patch_meta=meta, decision=decision, deployed=result["deployed"],
                           rolled_back=result.get("rolled_back"), staged=result.get("staged"),
                           recovery_s=result.get("recovery_s"))
    record.append_row(row)
    print(f"evidence row appended to {record.EVIDENCE_PATH.relative_to(REPO)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baseline")
    sub.add_parser("record-probes")
    c = sub.add_parser("candidate")
    c.add_argument("fault")
    c.add_argument("kind")
    c.add_argument("--gates-run", required=True)
    args = ap.parse_args()
    if args.cmd == "baseline":
        print(json.dumps(deploy.start_baseline(), indent=2))
        print(json.dumps(deploy.check_once(deploy.load_config(), deploy.load_probes()), indent=2))
    elif args.cmd == "record-probes":
        deploy.record_probes()
        print((REPO / "config" / "probes.yaml").read_text(encoding="utf-8"))
    else:
        candidate(args.fault, args.kind, args.gates_run)


if __name__ == "__main__":
    main()
