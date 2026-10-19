import json
import math

import pytest

from pipeline import detector, observe
from pipeline.providers import get_provider

CFG = {"host_port": 18080, "latency_limit_ms": 2000, "poll_interval_s": 5, "consecutive_failures": 2,
       "metrics": {"window_s": 15, "min_requests": 20, "max_5xx_share": 0.05, "max_p95_ms": 2000}}
LES = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, math.inf]
NPE_JSON = json.dumps({
    "@timestamp": "2026-10-19T16:40:01.123Z", "@version": "1", "level": "ERROR", "level_value": 40000,
    "logger_name": "org.apache.catalina.core.ContainerBase.[Tomcat].[localhost].[/].[dispatcherServlet]",
    "thread_name": "http-nio-8080-exec-3",
    "message": "Servlet.service() for servlet [dispatcherServlet] threw exception [Request processing failed: "
               "java.lang.NullPointerException] with root cause",
    "stack_trace": "java.lang.NullPointerException: Cannot invoke \"String.trim()\" because \"customer\" is null\n"
                   "\tat com.autoimmune.orders.OrderService.searchByCustomer(OrderService.java:46)\n"
                   "\tat com.autoimmune.orders.OrderController.search(OrderController.java:39)\n"
                   "\tat org.apache.catalina.core.ApplicationFilterChain.doFilter(ApplicationFilterChain.java:140)\n"})
LOGS = "\n".join([
    "  .   ____          _            __ _ _",                                 # banner: not JSON
    json.dumps({"@timestamp": "2026-10-19T16:40:00Z", "level": "INFO", "message": "Started"}),
    NPE_JSON,
    json.dumps({"@timestamp": "2026-10-19T16:40:02Z", "level": "ERROR", "message": "no trace here"}),
    json.dumps({"@timestamp": "2026-10-19T16:40:03Z", "level": "INFO", "message": "later"}),
])


class FakeService:
    """Serves /actuator/health and a cumulative /actuator/prometheus; each scrape adds traffic(poll) requests."""

    def __init__(self, clock, traffic):
        self.clock, self.traffic, self.polls = clock, traffic, 0
        self.series: dict[tuple[str, str], dict] = {}

    def _add(self, uri, status, latency_s, n):
        s = self.series.setdefault((uri, status), {"count": 0, "b": {le: 0 for le in LES}})
        s["count"] += n
        for le in LES:
            if latency_s <= le:
                s["b"][le] += n

    def text(self):
        out = ["# HELP http_server_requests_seconds", "# TYPE http_server_requests_seconds histogram"]
        for (uri, status), s in sorted(self.series.items()):
            lab = f'error="none",exception="none",method="GET",outcome="X",status="{status}",uri="{uri}"'
            for le in LES:
                out.append(f'http_server_requests_seconds_bucket{{{lab},le="{"+Inf" if math.isinf(le) else le}"}} '
                           f'{float(s["b"][le])}')
            out.append(f"http_server_requests_seconds_count{{{lab}}} {float(s['count'])}")
            out.append(f"http_server_requests_seconds_sum{{{lab}}} 1.0")
        out.append('http_server_requests_active_seconds_count{method="GET",uri="UNKNOWN"} 1.0')
        return "\n".join(out) + "\n"

    def __call__(self, url, timeout=None):
        path = url.split("18080", 1)[1]
        if path == "/actuator/health":
            return Resp(200, '{"status":"UP"}')
        if path == "/actuator/prometheus":
            self.polls += 1
            action = self.traffic(self.polls)
            if action == "restart":
                self.series = {}
            else:
                for uri, status, latency_s, n in action:
                    self._add(uri, status, latency_s, n)
            self._add("/actuator/health", "200", 0.002, 1)        # observer traffic, must be excluded
            return Resp(200, self.text())
        raise AssertionError(path)


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


HEALTHY_MIX = [("/api/orders", "200", 0.004, 30), ("/api/orders/{id}/total", "200", 0.003, 30),
               ("/api/orders/{id}/discount", "200", 0.003, 30), ("/api/orders/search", "400", 0.002, 10)]


def run(traffic, tmp_path, timeout_s=60, logs=LOGS):
    clock = Clock()
    svc = FakeService(clock, traffic)
    seen = []
    inc = detector.watch(timeout_s, run_id="m", cfg=CFG, probes=[], http_get=svc, clock=clock, sleep=clock.sleep,
                         logs_fn=lambda: logs, runs_dir=tmp_path, on_check=lambda o, f: seen.append((o, f)))
    return inc, seen


def test_snapshot_parses_counts_and_buckets_and_drops_actuator():
    svc = FakeService(Clock(), lambda p: [("/api/orders", "200", 0.004, 7), ("/api/orders/search", "500", 0.02, 3)])
    snap = observe.request_snapshot(svc(f"http://localhost:18080/actuator/prometheus").text)
    assert snap["counts"] == {("/api/orders", "200"): 7.0, ("/api/orders/search", "500"): 3.0}
    assert snap["buckets"][0.005] == 7.0 and snap["buckets"][0.05] == 10.0 and snap["buckets"][math.inf] == 10.0


