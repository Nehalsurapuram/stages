import ast
from pathlib import Path

import pytest
import yaml

from pipeline import decide as decide_mod
from pipeline.decide import decide, patch_meta, policy_version

REPO = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((REPO / "config/decision.yaml").read_text(encoding="utf-8"))
SVC = "app/src/main/java/com/autoimmune/orders/OrderService.java"
CTL = "app/src/main/java/com/autoimmune/orders/OrderController.java"
PROPS = "app/src/main/resources/application.properties"
SQL = {"rule_id": "sql-string-concatenation", "file": SVC, "line": 43, "severity": "ERROR", "message": "m"}
SECRET = {"rule_id": "hardcoded-secret", "file": SVC, "line": 12, "severity": "WARNING", "message": "m"}


def gates(v0="pass", v1="pass", v2="pass", v3="pass", findings=()):
    g = {name: {"status": s, "seconds": 1.0, "exit_code": 0 if s == "pass" else 1, "log_tail": ""}
         for name, s in (("V0", v0), ("V1", v1), ("V2", v2), ("V3", v3))}
    g["V3"]["new_findings"] = list(findings) if v3 in ("pass", "fail") else None
    return {"run_id": "r", "fault_id": "F06", "candidate_id": "F06-scripted-abc", "gates": g}


def diag(file=SVC, mode="rule", **extra):
    causes = [{"rank": 1, "file": file, "line": None, "reason": "r", "evidence": "e"}] if file else []
    return {"provider_mode": mode, "causes": causes, **extra}


def meta(files=(SVC,), lines=2):
    return {"files_touched": list(files), "diff_lines": lines}


def run(**kw):
    return decide(kw.get("g", gates()), kw.get("d", diag()), kw.get("m", meta()), kw.get("c", CONFIG))


# ---------------------------------------------------------------- REJECT

@pytest.mark.parametrize("statuses, reason", [
    ({"v0": "fail", "v1": "not_run", "v2": "not_run", "v3": "not_run"}, "V0 (patch applies and compiles) failed"),
    ({"v0": "error"}, "V0 (patch applies and compiles) hit a tool error, which is not a pass"),
    ({"v1": "fail"}, "V1 (target test) failed"),
    ({"v1": "timeout"}, "V1 (target test) timed out, which is not a pass"),
    ({"v1": "not_run"}, "V1 (target test) was not run, which is not a pass"),
    ({"v2": "fail", "v3": "not_run"}, "V2 (existing test suite) failed"),
    ({"v2": "error"}, "V2 (existing test suite) hit a tool error, which is not a pass"),
    ({"v2": "skipped"}, "V2 (existing test suite) has status 'skipped'"),
    ({"v3": "error"}, "V3 (security scan) hit a tool error, which is not a pass"),
    ({"v3": "timeout"}, "V3 (security scan) timed out, which is not a pass"),
    ({"v3": "not_run"}, "V3 (security scan) was not run, which is not a pass"),
])
def test_reject_when_a_gate_did_not_pass(statuses, reason):
    d = run(g=gates(**statuses))
    assert (d.tier, d.decision, d.reasons) == ("high", "REJECT", (reason,))


def test_reject_reports_only_the_first_failed_gate_even_with_findings():
    d = run(g=gates(v0="pass", v1="fail", v2="not_run", v3="not_run"))
    assert d.reasons == ("V1 (target test) failed",)


def test_missing_gate_counts_as_not_run():
    g = gates()
    del g["gates"]["V2"]
    assert run(g=g).reasons == ("V2 (existing test suite) was not run, which is not a pass",)


def test_reject_on_new_high_severity_finding():
    d = run(g=gates(v3="fail", findings=[SQL]))
    assert (d.tier, d.decision) == ("high", "REJECT")
    assert d.reasons == ("V3 (security scan) found 1 new high-severity finding(s): "
                         f"sql-string-concatenation at {SVC}:43 (ERROR)",)


@pytest.mark.parametrize("severity", ["ERROR", "high", "Critical"])
def test_high_severity_names_reject_even_if_v3_status_says_pass(severity):
    d = run(g=gates(v3="pass", findings=[dict(SQL, severity=severity)]))
    assert d.decision == "REJECT" and "new high-severity finding" in d.reasons[0]


def test_v3_pass_without_findings_list_is_not_trusted():
    g = gates()
    g["gates"]["V3"]["new_findings"] = None
    assert run(g=g).reasons == ("V3 (security scan) passed without a findings list, so the scan cannot be trusted",)


def test_bare_gates_section_is_accepted():
    assert run(g=gates(v1="fail")["gates"]).decision == "REJECT"


# ---------------------------------------------------------------- ESCALATE

def test_escalate_on_new_lower_severity_finding():
    d = run(g=gates(findings=[SECRET]))
    assert (d.tier, d.decision) == ("medium", "ESCALATE")
    assert d.reasons == (f"V3 (security scan) found 1 new lower-severity finding(s): hardcoded-secret at {SVC}:12 (WARNING)",)


def test_escalate_when_diff_is_larger_than_limit_but_not_at_limit():
    assert run(m=meta(lines=20)).decision == "AUTO_APPLY"
    d = run(m=meta(lines=21))
    assert d.decision == "ESCALATE" and d.reasons == ("diff changes 21 lines, more than max_diff_lines (20)",)


