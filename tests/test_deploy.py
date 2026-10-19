import ast
import json
import time
from pathlib import Path

import pytest

from pipeline import deploy
from pipeline.decide import Decision

CFG = {"host_port": 18080, "app_port": 8080, "latency_limit_ms": 2000, "health_window_s": 30, "check_interval_s": 3}
PROBES = [
    {"name": "order1-total", "path": "/api/orders/1/total", "expect_status": 200, "expect_body": "950.00"},
    {"name": "order2-discount-at-threshold", "path": "/api/orders/2/discount", "expect_status": 200,
     "expect_body": "900.00"},
    {"name": "search-alice", "path": "/api/orders/search?customer=alice", "expect_status": 200,
     "expect_body": '[{"id":1,"customer":"alice"},{"id":3,"customer":"alice"}]'},
]
HEALTHY = {"/actuator/health": (200, '{"status":"UP"}'), "/api/orders/1/total": (200, "950.00"),
           "/api/orders/2/discount": (200, "900.00"),
           "/api/orders/search?customer=alice": (200, '[{"id": 1, "customer": "alice"}, {"id": 3, "customer": "alice"}]')}


class Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text
        self.headers = {"Content-Type": "application/json" if text.startswith(("{", "[")) else "text/plain"}

    def json(self):
        return json.loads(self.text)


def fake_http(routes, delay=None):
    calls = []

    def get(url, timeout=None):
        path = url.split("18080", 1)[1]
        calls.append(path)
        if delay and path in delay:
            time.sleep(delay[path])
        return Resp(*routes[path])

    get.calls = calls
    return get


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.0)


def window(http, cfg=CFG):
    clock = FakeClock()
    return deploy.health_window(cfg, PROBES, http_get=http, clock=clock, sleep=clock.sleep)


# ------------------------------------------------------------------ health window

def test_window_passes_with_every_check_recorded():
    w = window(fake_http(HEALTHY))
    assert w["passed"] is True and w["breach"] is None
    assert len(w["checks"]) == 11 and [c["t_s"] for c in w["checks"]][:3] == [0.0, 3.0, 6.0]
    assert all(c["ok"] and len(c["probes"]) == 3 for c in w["checks"])


def test_probe_bodies_compare_as_exact_json_decimals():
    assert deploy._same_body("950.00", "950.0") and not deploy._same_body("900.00", "1000.00")
    assert deploy._same_body('[{"a": 1}]', '[{"a":1}]')


def test_wrong_value_is_a_breach():
    w = window(fake_http({**HEALTHY, "/api/orders/2/discount": (200, "1000.00")}))
    assert w["passed"] is False and len(w["checks"]) == 1
    assert w["breach"]["breaches"] == ["probe order2-discount-at-threshold returned http=200 body='1000.00', "
                                       "expected http=200 body='900.00'"]


def test_latency_over_limit_is_a_breach():
    w = window(fake_http(HEALTHY, delay={"/api/orders/2/discount": 0.08}), dict(CFG, latency_limit_ms=50))
    assert w["passed"] is False
    (msg,) = w["breach"]["breaches"]
    assert msg.startswith("probe order2-discount-at-threshold took ") and msg.endswith("ms, limit 50 ms")


def test_health_down_is_a_breach():
    w = window(fake_http({**HEALTHY, "/actuator/health": (503, '{"status":"DOWN"}')}))
    assert w["breach"]["breaches"] == ["/actuator/health not UP (http=503, status=DOWN)"]


# ------------------------------------------------------------------ approval gate and release

AUTO = Decision("low", "AUTO_APPLY", ("ok",), "v")
ESC = Decision("medium", "ESCALATE", ("diff touches a sensitive path",), "v")
REJ = Decision("high", "REJECT", ("V2 failed",), "v")


@pytest.mark.parametrize("decision, approval, allowed", [
    (AUTO, None, True), (ESC, None, False), (ESC, {"answer": "reject"}, False),
    (ESC, {"answer": "no_answer"}, False), (ESC, {"answer": "approve", "user": "u", "time": "t"}, True),
    (REJ, None, False), (REJ, {"answer": "approve"}, False),
])
def test_may_deploy(decision, approval, allowed):
    assert deploy.may_deploy(decision, approval)[0] is allowed