def test_histogram_quantile_interpolates_like_prometheus():
    b = {0.1: 50.0, 0.5: 90.0, 1.0: 100.0, math.inf: 100.0}
    assert detector.histogram_quantile(0.95, b) == pytest.approx(0.75)
    assert detector.histogram_quantile(0.95, {0.1: 0.0, math.inf: 0.0}) is None
    assert detector.histogram_quantile(0.5, {1.0: 0.0, math.inf: 10.0}) == 1.0     # all beyond last bound


def test_5xx_share_rule_raises_an_incident_with_the_json_stack_trace(tmp_path):
    inc, seen = run(lambda p: HEALTHY_MIX + [("/api/orders/search", "500", 0.01, 12)], tmp_path)
    assert (inc["rule"], inc["kind"], inc["endpoint"], inc["status"]) == \
        ("metrics_5xx_share", "exception", "/api/orders/search", 500)
    # 112 requests per poll; the 15 s window holds 3 polls of traffic: 36 x 500 of 336 = 10.7%
    assert inc["expected"] == "5xx share <= 5.0%" and inc["actual"] == "10.7%"
    assert inc["evidence"][0].startswith("5xx share 10.7% (36/336) over the last 15.0s")
    assert inc["consecutive_bad_checks"] == 2 and inc["failing_check"]["t_s"] == 20.0
    assert [o.get("metrics_window") is not None for o, _ in seen] == [False, False, False, True, True]
    assert inc["stack_trace_source"] == "docker logs autoimmune-app (JSON stack_trace)"
    assert inc["stack_trace"].startswith("2026-10-19T16:40:01.123Z ERROR org.apache.catalina")
    diag = get_provider("rule").diagnose(inc, {})
    assert (diag.causes[0].file, diag.causes[0].line, diag.oracle_localisation) == \
        ("app/src/main/java/com/autoimmune/orders/OrderService.java", 46, False)
    json.loads((tmp_path / "m" / "incident.json").read_text(encoding="utf-8"))      # serialisable


def test_p95_rule_raises_a_latency_incident(tmp_path):
    inc, _ = run(lambda p: HEALTHY_MIX + [("/api/orders/{id}/discount", "200", 3.0, 12)], tmp_path)
    assert (inc["rule"], inc["kind"]) == ("metrics_p95", "latency")
    assert inc["evidence"][0].startswith("p95 latency ") and inc["evidence"][0].endswith("limit 2000 ms")
    assert float(inc["actual"].split()[0]) > 2000


def test_low_traffic_windows_are_not_judged(tmp_path):
    inc, seen = run(lambda p: [("/api/orders/search", "500", 0.01, 5)], tmp_path)    # 100% 5xx, 15/window
    assert inc is None
    assert all(o["metrics_window"]["requests"] < 20 for o, _ in seen if o.get("metrics_window"))


def test_counter_reset_skips_the_window_instead_of_firing(tmp_path):
    traffic = lambda p: "restart" if p == 5 else HEALTHY_MIX                          # noqa: E731
    inc, seen = run(traffic, tmp_path, timeout_s=60)
    assert inc is None
    windows = [o.get("metrics_window", "absent") for o, _ in seen]
    assert None in windows                                    # the reset window was skipped, not judged


def test_healthy_load_raises_nothing_for_five_minutes(tmp_path):
    inc, seen = run(lambda p: HEALTHY_MIX, tmp_path, timeout_s=300)
    assert inc is None and len(seen) == 60
    judged = [o["metrics_window"] for o, _ in seen if o.get("metrics_window")]
    assert judged and all(w["share_5xx"] == 0 and w["p95_ms"] < 10 for w in judged)


def test_metrics_endpoint_missing_is_not_an_incident(tmp_path):
    clock = Clock()

    def no_prometheus(url, timeout=None):
        return Resp(200, '{"status":"UP"}') if url.endswith("/health") else Resp(404, "Not Found")

    inc = detector.watch(30, run_id="m", cfg=CFG, probes=[], http_get=no_prometheus, clock=clock,
                         sleep=clock.sleep, logs_fn=lambda: "", runs_dir=tmp_path)
    assert inc is None


def test_json_stack_trace_falls_back_to_plain_text_logs():
    assert detector.last_error_from_json("plain line\n2026-10-19T12:00:05.208Z ERROR 1 --- x\n") is None
    assert detector.last_error_from_json(LOGS).splitlines()[1].startswith("java.lang.NullPointerException")
    obs = {"probes": []}
    trace, source = detector._stack(obs, lambda: "2026-10-19T12:00:05.208Z ERROR 1 --- boom\n\tat a.B.c(B.java:1)\n")
    assert source == "docker logs autoimmune-app (plain text)" and "boom" in trace