def test_escalate_when_a_file_outside_the_diagnosed_file_is_touched():
    d = run(m=meta(files=[SVC, CTL]))
    assert d.reasons == (f"diff touches {CTL}, outside the diagnosed file {SVC}",)


def test_diagnosed_file_is_the_rank_1_cause():
    d = diag()
    d["causes"] = [{"rank": 2, "file": CTL}, {"rank": 1, "file": SVC}]
    assert run(d=d).decision == "AUTO_APPLY"


@pytest.mark.parametrize("path, pattern", [
    (PROPS, "application.properties"),
    ("app/src/main/java/com/autoimmune/orders/security/AuthFilter.java", "security/"),
])
def test_escalate_on_sensitive_path(path, pattern):
    d = run(d=diag(file=path), m=meta(files=[path]))
    assert d.reasons == (f"diff touches a sensitive path: {path} (matches '{pattern}')",)


def test_sensitive_patterns_match_whole_path_parts_only():
    for path in ("app/src/main/java/insecurity/X.java", "app/src/main/resources/my-application.properties"):
        assert run(d=diag(file=path), m=meta(files=[path])).decision == "AUTO_APPLY", path


@pytest.mark.parametrize("mode", ["live", "replay"])
def test_escalate_when_llm_diagnosis_samples_disagree(mode):
    d = run(d=diag(mode=mode, samples_disagree=True))
    assert d.reasons == ("diagnosis samples disagree on the cause",)
    assert run(d=diag(mode=mode, samples_disagree=False)).decision == "AUTO_APPLY"


def test_disagreement_is_ignored_for_rule_diagnosis():
    assert run(d=diag(mode="rule", samples_disagree=True)).decision == "AUTO_APPLY"


def test_escalate_when_diagnosis_names_no_file():
    assert run(d=diag(file=None)).reasons == ("diagnosis names no file, so the diff cannot be checked against it",)


def test_escalate_reasons_accumulate_in_order():
    d = run(g=gates(findings=[SECRET]), d=diag(mode="live", samples_disagree=True), m=meta(files=[SVC, PROPS], lines=30))
    assert d.decision == "ESCALATE"
    assert d.reasons == (
        f"V3 (security scan) found 1 new lower-severity finding(s): hardcoded-secret at {SVC}:12 (WARNING)",
        "diff changes 30 lines, more than max_diff_lines (20)",
        f"diff touches {PROPS}, outside the diagnosed file {SVC}",
        f"diff touches a sensitive path: {PROPS} (matches 'application.properties')",
        "diagnosis samples disagree on the cause",
    )


# ---------------------------------------------------------------- AUTO_APPLY

def test_auto_apply_when_everything_is_inside_the_envelope():
    d = run()
    assert (d.tier, d.decision) == ("low", "AUTO_APPLY")
    assert d.reasons == (f"all gates passed with no new findings; 2-line diff inside the diagnosed file {SVC}",)
    assert d.policy_version == policy_version(CONFIG) and d.policy_version.startswith("decide-v1+")


def test_policy_version_changes_with_thresholds():
    assert policy_version(CONFIG) != policy_version(dict(CONFIG, max_diff_lines=10))


# ---------------------------------------------------------------- independence from what a candidate "is"

@pytest.mark.parametrize("g", [gates(), gates(v2="fail", v3="not_run"), gates(v3="fail", findings=[SQL])])
def test_decision_identical_when_candidate_kind_changes(g):
    results = set()
    for kind in ("correct", "overfitting", "regressive", "insecure", "slow", None):
        gk = {**g, "candidate_kind": kind, "label": kind}
        mk = {**meta(), "candidate_kind": kind}
        dk = {**diag(), "candidate_kind": kind}
        results.add(decide(gk, dk, mk, CONFIG))
    assert len(results) == 1


def test_decide_code_never_mentions_kinds_labels_hidden_tests_or_candidate_files():
    tree = ast.parse(Path(decide_mod.__file__).read_text(encoding="utf-8"))
    tree.body = tree.body[1:]                                   # drop the module docstring
    code = ast.unparse(tree).lower()
    for word in ("candidate_kind", "label", "hidden", "candidates/", "faults/", "open(", "read_text"):
        assert word not in code, word


# ---------------------------------------------------------------- patch_meta

def test_patch_meta_from_real_diffs():
    over = (REPO / "faults/F04/candidates/overfitting.diff").read_text(encoding="utf-8")
    assert patch_meta(over) == {"files_touched": ["app/src/main/java/com/autoimmune/orders/CatalogHealthIndicator.java"],
                                "diff_lines": 5}
    ins = (REPO / "faults/F04/candidates/insecure.diff").read_text(encoding="utf-8")
    assert patch_meta(ins)["files_touched"] == [CTL, PROPS]


def test_patch_meta_ignores_blank_lines_and_counts_new_files():
    diff = ("diff --git a/app/New.java b/app/New.java\nnew file mode 100644\n--- /dev/null\n+++ b/app/New.java\n"
            "@@ -0,0 +1,2 @@\n+class New {\n+}\n\n")
    assert patch_meta(diff) == {"files_touched": ["app/New.java"], "diff_lines": 2}
