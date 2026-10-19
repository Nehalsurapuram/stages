"""Run sandbox gates + decide for selected scripted candidates and record evidence rows (no deploy).

    python tools/gate_candidates.py --kind ineffective F06 F01 F04

Diagnosis is rule mode on the incident.json of the fault's latest orchestrator run (runs/<fault>-rule-*/),
so it is the same evidence the pipeline itself saw. Selecting candidates by file name is the tool choosing
what to run; decide() never sees it. Nothing is deployed: rows record deployed=false.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline import record, sandbox  # noqa: E402
from pipeline.decide import decide, patch_meta  # noqa: E402
from pipeline.observe import load_config  # noqa: E402
from pipeline.providers import get_provider  # noqa: E402


def latest_incident(fault: str) -> dict:
    runs = sorted((REPO / "runs").glob(f"{fault.lower()}-rule-*/incident.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit(f"no orchestrator incident for {fault}; run python -m pipeline run --fault {fault} first")
    return json.loads(runs[-1].read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, help="scripted candidate file name without .diff")
    ap.add_argument("faults", nargs="+")
    args = ap.parse_args()
    cfg = load_config()
    run_id = f"gates-{args.kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}".lower()
    for fault in args.faults:
        incident = latest_incident(fault)
        diagnosis = get_provider("rule").diagnose(incident, {})
        patch = next(p for p in get_provider("scripted").draft_patches(diagnosis, {}, fault, n=None)
                     if p.candidate_kind == args.kind)
        print(f"{fault} {patch.candidate_id} (applies={patch.applies}) diagnosis={diagnosis.causes[0].file}:"
              f"{diagnosis.causes[0].line} oracle={diagnosis.oracle_localisation}", flush=True)
        gates = sandbox.run_gates(fault, patch, run_id)
        meta = patch_meta(patch.diff)
        dec = decide(gates, diagnosis, meta, cfg)
        (REPO / "runs" / run_id / patch.candidate_id / "decision.json").write_text(
            json.dumps({**dec.to_dict(), "patch_meta": meta}, indent=2), encoding="utf-8")
        row = record.build_row(run_id=run_id, fault_id=fault, gates=gates, diagnosis=diagnosis, patch=patch,
                               patch_meta=meta, decision=dec, deployed=False, rolled_back=None,
                               awaiting_review=dec.decision == "ESCALATE", recovery_s=None)
        record.append_row(row)
        print("   " + "  ".join(f"{g}={gates['gates'][g]['status']}:{gates['gates'][g]['seconds']}s"
                                for g in sandbox.GATES) + f"  -> {dec.tier} {dec.decision}: {'; '.join(dec.reasons)}",
              flush=True)
    print(f"evidence rows appended to results/evidence.jsonl (run {run_id})")


if __name__ == "__main__":
    main()
