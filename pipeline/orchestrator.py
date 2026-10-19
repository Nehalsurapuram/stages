"""One end-to-end run for one fault. Fixed pipeline, no agent loop:

     1 banner                       6 choose one candidate (no candidate_kind, no labels)
     2 baseline running + healthy   7 deploy + health window -> promote or roll back
     3 faulty build + detector      8 one evidence row per candidate
     4 diagnose + draft patches     9 restore the baseline
     5 gates + decide per candidate 10 summary table

    python -m pipeline run --fault F06 --diag-mode rule --patch-mode scripted [--candidate ID] [--deploy-each]

Every step writes into runs/<run_id>/. A failing step stops the run with the real error (error.json);
the baseline is restored in any case.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline import deploy, detector, record, sandbox
from pipeline.decide import decide, patch_meta
from pipeline.providers import get_provider

REPO = Path(__file__).resolve().parents[1]
GATES = sandbox.GATES


class StepError(RuntimeError):
    def __init__(self, step: int, title: str, cause: BaseException):
        super().__init__(f"step {step} ({title}) failed: {type(cause).__name__}: {cause}")
        self.step, self.title, self.cause = step, title, cause


@dataclass
class Selection:
    chosen: list[dict]      # entries to deploy, in order
    note: str


def _rank(e: dict) -> tuple:
    return (e["diff_lines"], e["candidate_id"])


def choose(entries: list[dict], override: str | None = None, deploy_each: bool = False) -> Selection:
    """Pick what to deploy from {candidate_id, decision, diff_lines} entries; no other field is read.

    AUTO_APPLY with the fewest diff lines (tie: candidate_id); --deploy-each: every AUTO_APPLY in that order.
    No AUTO_APPLY: the first ESCALATE in the same order goes to human review. Neither: no safe candidate.
    override (--candidate) picks one id for demos; deploy.release still refuses REJECT and unapproved ESCALATE.
    """
    view = [{"candidate_id": e["candidate_id"], "decision": e["decision"], "diff_lines": e["diff_lines"]}
            for e in entries]
    if override:
        match = [e for e in view if e["candidate_id"] == override]
        if not match:
            raise ValueError(f"--candidate {override} is not one of {[e['candidate_id'] for e in view]}")
        return Selection(match, f"--candidate {override} chosen by the operator ({match[0]['decision']})")
    auto = sorted((e for e in view if e["decision"] == "AUTO_APPLY"), key=_rank)
    if auto:
        if deploy_each:
            return Selection(auto, f"--deploy-each: all {len(auto)} AUTO_APPLY candidates, fewest diff lines first")
        return Selection(auto[:1], f"AUTO_APPLY with the fewest diff lines ({auto[0]['diff_lines']})")
    esc = sorted((e for e in view if e["decision"] == "ESCALATE"), key=_rank)
    if esc:
        return Selection(esc[:1], "no AUTO_APPLY candidate; the first ESCALATE candidate goes to human review")
    return Selection([], "no safe candidate: every candidate was REJECTED")


def keep_awake() -> None:
    """Windows: keep the host from idle-sleeping while this process runs (process-scoped, no settings change)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _source_files(app_dir: Path) -> dict[str, str]:
    """Production sources of the deployed (faulty) build, keyed by repo path. Never tests, never faults/."""
    files = {}
    for p in sorted((app_dir / "src" / "main").rglob("*")):
        if p.is_file() and p.suffix in (".java", ".properties"):
            files["app/" + p.relative_to(app_dir).as_posix()] = p.read_text(encoding="utf-8")
    return files


def _result(rel: dict | None, decision: str) -> str:
    if rel is None:
        return {"REJECT": "not deployed (REJECT)", "ESCALATE": "awaiting review"}.get(decision, "not chosen")
    if not rel["deployed"]:
        return "awaiting review (not approved)" if rel.get("awaiting_review") else rel.get("reason", "not deployed")
    if rel["outcome"] == "promoted":
        return "promoted"
    if rel["outcome"] == "rolled_back":
        return f"rolled back in {rel['recovery_s']}s: {rel['reason']}"
    return rel["outcome"]


def print_table(rows: list[dict]) -> None:
    head = ["candidate", "V0", "V1", "V2", "V3", "tier", "decision", "deployed", "result"]
    body = [[r["candidate_id"], *[r["gates"][g] for g in GATES], r["tier"], r["decision"],
             "yes" if r["deployed"] else "no", r["result"]] for r in rows]
    widths = [max(len(str(x)) for x in col) for col in zip(head, *body)]
    line = lambda cells: "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"  # noqa: E731
    print(line(head))
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for b in body:
        print(line(b))


