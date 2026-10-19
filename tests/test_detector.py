import json
import time

import pytest

from pipeline import detector, observe
from pipeline.providers import get_provider

CFG = {"host_port": 18080, "latency_limit_ms": 2000, "poll_interval_s": 5, "consecutive_failures": 2}
PROBES = [
    {"name": "order1-total", "path": "/api/orders/1/total", "expect_status": 200, "expect_body": "950.00"},
    {"name": "order2-discount-at-threshold", "path": "/api/orders/2/discount", "expect_status": 200,
     "expect_body": "900.00"},
    {"name": "search-alice", "path": "/api/orders/search?customer=alice", "expect_status": 200,
     "expect_body": '[{"id":1},{"id":3}]'},
    {"name": "search-no-customer", "path": "/api/orders/search", "expect_status": 400},
]
HEALTHY = {
    "/actuator/health": (200, '{"status":"UP"}'),
    "/api/orders/1/total": (200, "950.00"),
    "/api/orders/2/discount": (200, "900.00"),
    "/api/orders/search?customer=alice": (200, '[{"id": 1}, {"id": 3}]'),
    "/api/orders/search": (400, '{"timestamp":"2026-10-19T12:00:00.000+00:00","status":400,"error":"Bad Request"}'),
}
NPE_LOGS = """\
2026-10-19T12:00:00.000Z  INFO 1 --- [order-service] [main] c.a.orders.OrderServiceApplication : Started
2026-10-19T12:00:05.208Z ERROR 1 --- [order-service] [nio-8080-exec-2] o.a.c.c.C.[.[.[/].[dispatcherServlet]    : Servlet.service() for servlet [dispatcherServlet] threw exception [Request processing failed: java.lang.NullPointerException: Cannot invoke "String.trim()" because "customer" is null] with root cause

java.lang.NullPointerException: Cannot invoke "String.trim()" because "customer" is null
\tat com.autoimmune.orders.OrderService.searchByCustomer(OrderService.java:46) ~[!/:0.1.0]
\tat com.autoimmune.orders.OrderController.search(OrderController.java:39) ~[!/:0.1.0]
\tat org.apache.catalina.core.ApplicationFilterChain.doFilter(ApplicationFilterChain.java:140) ~[tomcat-embed-core.jar!/:na]
2026-10-19T12:00:06.000Z  INFO 1 --- [order-service] [nio-8080-exec-3] c.a.orders.Other : later line
"""


class Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.0)


CLOCK = None


def http(routes, delay=None, timeline=None, stall=None):
    """Fake requests.get. timeline: route overrides per poll (by health-call count). delay: {path: s} on the
    fake clock. stall: (poll number, seconds) the observer is frozen during that poll's health request."""
    state = {"polls": 0}

    def get(url, timeout=None):
        path = url.split("18080", 1)[1]
        if path == "/actuator/health":
            state["polls"] += 1
            if stall and state["polls"] == stall[0]:
                CLOCK.t += stall[1]
        table = dict(routes)
        if timeline:
            table.update(timeline[min(state["polls"], len(timeline)) - 1])
        if delay and path in delay:
            CLOCK.t += delay[path]
        return Resp(*table[path])

    return get


def watch(get, tmp_path, cfg=CFG, timeout_s=30, logs="", sleep=None, **kw):
    clock = CLOCK
    return detector.watch(timeout_s, run_id="t", cfg=cfg, probes=PROBES, http_get=get, clock=clock,
                          sleep=sleep or clock.sleep, logs_fn=lambda: logs, runs_dir=tmp_path, **kw)


@pytest.fixture(autouse=True)
def fresh_clock():
    global CLOCK
    CLOCK = Clock()
    yield CLOCK


def test_healthy_service_raises_nothing(tmp_path):
    assert watch(http(HEALTHY), tmp_path) is None
    assert not (tmp_path / "t" / "incident.json").exists()


def test_observe_records_status_value_latency_and_error_body():
    obs = observe.check(CFG, PROBES, http_get=http(HEALTHY))
    assert obs["health"]["status"] == "UP" and obs["health"]["http"] == 200
    nc = obs["probes"][3]
    assert nc["http"] == 400 and nc["value"].startswith('{"timestamp"') and "error_body" in nc
    assert all(isinstance(p["ms"], int) for p in obs["probes"])


def test_probe_mismatch_incident(tmp_path):
    inc = watch(http({**HEALTHY, "/api/orders/2/discount": (200, "1000.00")}), tmp_path, fault_id="F06")
    assert (inc["rule"], inc["kind"], inc["endpoint"]) == ("probe_mismatch", "probe_mismatch", "/api/orders/2/discount")
    assert (inc["expected"], inc["actual"], inc["consecutive_bad_checks"]) == ("900.00", "1000.00", 2)
    assert inc["evidence"] == ["/api/orders/2/discount returned http=200 value='1000.00', expected http=200 value='900.00'"]
    assert inc["fault_id"] == "F06"
    assert json.loads((tmp_path / "t" / "incident.json").read_text(encoding="utf-8")) == inc


