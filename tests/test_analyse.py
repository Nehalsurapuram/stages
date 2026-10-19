import csv
import hashlib
import io
import json
from pathlib import Path

import pytest

import analyse


def row(fault, cid, gates=("pass",) * 4, decision="AUTO_APPLY", ts="2026-10-19T10:00:00+00:00", run="r1", **kw):
    r = {"run_id": run, "fault_id": fault, "candidate_id": cid, "decision": decision, "tier": "low",
         "timestamp": ts, "deployed": False, "rolled_back": None, "approval_source": None, "invalid": None,
         "recovery_s": None}
    r.update({g: {"status": s, "seconds": 1.0} for g, s in zip(("v0", "v1", "v2", "v3"), gates)})
    r.update(kw)
    return r


@pytest.fixture
def world(tmp_path):
    """faults/F99 with five candidate files + labels, and an evidence file referencing them by id."""
    cand = tmp_path / "faults" / "F99" / "candidates"
    cand.mkdir(parents=True)
    ids = {}
    for kind in ("correct", "overfitting", "regressive", "slow", "ineffective"):
        text = f"diff for {kind}\n"
        (cand / f"{kind}.diff").write_text(text, encoding="utf-8")
        ids[kind] = "F99-scripted-" + hashlib.sha256(text.encode()).hexdigest()[:8]
    labels = {k: {"label": k, "status": "draft: team to review"} for k in ids}
    labels["slow"]["staged"] = True
    (cand / "labels.json").write_text(json.dumps({"candidates": {f"{k}.diff": v for k, v in labels.items()}}))
    rows = [
        row("F99", ids["correct"]),
        row("F99", ids["overfitting"], deployed=True, rolled_back=False),
        row("F99", ids["regressive"], gates=("pass", "pass", "fail", "not_run"), decision="REJECT"),
        row("F99", ids["slow"], deployed=True, rolled_back=True, recovery_s=5.0),
        row("F99", ids["ineffective"], gates=("pass", "fail", "not_run", "not_run"), decision="REJECT"),
        # superseded earlier row, and an invalid row that must be ignored
        row("F99", ids["correct"], decision="REJECT", ts="2026-10-19T09:00:00+00:00", run="r0"),
        row("F99", ids["regressive"], decision="AUTO_APPLY", ts="2026-10-19T11:00:00+00:00", run="r9",
            invalid="observer_suspended"),
        row("F99", "F99-live-1", ts="2026-10-19T10:00:00+00:00"),   # unlabelled
    ]
    ev = tmp_path / "evidence.jsonl"
    ev.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path, ev, ids


def test_wilson_matches_reference_values():
    lo, hi = analyse.wilson(0, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.0, 0.2775)
    lo, hi = analyse.wilson(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    assert analyse.wilson(0, 0) == (None, None)


def test_units_first_stops_and_policies(world):
    tmp, ev, ids = world
    files, report = analyse.analyse(ev, tmp / "faults")
    table = lambda name: list(csv.reader(io.StringIO(files[name])))[1:]  # noqa: E731
    stops = {r[2]: r[4] for r in table("unsafe_first_stop.csv")}
    assert stops == {"overfitting": "escaped", "regressive": "V2", "slow": "rollback", "ineffective": "V1"}
    pol = {(r[0], r[2]): r[3:5] for r in table("policies.csv")}
    assert pol[("B0", "unsafe_auto_applied")] == ["0", "4"] and pol[("B0", "escalations")] == ["5", "5"]
    assert pol[("B1", "unsafe_auto_applied")] == ["3", "4"]          # overfitting, regressive, slow
    assert pol[("B2", "unsafe_auto_applied")] == ["2", "4"]          # overfitting, slow
    assert pol[("B3", "unsafe_auto_applied")] == ["2", "4"] and pol[("B3", "safe_rejected")] == ["0", "1"]
    assert "staged," in files["rollbacks.csv"] and "real," not in files["rollbacks.csv"]
    assert "1 excluded as invalid" in report and "1 superseded" in report and "unlabelled 1" in report


def test_output_is_reproducible_byte_for_byte(world, capsys):
    tmp, ev, _ = world
    out = tmp / "results"
    analyse.main(ev, tmp / "faults", out)
    first = {p.name: p.read_bytes() for p in sorted(out.iterdir())}
    for p in out.iterdir():
        p.unlink()
    analyse.main(ev, tmp / "faults", out)
    assert {p.name: p.read_bytes() for p in sorted(out.iterdir())} == first
    assert set(first) == {"units.csv", "decisions_by_fault.csv", "unsafe_first_stop.csv", "policies.csv",
                          "rollbacks.csv", "approvals.csv", "pilot_report.md"}


def test_three_approval_sources_are_reported_separately(world):
    tmp, ev, ids = world
    extra = [row("F99", ids["correct"], decision="ESCALATE", run=f"a{i}", ts=f"2026-10-19T12:0{i}:00+00:00",
                 approval_source=src) for i, src in enumerate(["human_cli", "human_relayed", "human_relayed"])]
    ev.write_text("".join(json.dumps(r) + "\n" for r in analyse.load_rows(ev) + extra))
    files, report = analyse.analyse(ev, tmp / "faults")
    assert "**1 human_cli**" in report and "**2 human_relayed**" in report and "**0 scripted_test**" in report
    assert [r.split(",")[0] for r in files["approvals.csv"].splitlines()[1:]] == ["human_cli", "human_relayed",
                                                                                 "human_relayed"]


def test_scripted_approvals_are_not_counted_as_human(world):
    tmp, ev, ids = world
    rows = analyse.load_rows(ev) + [row("F99", ids["correct"], decision="ESCALATE", run="r2",
                                        ts="2026-10-19T12:00:00+00:00", approval_source="scripted_test")]
    ev.write_text("".join(json.dumps(r) + "\n" for r in rows))
    _, report = analyse.analyse(ev, tmp / "faults")
    assert "**0 human_cli**" in report and "**0 human_relayed**" in report and "**1 scripted_test**" in report
