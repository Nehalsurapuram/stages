import json
from pathlib import Path

import pytest
import requests

from pipeline import providers
from pipeline.providers import Diagnosis, Cause, ProviderError, get_provider

REPO = Path(__file__).resolve().parents[1]
SERVICE = "app/src/main/java/com/autoimmune/orders/OrderService.java"
CONTROLLER = "app/src/main/java/com/autoimmune/orders/OrderController.java"
ENV = {"LLM_BASE_URL": "http://llm.invalid/v1", "LLM_MODEL": "test-model", "LLM_API_KEY": "not-a-real-key"}

# Shape of the F01 container log (Spring Boot 3.5, logback), trimmed. Line 46 is customer.trim() after inject.diff.
NPE_TRACE = """\
2026-10-19T05:29:06.208Z ERROR 1 --- [order-service] [nio-8080-exec-2] o.a.c.c.C.[.[.[/].[dispatcherServlet]    : Servlet.service() for servlet [dispatcherServlet] in context with path [] threw exception [Request processing failed: java.lang.NullPointerException: Cannot invoke "String.trim()" because "customer" is null] with root cause

java.lang.NullPointerException: Cannot invoke "String.trim()" because "customer" is null
\tat com.autoimmune.orders.OrderService.searchByCustomer(OrderService.java:46) ~[!/:0.1.0]
\tat com.autoimmune.orders.OrderController.search(OrderController.java:39) ~[!/:0.1.0]
\tat java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke(DirectMethodHandleAccessor.java:103) ~[na:na]
\tat org.springframework.web.method.support.InvocableHandlerMethod.doInvoke(InvocableHandlerMethod.java:258) ~[spring-web-6.2.11.jar!/:6.2.11]
\tat org.apache.tomcat.util.threads.TaskThread$WrappingRunnable.run(TaskThread.java:63) ~[tomcat-embed-core-10.1.46.jar!/:na]
\tat java.base/java.lang.Thread.run(Thread.java:1583) ~[na:na]
"""


def sources():
    return {p: (REPO / p).read_text(encoding="utf-8") for p in (SERVICE, CONTROLLER)}


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network call attempted")

    monkeypatch.setattr(providers.requests, "post", fail)
    return calls


class FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def fake_llm(monkeypatch, replies):
    """Mock requests.post: returns replies in order (last one repeats). Returns the list of request bodies."""
    bodies = []

    def post(url, json=None, headers=None, timeout=None):
        bodies.append(json)
        return FakeResponse(replies[min(len(bodies), len(replies)) - 1])

    monkeypatch.setattr(providers.requests, "post", post)
    return bodies


# ---------------------------------------------------------------- rule mode

def test_rule_mode_uses_top_app_frame_of_stack_trace():
    diag = get_provider("rule").diagnose({"kind": "exception", "fault_id": "F01", "status": 500,
                                          "stack_trace": NPE_TRACE}, sources())
    assert diag.provider_mode == "rule"
    assert diag.model is None
    assert diag.oracle_localisation is False
    top = diag.causes[0]
    assert (top.rank, top.file, top.line) == (1, SERVICE, 46)
    assert "NullPointerException" in top.evidence and "searchByCustomer" in top.reason
    assert [(c.rank, c.file, c.line) for c in diag.causes[1:]] == [(2, CONTROLLER, 39)]
    assert all(not c.file.startswith(("java", "org")) for c in diag.causes)


def test_rule_mode_probe_incident_falls_back_to_fault_json():
    incident = {"kind": "probe_mismatch", "fault_id": "F06", "endpoint": "/api/orders/2/discount",
                "status": 200, "expected": "900.00", "actual": "1000.00"}
    diag = get_provider("rule").diagnose(incident, sources())
    assert diag.oracle_localisation is True
    assert diag.to_dict()["oracle_localisation"] is True
    assert len(diag.causes) == 1
    cause = diag.causes[0]
    assert (cause.rank, cause.file, cause.line) == (1, SERVICE, None)
    assert "oracle localisation" in cause.reason
    assert '"actual": "1000.00"' in cause.evidence


def test_rule_mode_health_down_uses_config_file():
    diag = get_provider("rule").diagnose({"kind": "health_down", "fault_id": "F04", "status": 503}, {})
    assert diag.oracle_localisation is True
    assert diag.causes[0].file == "app/src/main/resources/application.properties"


def test_rule_mode_without_trace_or_fault_fails_cleanly():
    with pytest.raises(ProviderError, match="no stack trace"):
        get_provider("rule").diagnose({"kind": "probe_mismatch"}, {})


# ---------------------------------------------------------------- scripted mode

def test_scripted_mode_loads_f06_candidates():
    diag = Diagnosis(causes=[Cause(1, SERVICE, None, "r", "e")], provider_mode="rule")
    patches = get_provider("scripted").draft_patches(diag, sources(), "F06", n=None)
    kinds = [p.candidate_kind for p in patches]
    assert kinds == ["correct", "insecure", "overfitting", "regressive", "slow"]
    for p in patches:
        assert p.provider_mode == "scripted" and p.model is None
        assert p.applies is True, p.apply_error
        assert p.diff == (REPO / "faults/F06/candidates" / f"{p.candidate_kind}.diff").read_text(encoding="utf-8")
        assert p.candidate_kind not in p.candidate_id      # ids stay opaque to the decision engine
    assert len({p.candidate_id for p in patches}) == 5
    assert len(get_provider("scripted").draft_patches(diag, sources(), "F06")) == 3