def test_status_only_probe_ignores_body_but_not_status(tmp_path):
    other_body = {**HEALTHY, "/api/orders/search": (400, '{"timestamp":"different"}')}
    assert watch(http(other_body), tmp_path) is None
    inc = watch(http({**HEALTHY, "/api/orders/search": (200, "[]")}), tmp_path)
    assert inc["rule"] == "probe_mismatch" and inc["endpoint"] == "/api/orders/search"


def test_probe_5xx_incident_takes_stack_trace_from_docker_logs(tmp_path):
    routes = {**HEALTHY, "/api/orders/search": (500, '{"status":500,"error":"Internal Server Error"}')}
    inc = watch(http(routes), tmp_path, logs=NPE_LOGS, fault_id="F01")
    assert (inc["rule"], inc["kind"], inc["status"]) == ("probe_5xx", "exception", 500)
    assert inc["stack_trace_source"] == "docker logs autoimmune-app (plain text)"
    assert inc["stack_trace"].splitlines()[0].startswith("2026-10-19T12:00:05.208Z ERROR")
    assert "OrderService.searchByCustomer(OrderService.java:46)" in inc["stack_trace"]
    assert "later line" not in inc["stack_trace"]
    diag = get_provider("rule").diagnose(inc, {})           # the incident is directly usable by rule mode
    assert (diag.causes[0].line, diag.oracle_localisation) == (46, False)


def test_stack_trace_from_response_body_is_preferred(tmp_path):
    routes = {**HEALTHY, "/api/orders/search": (500, json.dumps({"status": 500, "trace": "java.lang.X\n\tat a.B.c(B.java:1)"}))}
    inc = watch(http(routes), tmp_path, logs=NPE_LOGS)
    assert inc["stack_trace_source"] == "response body" and inc["stack_trace"].startswith("java.lang.X")


def test_health_down_incident(tmp_path):
    inc = watch(http({**HEALTHY, "/actuator/health": (503, '{"status":"DOWN"}')}), tmp_path, fault_id="F04")
    assert (inc["rule"], inc["kind"], inc["endpoint"], inc["actual"]) == ("health_down", "health_down",
                                                                          "/actuator/health", "DOWN")
    assert inc["evidence"][0].startswith("/actuator/health http=503 status=DOWN")
    assert get_provider("rule").diagnose(inc, {}).oracle_localisation is True


def test_latency_incident(tmp_path):
    inc = watch(http(HEALTHY, delay={"/api/orders/2/discount": 3.015}), tmp_path)
    assert (inc["rule"], inc["kind"]) == ("probe_latency", "latency")
    assert inc["evidence"] == ["/api/orders/2/discount took 3015 ms, limit 2000 ms"]


def test_oversleep_between_polls_is_invalid_not_quiet(tmp_path, fresh_clock):
    naps = iter([0, 0, 300])

    def sleep(s):
        fresh_clock.t += max(s, 0.0) + next(naps, 0)

    res = watch(http(HEALTHY), tmp_path, timeout_s=600, sleep=sleep)
    assert res["status"] == "invalid" and res["invalid"] == "observer_suspended"
    assert res["sign"] == "gap between checks" and res["gap_s"] > 15
    assert json.loads((tmp_path / "t" / "detector.json").read_text(encoding="utf-8")) == res
    assert not (tmp_path / "t" / "incident.json").exists()


def test_freeze_during_a_bad_streak_is_invalid_not_an_incident(tmp_path):
    # The first poll is genuinely bad; the second is frozen 264 s mid-request. No incident from that.
    routes = {**HEALTHY, "/api/orders/2/discount": (200, "1000.00")}
    res = watch(http(routes, stall=(2, 264.0)), tmp_path)
    assert res["status"] == "invalid" and res["sign"] == "request outlasted its timeout"
    assert res["bad_checks_before"] == 1
    assert not (tmp_path / "t" / "incident.json").exists()


def test_unreachable_service_counts_as_5xx_and_health_down(tmp_path):
    import requests

    def down(url, timeout=None):
        raise requests.ConnectionError("refused")

    inc = watch(down, tmp_path)
    assert inc["rule"] == "health_down" and "probe_5xx" in inc["rules_fired"]


def test_single_bad_check_is_not_an_incident(tmp_path):
    flaky = [{"/api/orders/2/discount": (200, "1000.00")}, {}, {"/api/orders/2/discount": (200, "1000.00")}, {}]
    assert watch(http(HEALTHY, timeline=flaky), tmp_path, timeout_s=20) is None


def test_highest_priority_rule_is_reported(tmp_path):
    routes = {**HEALTHY, "/actuator/health": (503, '{"status":"DOWN"}'), "/api/orders/2/discount": (200, "1")}
    inc = watch(http(routes), tmp_path)
    assert inc["rules_fired"] == ["health_down", "probe_mismatch"] and inc["rule"] == "health_down"


@pytest.mark.parametrize("logs, first", [(NPE_LOGS, "2026-10-19T12:00:05.208Z ERROR"), ("no errors here\n", None)])
def test_last_error_block(logs, first):
    block = detector.last_error_block(logs)
    assert (block.splitlines()[0][:30] if block else None) == (first[:30] if first else None)
