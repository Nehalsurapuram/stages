import json
import re

import pytest

from pipeline.decide import Decision
from pipeline.record import InvalidEvidenceRow, append_row, build_row, max_severity, validate_row

SVC = "app/src/main/java/com/autoimmune/orders/OrderService.java"


def _gates(v3_findings):
    g = {n: {"status": "pass", "seconds": 1.5, "exit_code": 0, "log_tail": ""} for n in ("V0", "V1", "V2", "V3")}
    g["V3"]["new_findings"] = v3_findings
    return {"gates": g}


def _row(**overrides):
    row = build_row(
        run_id="r1", fault_id="F06",
        gates=_gates([{"rule_id": "x", "file": SVC, "line": 3, "severity": "WARNING"}]),
        diagnosis={"provider_mode": "rule", "oracle_localisation": True, "causes": []},
        patch={"candidate_id": "F06-scripted-abc", "candidate_kind": "correct", "provider_mode": "scripted"},
        patch_meta={"files_touched": [SVC], "diff_lines": 2},
        decision=Decision("medium", "ESCALATE", ("reason",), "decide-v1+abc"),
        timestamp="2026-10-19T12:00:00+00:00")
    row.update(overrides)
    return row


def test_valid_row_is_appended_as_one_json_line(tmp_path):
    path = tmp_path / "evidence.jsonl"
    append_row(_row(), path)
    append_row(_row(candidate_id="F06-scripted-def"), path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row["v0"] == {"status": "pass", "seconds": 1.5}
    assert row["new_findings"] == {"count": 1, "max_severity": "WARNING"}
    assert (row["diag_mode"], row["patch_mode"], row["oracle_localisation"]) == ("rule", "scripted", True)
    assert row["deployed"] is None and row["recovery_s"] is None


@pytest.mark.parametrize("bad, where", [
    ({"label": "correct"}, "(row)"),                              # no labels allowed in evidence
    ({"v2": {"status": "passed", "seconds": 1}}, "v2/status"),
    ({"decision": "MAYBE"}, "decision"),
    ({"reasons": []}, "reasons"),
    ({"diag_mode": "llm"}, "diag_mode"),
    ({"fault_id": "F6"}, "fault_id"),
    ({"timestamp": "yesterday"}, "timestamp"),
    ({"recovery_s": -1}, "recovery_s"),
    ({"approval_source": "human"}, "approval_source"),
    ({"invalid": "maybe"}, "invalid"),
])
def test_invalid_row_is_refused_and_nothing_is_written(tmp_path, bad, where):
    path = tmp_path / "evidence.jsonl"
    with pytest.raises(InvalidEvidenceRow, match=re.escape(f"refused at {where}")):
        append_row(_row(**bad), path)
    assert not path.exists()


def test_missing_field_is_refused():
    row = _row()
    del row["rolled_back"]
    with pytest.raises(InvalidEvidenceRow, match="rolled_back"):
        validate_row(row)


def test_v3_without_findings_list_records_null_count():
    row = build_row(run_id="r", fault_id="F01", gates=_gates(None),
                    diagnosis={"provider_mode": "live"}, patch={"candidate_id": "c", "provider_mode": "live"},
                    patch_meta={"files_touched": [], "diff_lines": 0},
                    decision={"tier": "high", "decision": "REJECT", "reasons": ["x"]})
    validate_row(row)
    assert row["new_findings"] == {"count": None, "max_severity": None} and row["candidate_kind"] is None


@pytest.mark.parametrize("approval, source", [(None, None), ({"answer": "approve", "approval_source": "human_cli"}, "human_cli"),
                                              ({"answer": "approve", "approval_source": "human_relayed"}, "human_relayed"),
                                              ({"answer": "approve", "approval_source": "scripted_test"}, "scripted_test")])
def test_approval_source_and_invalid_are_recorded(approval, source):
    row = build_row(run_id="r", fault_id="F04", gates=_gates([]), diagnosis={"provider_mode": "rule"},
                    patch={"candidate_id": "c", "provider_mode": "scripted"},
                    patch_meta={"files_touched": [], "diff_lines": 1},
                    decision={"tier": "medium", "decision": "ESCALATE", "reasons": ["x"]},
                    approval=approval, invalid="observer_suspended")
    validate_row(row)
    assert row["approval_source"] == source and row["invalid"] == "observer_suspended"


def test_max_severity():
    assert max_severity([{"severity": "INFO"}, {"severity": "ERROR"}, {"severity": "WARNING"}]) == "ERROR"
    assert max_severity([]) is None
