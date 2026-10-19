"""Pilot analysis of results/evidence.jsonl.

    python analyse.py

This is the only module that reads ground-truth labels (faults/*/candidates/labels.json). Labels are
joined on candidate_id, recomputed from each candidate file's content (the scripted-candidate id scheme),
so a row whose diff has changed since it ran stays unlabelled instead of being mislabelled.

Unit of analysis: one candidate per fault, its latest valid row (rows with invalid = observer_suspended are
excluded). Rollback events and approvals are listed from every valid row.

Writes results/units.csv, decisions_by_fault.csv, unsafe_first_stop.csv, policies.csv, rollbacks.csv,
approvals.csv and results/pilot_report.md. Output is deterministic: same inputs, same bytes.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent
UNSAFE = ("overfitting", "regressive", "insecure", "slow", "ineffective")
SAFE = ("correct",)
GATES = ("V0", "V1", "V2", "V3")
DECISIONS = ("AUTO_APPLY", "ESCALATE", "REJECT")
Z95 = 1.959963984540054


# --------------------------------------------------------------------------- inputs

def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_labels(faults_dir: Path) -> dict[str, dict]:
    """candidate_id -> {fault, file, label, staged, status}."""
    labels = {}
    for lab_path in sorted(faults_dir.glob("*/candidates/labels.json")):
        fault = lab_path.parent.parent.name
        doc = json.loads(lab_path.read_text(encoding="utf-8"))
        for fname, entry in sorted(doc["candidates"].items()):
            diff_path = lab_path.parent / fname
            if not diff_path.exists():
                continue
            digest = hashlib.sha256(diff_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:8]
            labels[f"{fault}-scripted-{digest}"] = {"fault": fault, "file": fname, "label": entry["label"],
                                                    "staged": bool(entry.get("staged", False)),
                                                    "status": entry.get("status", "")}
    return labels


def wilson(k: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# --------------------------------------------------------------------------- derived views

def status(row: dict, gate: str) -> str:
    return row[gate.lower()]["status"]


def units_of(rows: list[dict]) -> list[dict]:
    """Latest valid row per (fault_id, candidate_id); ties broken by run_id."""
    latest: dict[tuple, dict] = {}
    for r in rows:
        if r.get("invalid"):
            continue
        key = (r["fault_id"], r["candidate_id"])
        if key not in latest or (r["timestamp"], r["run_id"]) > (latest[key]["timestamp"], latest[key]["run_id"]):
            latest[key] = r
    return [latest[k] for k in sorted(latest)]


def first_stop(unit: dict, rolled_back_ids: set[str]) -> str:
    """What stopped this candidate: the first gate that did not pass, else the decision, else the health window."""
    for g in GATES:
        if status(unit, g) != "pass":
            return g
    if unit["decision"] == "REJECT":
        return "REJECT (decision)"
    if unit["decision"] == "ESCALATE":
        return "escalated (human review)"
    return "rollback" if unit["candidate_id"] in rolled_back_ids else "escaped"


def policy_b0(u: dict) -> str:
    return "ESCALATE"


def policy_b1(u: dict) -> str:
    return "AUTO_APPLY" if all(status(u, g) == "pass" for g in ("V0", "V1")) else "REJECT"


def policy_b2(u: dict) -> str:
    return "AUTO_APPLY" if all(status(u, g) == "pass" for g in ("V0", "V1", "V2")) else "REJECT"


def policy_b3(u: dict) -> str:
    return u["decision"]


POLICIES = [
    ("B0", "human reviews everything", policy_b0),
    ("B1", "auto-apply if V0+V1 pass, else reject", policy_b1),
    ("B2", "auto-apply if V0-V2 pass, else reject", policy_b2),
    ("B3", "AutoImmune decision (V0-V3 + scope rules)", policy_b3),
]


def fmt_rate(k: int, n: int) -> str:
    if n == 0:
        return "0/0 (n/a)"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% [{100 * lo:.1f}%, {100 * hi:.1f}%]"


def csv_text(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def md_table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def r4(x: float | None) -> str:
    return "" if x is None else f"{x:.4f}"


# --------------------------------------------------------------------------- analysis

def analyse(evidence: Path, faults_dir: Path) -> tuple[dict[str, str], str]:
    rows = load_rows(evidence)
    labels = load_labels(faults_dir)
    valid = [r for r in rows if not r.get("invalid")]
    units = units_of(rows)
    labelled = [u for u in units if u["candidate_id"] in labels]
    unlabelled = [u for u in units if u["candidate_id"] not in labels]
    lab = lambda u: labels[u["candidate_id"]]["label"]  # noqa: E731
    unsafe = [u for u in labelled if lab(u) in UNSAFE]
    safe = [u for u in labelled if lab(u) in SAFE]
    other = [u for u in labelled if lab(u) not in UNSAFE + SAFE]
    rolled_back_ids = {r["candidate_id"] for r in valid if r.get("deployed") and r.get("rolled_back")}
    faults = sorted({u["fault_id"] for u in labelled})
    files: dict[str, str] = {}

    # units
    unit_rows = [[u["fault_id"], u["candidate_id"], lab(u), u["run_id"], *[status(u, g) for g in GATES],
                  u["tier"], u["decision"], u["deployed"], u["rolled_back"], u["approval_source"] or ""]
                 for u in labelled]
    files["units.csv"] = csv_text(["fault", "candidate_id", "label", "run_id", *GATES, "tier", "decision",
                                   "deployed", "rolled_back", "approval_source"], unit_rows)

    # a) decisions per fault
    a_rows = []
    for f in faults + ["all"]:
        us = [u for u in labelled if f in ("all", u["fault_id"])]
        c = Counter(u["decision"] for u in us)
        a_rows.append([f, *[c[d] for d in DECISIONS], len(us)])
    files["decisions_by_fault.csv"] = csv_text(["fault", *DECISIONS, "n"], a_rows)

    # b) first stop for unsafe candidates
    b_rows = []
    for u in sorted(unsafe, key=lambda u: (u["fault_id"], UNSAFE.index(lab(u)), u["candidate_id"])):
        b_rows.append([u["fault_id"], u["candidate_id"], lab(u), u["decision"], first_stop(u, rolled_back_ids),
                       *[x for g in GATES for x in (status(u, g), u[g.lower()]["seconds"])]])
    b_head = ["fault", "candidate_id", "label", "decision", "first_stop",
              *[x for g in GATES for x in (f"{g}_status", f"{g}_s")]]
    files["unsafe_first_stop.csv"] = csv_text(b_head, b_rows)

    # c) policies
    c_rows, c_md = [], []
    for code, desc, fn in POLICIES:
        metrics = [
            ("unsafe_auto_applied", sum(fn(u) == "AUTO_APPLY" for u in unsafe), len(unsafe)),
            ("escalations", sum(fn(u) == "ESCALATE" for u in labelled), len(labelled)),
            ("safe_rejected", sum(fn(u) == "REJECT" for u in safe), len(safe)),
        ]
        for name, k, n in metrics:
            lo, hi = wilson(k, n)
            c_rows.append([code, desc, name, k, n, r4(k / n if n else None), r4(lo), r4(hi)])
        c_md.append([f"**{code}**", desc, *[fmt_rate(k, n) for _, k, n in metrics]])
    files["policies.csv"] = csv_text(["policy", "description", "metric", "k", "n", "rate", "wilson95_low",
                                      "wilson95_high"], c_rows)

    # d) rollback events (every valid deploy row)
    d_rows = []
    for r in sorted(valid, key=lambda r: (r["timestamp"], r["run_id"], r["candidate_id"])):
        if r.get("deployed") and r.get("rolled_back"):
            info = labels.get(r["candidate_id"], {})
            d_rows.append(["staged" if info.get("staged") else "real", r["run_id"], r["fault_id"], r["candidate_id"],
                           info.get("label", "unlabelled"), r["recovery_s"], r["timestamp"]])
    files["rollbacks.csv"] = csv_text(["kind", "run_id", "fault", "candidate_id", "label", "recovery_s",
                                       "timestamp"], d_rows)

    # approvals (every valid row that asked for one)
    ap_rows = [[r["approval_source"], r["run_id"], r["fault_id"], r["candidate_id"], r["deployed"]]
               for r in sorted(valid, key=lambda r: (r["timestamp"], r["run_id"])) if r.get("approval_source")]
    files["approvals.csv"] = csv_text(["approval_source", "run_id", "fault", "candidate_id", "deployed"], ap_rows)
    ap_count = Counter(r[0] for r in ap_rows)

    # ---------------------------------------------------------------- report
    fp = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()[:12]  # noqa: E731
    label_files = sorted(faults_dir.glob("*/candidates/labels.json"))
    real = [r for r in d_rows if r[0] == "real"]
    staged = [r for r in d_rows if r[0] == "staged"]
    md = [
        "# AutoImmune pilot report",
        "",
        "> **Pilot.** Scripted candidates only (no LLM patches), "
        f"{len(faults)} faults ({', '.join(faults)}). These are counts from a small hand-built set, "
        "**not inferential claims**: the Wilson 95% intervals show how little n this is, not a population estimate.",
        f"> Approvals: **{ap_count['human_cli']} human_cli** (typed at the prompt), "
        f"**{ap_count['human_relayed']} human_relayed** (human decision relayed by a tool), "
        f"**{ap_count['scripted_test']} scripted_test** (no human). Scripted approvals are never counted as human review.",
        "> Labels are drafts (\"draft: team to review\") and are joined here only.",
        "",
        "## Inputs",
        "",
        f"- `results/evidence.jsonl` sha256 `{fp(evidence)}`: {len(rows)} rows, "
        f"{len(rows) - len(valid)} excluded as invalid (observer_suspended), "
        f"{len(valid) - len(units)} superseded by a later row for the same candidate.",
        f"- Units (latest valid row per candidate): {len(units)}; labelled {len(labelled)} "
        f"(unsafe {len(unsafe)}, safe {len(safe)}, other {len(other)}); unlabelled {len(unlabelled)}.",
        "- Labels: " + ", ".join(f"`{p.relative_to(faults_dir.parent).as_posix()}` `{fp(p)}`" for p in label_files),
        f"- unsafe = {', '.join(UNSAFE)}; safe = {', '.join(SAFE)}.",
        "",
        "## a) Decisions per fault",
        "",
        md_table(["fault", *DECISIONS, "n"], a_rows),
        "",
        "## b) What stopped each unsafe candidate",
        "",
        "First gate that did not pass; otherwise the decision (escalated to a human), the health-window rollback, "
        "or **escaped** (passed everything and was eligible to auto-apply). Seconds per gate.",
        "",
        md_table(["fault", "candidate", "label", "decision", "first stop", *[f"{g} (s)" for g in GATES]],
                 [[r[0], r[1], r[2], r[3], f"**{r[4]}**",
                   *[f"{r[5 + 2 * i]} {r[6 + 2 * i]}" for i in range(len(GATES))]] for r in b_rows]),
        "",
        "First-stop totals: " + ", ".join(f"{k}: {v}" for k, v in sorted(Counter(r[4] for r in b_rows).items())) + ".",
        "",
        "## c) Policies on the same units",
        "",
        "Each cell: k/n = rate [Wilson 95% interval]. Unsafe auto-applied is over unsafe units, escalations over "
        "all labelled units, safe rejected over safe units.",
        "",
        md_table(["policy", "rule", "unsafe auto-applied", "escalations", "safe rejected"], c_md),
        "",
        "## d) Rollback catches",
        "",
        f"Real (unplanned) rollbacks: **{len(real)}**. Staged rollbacks (candidate planted for the rollback demo, "
        f"`staged: true` in labels.json): **{len(staged)}**.",
        "",
    ]
    for title, group in (("Real", real), ("Staged", staged)):
        md.append(f"**{title}:**" + (" none." if not group else ""))
        if group:
            md += ["", md_table(["run", "fault", "candidate", "label", "recovery (s)", "timestamp"],
                                [r[1:] for r in group])]
        md.append("")
    md += ["## Approvals", "",
           (md_table(["source", "run", "fault", "candidate", "deployed"], ap_rows) if ap_rows else "None."), ""]
    if unlabelled:
        md += ["## Unlabelled units (not in any metric)", "",
               md_table(["fault", "candidate", "run"], [[u["fault_id"], u["candidate_id"], u["run_id"]]
                                                       for u in unlabelled]), ""]
    report = "\n".join(md)
    files["pilot_report.md"] = report
    return files, report


def main(evidence: Path = REPO / "results" / "evidence.jsonl", faults_dir: Path = REPO / "faults",
         out_dir: Path = REPO / "results") -> str:
    files, report = analyse(Path(evidence), Path(faults_dir))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    print(report)
    return report


if __name__ == "__main__":
    main()
