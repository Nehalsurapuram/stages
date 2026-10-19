import json
from pathlib import Path

import pytest

from pipeline import orchestrator
from pipeline.orchestrator import choose

E = lambda cid, decision, lines, **extra: {"candidate_id": cid, "decision": decision, "diff_lines": lines, **extra}  # noqa: E731


def ids(sel):
    return [e["candidate_id"] for e in sel.chosen]


def test_fewest_diff_lines_auto_apply_wins_ties_by_candidate_id():
    entries = [E("c3", "AUTO_APPLY", 7), E("c2", "AUTO_APPLY", 2), E("c1", "AUTO_APPLY", 2), E("c0", "REJECT", 1)]
    assert ids(choose(entries)) == ["c1"]
    assert ids(choose(entries, deploy_each=True)) == ["c1", "c2", "c3"]


def test_escalate_fallback_and_no_safe_candidate():
    entries = [E("a", "REJECT", 1), E("b", "ESCALATE", 5), E("c", "ESCALATE", 2)]
    sel = choose(entries)
    assert ids(sel) == ["c"] and "human review" in sel.note
    assert ids(choose(entries, deploy_each=True)) == ["c"]
    none = choose([E("a", "REJECT", 1)])
    assert none.chosen == [] and none.note.startswith("no safe candidate")


def test_override_picks_the_named_candidate_even_if_rejected():
    entries = [E("a", "AUTO_APPLY", 1), E("b", "REJECT", 3)]
    assert ids(choose(entries, override="b")) == ["b"]      # deploy.release still refuses to deploy it
    with pytest.raises(ValueError, match="not one of"):
        choose(entries, override="zzz")


def test_choice_ignores_candidate_kind_and_labels():
    base = [E("a", "AUTO_APPLY", 5), E("b", "AUTO_APPLY", 2), E("c", "ESCALATE", 1)]
    for kinds in (("correct", "overfitting", "slow"), ("insecure", "correct", "regressive"), (None, None, None)):
        tagged = [dict(e, candidate_kind=k, label=k) for e, k in zip(base, kinds)]
        assert choose(tagged).chosen == choose(base).chosen
        assert choose(tagged, deploy_each=True).chosen == choose(base, deploy_each=True).chosen


# ------------------------------------------------------------- full run, Docker and sandbox faked

GATE_PLAN = {  # what the fake sandbox reports per scripted file; the orchestrator never sees this mapping
    "correct": ("pass", "pass", "pass", "pass", []),
    "overfitting": ("pass", "pass", "pass", "pass", []),
    "slow": ("pass", "pass", "pass", "pass", []),
    "regressive": ("pass", "pass", "fail", "not_run", None),
    "insecure": ("pass", "pass", "pass", "fail", [{"rule_id": "sql-string-concatenation", "file": "x", "line": 1,
                                                   "severity": "ERROR"}]),
}