def test_scripted_mode_never_opens_labels_or_hidden_tests(monkeypatch):
    opened = []
    real_read_text = Path.read_text

    def spy(self, *args, **kwargs):
        opened.append(self.as_posix())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    diag = Diagnosis(causes=[Cause(1, SERVICE, None, "r", "e")], provider_mode="rule")
    get_provider("scripted").draft_patches(diag, {}, "F06", n=None)
    assert opened and not [p for p in opened if "labels.json" in p or "hidden_tests" in p]


def test_hidden_tests_are_refused_as_source_files():
    with pytest.raises(ProviderError, match="hidden tests"):
        get_provider("rule").diagnose({"kind": "health_down", "fault_id": "F04"},
                                      {"faults/F04/hidden_tests/F04HiddenTest.java": "class X {}"})


# ---------------------------------------------------------------- replay mode

def test_replay_mode_reads_cache_and_makes_no_network_call(tmp_path, no_network):
    prov = get_provider("replay", cache_dir=tmp_path, env=ENV)
    incident = {"kind": "exception", "fault_id": "F01", "stack_trace": NPE_TRACE}
    key = prov.cache_key(prov.diagnosis_messages(incident, sources()))
    answer = {"causes": [{"rank": 1, "file": SERVICE, "line": 46, "reason": "customer is not null-checked",
                          "evidence": "NullPointerException at OrderService.java:46"}]}
    (tmp_path / f"{key}.json").write_text(json.dumps({"key": key, "response": json.dumps(answer)}), encoding="utf-8")

    diag = prov.diagnose(incident, sources())

    assert no_network == []
    assert diag.provider_mode == "replay" and diag.model == "test-model"
    assert (diag.causes[0].file, diag.causes[0].line) == (SERVICE, 46)
    assert diag.attempts == [{"attempt": 1, "cache_key": key, "ok": True, "error": None}]


def test_replay_cache_miss_is_a_clear_error(tmp_path, no_network):
    prov = get_provider("replay", cache_dir=tmp_path, env=ENV)
    with pytest.raises(ProviderError, match="replay cache miss"):
        prov.diagnose({"kind": "exception", "stack_trace": NPE_TRACE}, sources())
    assert no_network == []


def test_cache_key_ignores_fault_id_and_changes_with_temperature(tmp_path):
    a = get_provider("replay", cache_dir=tmp_path, env=ENV)
    b = get_provider("replay", cache_dir=tmp_path, env=ENV, temperature=0.7)
    m1 = a.diagnosis_messages({"kind": "x", "fault_id": "F01"}, {})
    m2 = a.diagnosis_messages({"kind": "x", "fault_id": "F06"}, {})
    assert m1 == m2 and "F01" not in json.dumps(m1)
    assert a.cache_key(m1) != b.cache_key(m1)


def test_llm_modes_need_env_vars(tmp_path):
    with pytest.raises(ProviderError, match="LLM_MODEL"):
        get_provider("live", cache_dir=tmp_path, env={"LLM_BASE_URL": "http://x/v1"})


# ---------------------------------------------------------------- live mode (HTTP mocked)

def test_invalid_json_is_retried_then_fails_cleanly(tmp_path, monkeypatch):
    bodies = fake_llm(monkeypatch, ["this is not json"])
    prov = get_provider("live", cache_dir=tmp_path, env=ENV)
    with pytest.raises(ProviderError) as exc:
        prov.diagnose({"kind": "exception", "stack_trace": NPE_TRACE}, sources())
    assert len(bodies) == 3                                   # first attempt + 2 retries
    assert "no valid response after 3 attempts" in str(exc.value)
    assert len(list(tmp_path.glob("*.json"))) == 3            # every response saved
    assert "That answer was invalid" in bodies[1]["messages"][-1]["content"]


def test_too_many_causes_is_retried_then_accepted(tmp_path, monkeypatch):
    cause = {"rank": 1, "file": SERVICE, "line": 46, "reason": "r", "evidence": "e"}
    four = {"causes": [dict(cause, rank=i) for i in range(1, 5)]}
    ok = {"causes": [cause]}
    bodies = fake_llm(monkeypatch, [json.dumps(four), "```json\n" + json.dumps(ok) + "\n```"])
    diag = get_provider("live", cache_dir=tmp_path, env=ENV).diagnose({"kind": "exception"}, sources())
    assert len(bodies) == 2
    assert [a["ok"] for a in diag.attempts] == [False, True]
    assert "too long" in diag.attempts[0]["error"]
    assert diag.provider_mode == "live" and diag.causes[0].line == 46
    saved = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*.json")]
    assert {s["model"] for s in saved} == {"test-model"} and len(saved) == 2
    assert all("not-a-real-key" not in json.dumps(s) for s in saved)   # key never written to cache


def test_live_patches_are_apply_checked(tmp_path, monkeypatch):
    good = (REPO / "faults/F06/candidates/correct.diff").read_text(encoding="utf-8")
    bad = good.replace("compareTo(DISCOUNT_THRESHOLD) > 0", "compareTo(SOMETHING_ELSE) > 0")
    fake_llm(monkeypatch, [json.dumps({"patches": [{"diff": good, "explanation": "restore >="},
                                                   {"diff": bad, "explanation": "wrong context"}]})])
    diag = Diagnosis(causes=[Cause(1, SERVICE, 41, "r", "e")], provider_mode="live")
    patches = get_provider("live", cache_dir=tmp_path, env=ENV).draft_patches(diag, sources(), "F06", n=2)
    assert [p.candidate_id for p in patches] == ["F06-live-1", "F06-live-2"]
    assert patches[0].applies is True and patches[0].apply_error is None
    assert patches[1].applies is False and "patch does not apply" in patches[1].apply_error
    assert all(p.candidate_kind is None and p.attempts[-1]["ok"] for p in patches)
