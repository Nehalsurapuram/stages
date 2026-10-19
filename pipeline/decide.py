"""Rule-based decision: risk tier + REJECT / ESCALATE / AUTO_APPLY. Pure functions, no I/O.

    decide(gates, diagnosis, patch_meta, config) -> Decision

Inputs, and only these:
  gates       gates.json from sandbox.run_gates (the whole file or just its "gates" section)
  diagnosis   Diagnosis or dict: provider_mode, causes (rank 1 = diagnosed file), optional samples_disagree
  patch_meta  {"files_touched": [...], "diff_lines": n}, from patch_meta(diff)
  config      parsed config/decision.yaml (max_diff_lines, sensitive_paths)
Rules (AGENTS.md):
  REJECT     (high)    V0, V1 or V2 not "pass"; or V3 not run / errored / timed out; or a new high-severity finding
  ESCALATE   (medium)  all gates pass, but: a new lower-severity finding, diff_lines > max_diff_lines, a touched file
                       other than the diagnosed file, a touched sensitive path, or diagnosis samples disagree
                       (only considered for live/replay diagnoses)
  AUTO_APPLY (low)     otherwise
The decision never depends on what a candidate is meant to be: no candidate kinds, labels, hidden tests or
candidate file names are read here. Only the fields named above are looked at.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

POLICY = "decide-v1"
HIGH_SEVERITIES = {"ERROR", "HIGH", "CRITICAL"}
GATE_NAMES = {
    "V0": "V0 (patch applies and compiles)",
    "V1": "V1 (target test)",
    "V2": "V2 (existing test suite)",
    "V3": "V3 (security scan)",
}
STATUS_PHRASES = {
    "fail": "failed",
    "error": "hit a tool error, which is not a pass",
    "timeout": "timed out, which is not a pass",
    "not_run": "was not run, which is not a pass",
}


@dataclass(frozen=True)
class Decision:
    tier: str
    decision: str
    reasons: tuple[str, ...]
    policy_version: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d


def patch_meta(diff: str) -> dict:
    """files_touched and diff_lines (added + removed lines) from a unified diff."""
    files: list[str] = []
    changed = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            for p in parts[2:4]:
                path = p[2:] if p[:2] in ("a/", "b/") else p
                if path not in files:
                    files.append(path)
        elif line.startswith(("+++ ", "--- ")):
            p = line[4:].split("\t")[0]
            path = p[2:] if p[:2] in ("a/", "b/") else p
            if path != "/dev/null" and path not in files:
                files.append(path)
        elif line[:1] in ("+", "-"):
            changed += 1
    return {"files_touched": files, "diff_lines": changed}


def policy_version(config: dict) -> str:
    material = json.dumps({"max_diff_lines": config["max_diff_lines"],
                           "sensitive_paths": sorted(config["sensitive_paths"])}, sort_keys=True)
    return f"{POLICY}+{hashlib.sha256(material.encode()).hexdigest()[:8]}"


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _diagnosed_file(diagnosis) -> str | None:
    causes = _get(diagnosis, "causes") or []
    ranked = sorted(causes, key=lambda c: _get(c, "rank", 99))
    return _get(ranked[0], "file") if ranked else None


def _sensitive_match(path: str, patterns: list[str]) -> str | None:
    p = "/" + path.replace("\\", "/").lstrip("/")
    for pat in patterns:
        if pat.endswith("/"):
            if f"/{pat}" in p:
                return pat
        elif p.endswith("/" + pat.lstrip("/")):
            return pat
    return None


def _describe(findings: list[dict]) -> str:
    return ", ".join(f"{f.get('rule_id')} at {f.get('file')}:{f.get('line')} ({f.get('severity')})" for f in findings)


def decide(gates: dict, diagnosis, patch_meta: dict, config: dict) -> Decision:
    g = gates.get("gates", gates)
    version = policy_version(config)

    # ---- REJECT: V0-V2 must pass; report the first gate that did not (later ones simply did not run)
    reject: list[str] = []
    for name in ("V0", "V1", "V2"):
        status = (g.get(name) or {}).get("status", "not_run")
        if status != "pass":
            reject.append(f"{GATE_NAMES[name]} {STATUS_PHRASES.get(status, f'has status {status!r}')}")
            break

    v3 = g.get("V3") or {}
    v3_status = v3.get("status", "not_run")
    new = v3.get("new_findings") or []
    high = [f for f in new if str(f.get("severity", "")).upper() in HIGH_SEVERITIES]
    low = [f for f in new if f not in high]
    if not reject:
        if high:
            reject.append(f"{GATE_NAMES['V3']} found {len(high)} new high-severity finding(s): {_describe(high)}")
        elif v3_status != "pass":
            reject.append(f"{GATE_NAMES['V3']} {STATUS_PHRASES.get(v3_status, f'has status {v3_status!r}')}")
        elif v3.get("new_findings") is None:
            reject.append(f"{GATE_NAMES['V3']} passed without a findings list, so the scan cannot be trusted")
    if reject:
        return Decision("high", "REJECT", tuple(reject), version)

    # ---- ESCALATE: everything passed, but the patch is outside the safe envelope
    escalate: list[str] = []
    if low:
        escalate.append(f"{GATE_NAMES['V3']} found {len(low)} new lower-severity finding(s): {_describe(low)}")
    max_lines = config["max_diff_lines"]
    lines = patch_meta["diff_lines"]
    if lines > max_lines:
        escalate.append(f"diff changes {lines} lines, more than max_diff_lines ({max_lines})")
    diagnosed = _diagnosed_file(diagnosis)
    files = patch_meta["files_touched"]
    if diagnosed is None:
        escalate.append("diagnosis names no file, so the diff cannot be checked against it")
    else:
        for f in files:
            if f != diagnosed:
                escalate.append(f"diff touches {f}, outside the diagnosed file {diagnosed}")
    for f in files:
        pat = _sensitive_match(f, config["sensitive_paths"])
        if pat:
            escalate.append(f"diff touches a sensitive path: {f} (matches '{pat}')")
    if _get(diagnosis, "provider_mode") in ("live", "replay") and _get(diagnosis, "samples_disagree") is True:
        escalate.append("diagnosis samples disagree on the cause")
    if escalate:
        return Decision("medium", "ESCALATE", tuple(escalate), version)

    return Decision("low", "AUTO_APPLY",
                    (f"all gates passed with no new findings; {lines}-line diff inside the diagnosed file {diagnosed}",),
                    version)