@pytest.fixture
def faked(tmp_path, monkeypatch):
    from pipeline import deploy, detector, record, sandbox
    calls, rows = [], []
    state = {"image": "order-service:baseline"}
    monkeypatch.setattr(orchestrator, "REPO", tmp_path)
    monkeypatch.setattr(orchestrator, "keep_awake", lambda: None)
    monkeypatch.setattr(deploy, "current_image", lambda: state["image"])
    monkeypatch.setattr(deploy, "_health", lambda cfg: {"ok": state["image"] == "order-service:baseline"})

    def fake_deploy(src, run_id, cfg):
        calls.append(("deploy_faulty", run_id))
        state["image"] = "order-service:faulty"
        return {"new_image": "order-service:faulty", "previous_image": "order-service:baseline",
                "swap_to_answer_s": 1.0, "answered": True, "swap_clock": 0.0}

    def fake_switch(tag, cfg, healthy=True):
        calls.append(("switch", tag))
        state["image"] = tag
        return 1.0

    def fake_release(decision, src, run_id, approval=None, run_dir=None, cfg=None):
        calls.append(("release", run_id))
        slow = "slow" in (Path(src) / "marker").read_text()
        state["image"] = "order-service:faulty" if slow else f"order-service:{run_id}"
        return ({"deployed": True, "staged": False, "rolled_back": True, "outcome": "rolled_back",
                 "reason": "probe took 3015 ms", "recovery_s": 5.0} if slow else
                {"deployed": True, "staged": False, "rolled_back": False, "outcome": "promoted", "recovery_s": None})

    def fake_prepare(fault, diff, dest, repo=None):
        app = Path(dest) / "app"
        (app / "src/main/java").mkdir(parents=True)
        (app / "src/main/java/X.java").write_text("class X {}")
        (app / "marker").write_text("slow" if diff and "Thread.sleep" in diff else "other")
        return app

    def fake_gates(fault, patch, run_id):
        v0, v1, v2, v3, findings = GATE_PLAN[patch.candidate_kind]
        g = {n: {"status": s, "seconds": 1.0} for n, s in zip(("V0", "V1", "V2", "V3"), (v0, v1, v2, v3))}
        g["V3"]["new_findings"] = findings
        return {"candidate_id": patch.candidate_id, "gates": g}

    monkeypatch.setattr(deploy, "deploy", fake_deploy)
    monkeypatch.setattr(deploy, "switch_to", fake_switch)
    monkeypatch.setattr(deploy, "release", fake_release)
    monkeypatch.setattr(sandbox, "prepare_patched_app", fake_prepare)
    monkeypatch.setattr(sandbox, "run_gates", fake_gates)
    monkeypatch.setattr(detector, "watch", lambda *a, **k: {
        "kind": "probe_mismatch", "rule": "probe_mismatch", "fault_id": "F06", "endpoint": "/api/orders/2/discount",
        "status": 200, "expected": "900.00", "actual": "1000.00"})
    monkeypatch.setattr(record, "append_row", lambda row, path=None: rows.append((row, path)))
    return calls, rows, tmp_path


def test_full_run_deploy_each_restores_faulty_build_between_candidates(faked, capsys):
    calls, rows, tmp = faked
    s = orchestrator.run("F06", "rule", "scripted", deploy_each=True, run_id="t1")
    out = capsys.readouterr().out
    assert [f"[{n}/10]" in out for n in range(2, 11)] == [True] * 9
    releases = [c for c in calls if c[0] == "release"]
    assert len(releases) == 3                                  # the three AUTO_APPLY candidates
    switches = [c for c in calls if c[0] == "switch"]
    assert ("switch", "order-service:faulty") in switches       # faulty build restored between candidates
    assert switches[-1] == ("switch", "order-service:baseline")  # step 9
    for name in ("run.json", "baseline.json", "faulty_deploy.json", "diagnosis.json", "patches.json",
                 "choice.json", "summary.json"):
        assert (tmp / "runs" / "t1" / name).exists(), name
    by_decision = {r["decision"]: r for r in s["candidates"]}
    assert sorted(r["decision"] for r in s["candidates"]) == ["AUTO_APPLY"] * 3 + ["REJECT"] * 2
    assert {r["result"] for r in s["candidates"] if r["decision"] == "REJECT"} == {"not deployed (REJECT)"}
    assert any(r["result"].startswith("rolled back in 5.0s") for r in s["candidates"])
    assert by_decision["AUTO_APPLY"]["deployed"] is True
    assert len(rows) == 10                                      # 5 rows, each to results/ and to runs/<id>/
    deployed = sorted(r["deployed"] for r, p in rows if p is None)
    assert deployed == [False, False, True, True, True]


def test_failing_step_stops_with_the_real_error_and_restores_baseline(faked, monkeypatch):
    calls, rows, tmp = faked
    from pipeline import detector
    monkeypatch.setattr(detector, "watch", lambda *a, **k: None)
    with pytest.raises(orchestrator.StepError, match=r"step 3 .* no incident within 60s"):
        orchestrator.run("F06", "rule", "scripted", run_id="t2")
    err = json.loads((tmp / "runs" / "t2" / "error.json").read_text(encoding="utf-8"))
    assert err["step"] == 3 and "no incident" in err["error"]
    assert calls[-1] == ("switch", "order-service:baseline") and rows == []