@pytest.mark.parametrize("decision", [ESC, REJ])
def test_release_never_deploys_without_permission(tmp_path, monkeypatch, decision):
    monkeypatch.setattr(deploy, "deploy", lambda *a, **k: pytest.fail("deploy must not be called"))
    res = deploy.release(decision, tmp_path, "r", run_dir=tmp_path, cfg=CFG)
    assert res["deployed"] is False and res["staged"] is (decision is ESC)
    assert not (tmp_path / "deploy.json").exists()


def _fake_swap(monkeypatch, window_result):
    events = []
    monkeypatch.setattr(deploy, "deploy", lambda src, run_id, cfg: {
        "run_id": run_id, "new_image": "order-service:new", "previous_image": "order-service:baseline",
        "build_s": 1.0, "swap_to_answer_s": 2.0, "answered": True, "swap_started_at": "t", "swap_clock": 0.0})
    monkeypatch.setattr(deploy, "health_window", lambda cfg: window_result)
    monkeypatch.setattr(deploy, "_stop_app", lambda: events.append("stop"))
    monkeypatch.setattr(deploy, "_run_app", lambda tag, cfg: events.append(f"run {tag}"))
    monkeypatch.setattr(deploy, "wait_until", lambda cfg, healthy, **k: events.append(f"wait healthy={healthy}") or 1.0)
    return events


def test_release_promotes_on_a_clean_window(tmp_path, monkeypatch):
    events = _fake_swap(monkeypatch, {"passed": True, "checks": [{"ok": True}], "breach": None, "breach_clock": None})
    res = deploy.release(AUTO, tmp_path, "r", run_dir=tmp_path, cfg=CFG)
    assert res["outcome"] == "promoted" and res["rolled_back"] is False and events == []
    rec = json.loads((tmp_path / "deploy.json").read_text(encoding="utf-8"))
    assert rec["outcome"] == "promoted" and rec["promoted_image"] == "order-service:new"
    assert not (tmp_path / "rollback.json").exists()


def test_release_rolls_back_to_previous_image_on_breach(tmp_path, monkeypatch):
    breach = {"ok": False, "breaches": ["probe x took 3021 ms, limit 2000 ms"]}
    events = _fake_swap(monkeypatch, {"passed": False, "checks": [breach], "breach": breach,
                                      "breach_clock": time.monotonic()})
    approval = {"answer": "approve", "user": "u", "time": "t"}
    res = deploy.release(ESC, tmp_path, "r", approval=approval, run_dir=tmp_path, cfg=CFG)
    assert events == ["stop", "run order-service:baseline", "wait healthy=True"]
    assert res["rolled_back"] is True and res["image"] == "order-service:baseline"
    rb = json.loads((tmp_path / "rollback.json").read_text(encoding="utf-8"))
    assert rb["reason"] == "probe x took 3021 ms, limit 2000 ms" and rb["failed_check"] == breach
    assert rb["restored_image"] == "order-service:baseline" and rb["rolled_back_image"] == "order-service:new"
    assert rb["seconds_breach_to_healthy"] >= 0
    assert json.loads((tmp_path / "deploy.json").read_text(encoding="utf-8"))["outcome"] == "rolled_back"


# ------------------------------------------------------------------ approve_cli

def _ask(answers):
    it = iter(answers)

    def input_fn(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return input_fn


@pytest.mark.parametrize("answers, expected", [(["approve"], "approve"), (["maybe", " Reject "], "reject"), ([], "no_answer")])
def test_approve_cli_records_answer_user_and_time(tmp_path, answers, expected):
    shown = []
    rec = deploy.approve_cli(ESC, "--- a/x\n+++ b/x\n", input_fn=_ask(answers), out=shown.append,
                             user="tester", run_dir=tmp_path)
    assert rec["answer"] == expected and rec["user"] == "tester" and rec["time"].endswith("+00:00")
    assert "--- a/x\n+++ b/x" in shown and any("diff touches a sensitive path" in s for s in shown)
    assert json.loads((tmp_path / "approval.json").read_text(encoding="utf-8")) == rec


def test_image_tags_are_valid_docker_tags():
    assert deploy.image_tag("Demo F06/Correct 2026-10-19T12:00:00Z") == "order-service:demo-f06-correct-2026-10-19t12-00-00z"


def test_deploy_module_never_runs_git():
    tree = ast.parse(Path(deploy.__file__).read_text(encoding="utf-8"))
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "git" not in strings and not any(s.startswith("git ") for s in strings)