def run(fault: str, diag_mode: str, patch_mode: str, *, candidate: str | None = None, deploy_each: bool = False,
        run_id: str | None = None, input_fn=input, approval_source: str = "human_cli") -> dict:
    """approval_source: "human_cli" when a person answers approve_cli at the terminal; callers that feed the
    answer through input_fn from a script must pass "scripted_test"."""
    cfg = deploy.load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = (run_id or f"{fault}-{diag_mode}-{patch_mode}-{stamp}").lower()
    run_dir = REPO / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    keep_awake()
    t0 = time.monotonic()
    state = {"step": 1, "title": "banner"}

    def step(n: int, title: str) -> None:
        state.update(step=n, title=title)
        print(f"\n[{n}/10] {title}  (+{time.monotonic() - t0:.0f}s)", flush=True)

    print("=" * 78)
    print(f"AutoImmune run {run_id}")
    print(f"fault={fault}  diag_mode={diag_mode}  patch_mode={patch_mode}"
          + ("" if {diag_mode, patch_mode} & {"live", "replay"} else "   (no LLM: rule/scripted results)"))
    print("=" * 78, flush=True)
    _write(run_dir / "run.json", {"run_id": run_id, "fault_id": fault, "diag_mode": diag_mode,
                                  "patch_mode": patch_mode, "candidate": candidate, "deploy_each": deploy_each,
                                  "started_at": stamp, "config": cfg})
    candidates: list[dict] = []
    releases: dict[str, dict] = {}
    approvals: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="autoimmune-run-") as tmp:
            step(2, "baseline running and healthy")
            if deploy.current_image() == deploy.BASELINE_TAG and deploy._health(cfg)["ok"]:
                info = {"image": deploy.BASELINE_TAG, "already_running": True}
            else:
                info = deploy.start_baseline(cfg=cfg)
            _write(run_dir / "baseline.json", info)
            print(f"  {info}")

            step(3, "deploy the faulty build (inject.diff) and watch for an incident")
            faulty_src = sandbox.prepare_patched_app(fault, None, Path(tmp) / "faulty")
            faulty = deploy.deploy(faulty_src, f"{run_id}-faulty", cfg)
            _write(run_dir / "faulty_deploy.json", {k: v for k, v in faulty.items() if k != "swap_clock"})
            print(f"  running {faulty['new_image']} (answering after {faulty['swap_to_answer_s']}s)", flush=True)
            incident = detector.watch(cfg.get("detect_timeout_s", 60), run_id=run_id, fault_id=fault)
            if incident is None:
                raise RuntimeError(f"no incident within {cfg.get('detect_timeout_s', 60)}s")
            if incident.get("status") == "invalid":
                raise RuntimeError(f"detector invalid: {incident['invalid']} ({incident['sign']})")
            print(f"  incident: rule={incident['rule']} endpoint={incident['endpoint']} "
                  f"status={incident['status']} -> runs/{run_id}/incident.json")

            step(4, f"diagnose ({diag_mode}) and draft patches ({patch_mode})")
            sources = _source_files(faulty_src)
            diagnosis = get_provider(diag_mode).diagnose(incident, sources)
            _write(run_dir / "diagnosis.json", diagnosis.to_dict())
            top = diagnosis.causes[0]
            print(f"  diagnosis: {top.file}:{top.line} (oracle_localisation={diagnosis.oracle_localisation})")
            patches = get_provider(patch_mode).draft_patches(diagnosis, sources, fault,
                                                             n=None if patch_mode == "scripted" else 3)
            _write(run_dir / "patches.json", [p.to_dict() for p in patches])
            print(f"  {len(patches)} candidates: {', '.join(p.candidate_id for p in patches)}")

            step(5, "gates V0-V3 and decision for every candidate (sandbox, one at a time)")
            for p in patches:
                gates = sandbox.run_gates(fault, p, run_id)
                meta = patch_meta(p.diff)
                dec = decide(gates, diagnosis, meta, cfg)
                _write(run_dir / p.candidate_id / "decision.json", {**dec.to_dict(), "patch_meta": meta})
                candidates.append({"patch": p, "gates": gates, "meta": meta, "decision": dec})
                print(f"  {p.candidate_id}: " + " ".join(f"{g}={gates['gates'][g]['status']}" for g in GATES)
                      + f" -> {dec.decision}", flush=True)

            step(6, "choose a candidate (decision and diff size only)")
            sel = choose([{"candidate_id": c["patch"].candidate_id, "decision": c["decision"].decision,
                           "diff_lines": c["meta"]["diff_lines"]} for c in candidates], candidate, deploy_each)
            _write(run_dir / "choice.json", {"note": sel.note, "chosen": sel.chosen})
            print(f"  {sel.note}: {[e['candidate_id'] for e in sel.chosen] or 'none'}")

            step(7, "deploy + health window -> promote or roll back")
            by_id = {c["patch"].candidate_id: c for c in candidates}
            for i, e in enumerate(sel.chosen):
                c = by_id[e["candidate_id"]]
                cid, dec = e["candidate_id"], c["decision"]
                if i and deploy.current_image() != faulty["new_image"]:
                    print(f"  restoring the faulty build {faulty['new_image']} before the next candidate", flush=True)
                    deploy.switch_to(faulty["new_image"], cfg, healthy=False)
                approval = None
                if dec.decision == "ESCALATE":
                    approval = deploy.approve_cli(dec, c["patch"].diff, input_fn=input_fn, run_dir=run_dir / cid,
                                                  source=approval_source)
                    approvals[cid] = approval
                    print(f"  approval: {approval['answer']} by {approval['user']} at {approval['time']} "
                          f"({approval['approval_source']})")
                print(f"  {cid}: deploying ({dec.decision})", flush=True)
                src = sandbox.prepare_patched_app(fault, c["patch"].diff, Path(tmp) / cid)
                rel = deploy.release(dec, src, f"{run_id}-{cid}", approval=approval, run_dir=run_dir / cid, cfg=cfg)
                releases[cid] = rel
                print(f"  {cid}: {_result(rel, dec.decision)}", flush=True)

            step(8, "record one evidence row per candidate")
            for c in candidates:
                cid, rel = c["patch"].candidate_id, releases.get(c["patch"].candidate_id)
                row = record.build_row(
                    run_id=run_id, fault_id=fault, gates=c["gates"], diagnosis=diagnosis, patch=c["patch"],
                    patch_meta=c["meta"], decision=c["decision"],
                    deployed=bool(rel and rel["deployed"]),
                    rolled_back=rel.get("rolled_back") if rel else None,
                    awaiting_review=(rel.get("awaiting_review") if rel else c["decision"].decision == "ESCALATE"),
                    recovery_s=rel.get("recovery_s") if rel else None,
                    approval=approvals.get(cid),
                    invalid="observer_suspended" if rel and str(rel.get("outcome", "")).startswith("invalid") else None)
                record.append_row(row)
                record.append_row(row, run_dir / "evidence.jsonl")
            print(f"  {len(candidates)} rows -> results/evidence.jsonl and runs/{run_id}/evidence.jsonl")
    except Exception as exc:
        err = StepError(state["step"], state["title"], exc)
        _write(run_dir / "error.json", {"step": state["step"], "title": state["title"], "error": str(exc),
                                        "type": type(exc).__name__, "traceback": traceback.format_exc()})
        _restore_baseline(cfg, step)
        raise err from exc

    _restore_baseline(cfg, step)
    step(10, "summary")
    rows = []
    for c in candidates:
        cid, rel = c["patch"].candidate_id, releases.get(c["patch"].candidate_id)
        rows.append({"candidate_id": cid, "gates": {g: c["gates"]["gates"][g]["status"] for g in GATES},
                     "tier": c["decision"].tier, "decision": c["decision"].decision,
                     "deployed": bool(rel and rel["deployed"]), "result": _result(rel, c["decision"].decision)})
    summary = {"run_id": run_id, "fault_id": fault, "diag_mode": diag_mode, "patch_mode": patch_mode,
               "incident_rule": incident["rule"], "diagnosed_file": diagnosis.causes[0].file,
               "oracle_localisation": diagnosis.oracle_localisation, "choice": sel.note,
               "seconds": round(time.monotonic() - t0), "candidates": rows}
    _write(run_dir / "summary.json", summary)
    print(f"run {run_id}  fault {fault}  diag_mode={diag_mode}  patch_mode={patch_mode}  "
          f"incident={incident['rule']}  ({summary['seconds']}s)")
    print_table(rows)
    return summary


def _restore_baseline(cfg: dict, step) -> None:
    step(9, "restore the baseline")
    try:
        if deploy.current_image() == deploy.BASELINE_TAG and deploy._health(cfg)["ok"]:
            print("  baseline already running and healthy")
            return
        seconds = deploy.switch_to(deploy.BASELINE_TAG, cfg, healthy=True)
        print(f"  {deploy.BASELINE_TAG} running and healthy after {seconds:.1f}s")
    except Exception as exc:   # report, but do not hide the original error
        print(f"  could not restore the baseline: {type(exc).__name__}: {exc}", file=sys.stderr)
