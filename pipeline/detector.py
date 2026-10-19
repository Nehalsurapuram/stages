"""Detect an incident from repeated observations.

    watch(timeout_s) -> incident dict (also written to runs/<run_id>/incident.json), or None if quiet

Polls observe.check() every poll_interval_s. A check is bad if any rule fires:
  health_down        /actuator/health is not 200 + UP
  probe_5xx          a probe answered 5xx (or did not answer at all)
  probe_mismatch     a probe's status, or its value when expect_body is set, differs from config/probes.yaml
  probe_latency      a probe took latency_limit_ms or longer
  metrics_5xx_share  over the last metrics.window_s of /actuator/prometheus (http.server.requests, actuator
                     traffic excluded), the share of 5xx responses is above metrics.max_5xx_share
  metrics_p95        over the same window, p95 latency (from the histogram buckets) is above metrics.max_p95_ms
The metrics rules only judge windows with at least metrics.min_requests requests, and skip a window whose
counters went backwards (app restarted). An incident is raised after consecutive_failures bad checks in a row.
Its stack trace comes from the response body if the app returned one, else from the last ERROR event with a
stack_trace in the JSON logs of `docker logs autoimmune-app`, else from the last plain-text ERROR block.
The incident "kind" uses the vocabulary of providers.py (exception, probe_mismatch, health_down, latency).
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline import observe

REPO = Path(__file__).resolve().parents[1]
APP_CONTAINER = "autoimmune-app"
RULE_PRIORITY = ["health_down", "probe_5xx", "metrics_5xx_share", "probe_mismatch", "probe_latency", "metrics_p95"]
KIND = {"health_down": "health_down", "probe_5xx": "exception", "metrics_5xx_share": "exception",
        "probe_mismatch": "probe_mismatch", "probe_latency": "latency", "metrics_p95": "latency"}
METRICS_TARGET = "http.server.requests"
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def evaluate(obs: dict, cfg: dict) -> list[dict]:
    """Rules fired by one observation's health and probes: [{"rule", "target", "evidence"}]."""
    fired = []
    h = obs["health"]
    if not (h["http"] == 200 and h.get("status") == "UP"):
        fired.append({"rule": "health_down", "target": "/actuator/health",
                      "evidence": f"/actuator/health http={h['http']} status={h.get('status')} "
                                  f"{h.get('error') or h.get('error_body') or ''}".strip()})
    limit = cfg["latency_limit_ms"]
    for p in obs["probes"]:
        if p["http"] is None or p["http"] >= 500:
            fired.append({"rule": "probe_5xx", "target": p["path"],
                          "evidence": f"{p['path']} http={p['http']} {p.get('error_body') or p.get('error') or ''}".strip()})
        elif p["http"] != p["expect_status"] or (
                p.get("expect_body") is not None and not observe.same_value(p["expect_body"], p["value"])):
            fired.append({"rule": "probe_mismatch", "target": p["path"],
                          "evidence": f"{p['path']} returned http={p['http']} value={p['value']!r}, expected "
                                      f"http={p['expect_status']}"
                                      + (f" value={p['expect_body']!r}" if p.get("expect_body") is not None else "")})
        if p["ms"] >= limit:
            fired.append({"rule": "probe_latency", "target": p["path"],
                          "evidence": f"{p['path']} took {p['ms']} ms, limit {limit} ms"})
    return fired


# --------------------------------------------------------------------------- metrics window

def histogram_quantile(q: float, buckets: dict[float, float]) -> float | None:
    """Prometheus-style quantile from cumulative buckets {upper bound: count}, linear inside the bucket."""
    bounds = sorted(buckets)
    if not bounds or buckets[bounds[-1]] <= 0:
        return None
    rank = q * buckets[bounds[-1]]
    prev_bound, prev_count = 0.0, 0.0
    for b in bounds:
        c = buckets[b]
        if c >= rank:
            if math.isinf(b):
                return prev_bound                     # beyond the last finite bucket: report its bound
            if c == prev_count:
                return b
            return prev_bound + (b - prev_bound) * (rank - prev_count) / (c - prev_count)
        prev_bound, prev_count = b, c
    return prev_bound


def window_stats(old: dict, new: dict, seconds: float) -> dict | None:
    """Request deltas between two snapshots; None if any counter went backwards (restart)."""
    d_counts = {k: new["counts"].get(k, 0.0) - old["counts"].get(k, 0.0)
                for k in set(new["counts"]) | set(old["counts"])}
    d_buckets = {le: new["buckets"].get(le, 0.0) - old["buckets"].get(le, 0.0)
                 for le in set(new["buckets"]) | set(old["buckets"])}
    if any(v < 0 for v in d_counts.values()) or any(v < 0 for v in d_buckets.values()):
        return None
    total = sum(d_counts.values())
    errors = sum(v for (uri, status), v in d_counts.items() if status.startswith("5"))
    p95 = histogram_quantile(0.95, d_buckets)
    top = sorted(((v, uri, status) for (uri, status), v in d_counts.items() if status.startswith("5") and v > 0),
                 reverse=True)[:3]
    return {"window_s": round(seconds, 1), "requests": int(total), "errors_5xx": int(errors),
            "share_5xx": (errors / total) if total else None,
            "p95_ms": round(p95 * 1000, 1) if p95 is not None else None,
            "top_5xx": [{"uri": uri, "status": status, "count": int(v)} for v, uri, status in top]}


def evaluate_metrics(stats: dict | None, cfg: dict) -> list[dict]:
    m = cfg["metrics"]
    if stats is None or stats["requests"] < m["min_requests"]:
        return []
    fired = []
    where = f"over the last {stats['window_s']}s ({stats['requests']} requests, actuator excluded)"
    if stats["share_5xx"] is not None and stats["share_5xx"] > m["max_5xx_share"]:
        top = ", ".join(f"{t['uri']} {t['status']} x{t['count']}" for t in stats["top_5xx"])
        fired.append({"rule": "metrics_5xx_share", "target": METRICS_TARGET,
                      "evidence": f"5xx share {100 * stats['share_5xx']:.1f}% ({stats['errors_5xx']}/{stats['requests']}) "
                                  f"{where}, limit {100 * m['max_5xx_share']:.1f}%; top: {top}"})
    if stats["p95_ms"] is not None and stats["p95_ms"] > m["max_p95_ms"]:
        fired.append({"rule": "metrics_p95", "target": METRICS_TARGET,
                      "evidence": f"p95 latency {stats['p95_ms']:.0f} ms {where}, limit {m['max_p95_ms']} ms"})
    return fired


# --------------------------------------------------------------------------- stack traces

def last_error_from_json(logs: str) -> str | None:
    """Last JSON log event (logstash encoder) at ERROR with a stack_trace, as 'header' + stack trace text."""
    for line in reversed(logs.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("level") == "ERROR" and ev.get("stack_trace"):
            return f"{ev.get('@timestamp')} ERROR {ev.get('logger_name')}: {ev.get('message')}\n{ev['stack_trace']}"
    return None


def last_error_block(logs: str) -> str | None:
    """The last plain-text log record containing ERROR plus its stack trace lines (Spring Boot default layout)."""
    lines = logs.splitlines()
    starts = [i for i, l in enumerate(lines) if LOG_LINE.match(l) and " ERROR " in l]
    if not starts:
        return None
    i = starts[-1]
    block = [lines[i]]
    for l in lines[i + 1:]:
        if LOG_LINE.match(l):
            break
        block.append(l)
    return "\n".join(block).strip()


def docker_logs(container: str = APP_CONTAINER, tail: int = 400) -> str:
    r = subprocess.run(["docker", "logs", "--tail", str(tail), container], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout + r.stderr


def _stack_from_body(obs: dict) -> str | None:
    for p in obs["probes"]:
        try:
            trace = json.loads(p.get("error_body") or "").get("trace")
        except (ValueError, AttributeError):
            trace = None
        if trace:
            return trace
    return None


def _stack(obs: dict, logs_fn) -> tuple[str | None, str | None]:
    trace = _stack_from_body(obs)
    if trace:
        return trace, "response body"
    logs = logs_fn()
    trace = last_error_from_json(logs)
    if trace:
        return trace, f"docker logs {APP_CONTAINER} (JSON stack_trace)"
    trace = last_error_block(logs)
    return (trace, f"docker logs {APP_CONTAINER} (plain text)") if trace else (None, None)


# --------------------------------------------------------------------------- incident

def build_incident(bad: list[tuple[dict, list[dict]]], cfg: dict, run_id: str, fault_id: str | None,
                   logs_fn=docker_logs) -> dict:
    obs, fired = bad[-1]
    rules = sorted({f["rule"] for f in fired}, key=RULE_PRIORITY.index)
    primary = next(f for f in fired if f["rule"] == rules[0])
    stack, source = _stack(obs, logs_fn)
    probe = next((p for p in obs["probes"] if p["path"] == primary["target"]), None)
    window = obs.get("metrics_window") or {}
    if primary["target"] == METRICS_TARGET:
        top = (window.get("top_5xx") or [{}])[0]
        m = cfg["metrics"]
        endpoint = top.get("uri") or METRICS_TARGET
        status = int(top["status"]) if top.get("status", "").isdigit() else None
        if rules[0] == "metrics_5xx_share":
            expected, actual = f"5xx share <= {100 * m['max_5xx_share']:.1f}%", f"{100 * window['share_5xx']:.1f}%"
        else:
            expected, actual = f"p95 <= {m['max_p95_ms']} ms", f"{window['p95_ms']} ms"
    else:
        endpoint = primary["target"]
        status = probe["http"] if probe else obs["health"]["http"]
        expected = (probe.get("expect_body") or probe["expect_status"]) if probe else "UP"
        actual = probe["value"] if probe else obs["health"].get("status")
    incident = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rule": rules[0],
        "rules_fired": rules,
        "kind": KIND[rules[0]],
        "endpoint": endpoint,
        "status": status,
        "expected": expected,
        "actual": actual,
        "consecutive_bad_checks": len(bad),
        "failing_check": obs,
        "evidence": [f["evidence"] for f in fired],
        "stack_trace": stack,
        "stack_trace_source": source,
    }
    if fault_id:
        incident["fault_id"] = fault_id     # experiment bookkeeping; providers strip it from prompts
    return incident


def watch(timeout_s: float, *, run_id: str | None = None, fault_id: str | None = None, cfg: dict | None = None,
          probes: list[dict] | None = None, http_get=requests.get, clock=time.monotonic, sleep=time.sleep,
          logs_fn=docker_logs, runs_dir: Path | None = None, on_check=None) -> dict | None:
    """on_check(obs, fired) is called after every poll (progress output); it cannot change the result."""
    cfg = cfg or observe.load_config()
    probes = observe.load_probes() if probes is None else probes
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    interval, needed = cfg["poll_interval_s"], cfg["consecutive_failures"]
    out = Path(runs_dir or REPO / "runs") / run_id
    start, k, bad, prev_end = clock(), 0, [], None
    history: list[tuple[float, dict]] = []      # (time, metrics snapshot), oldest first
    while clock() - start < timeout_s:
        begin = clock()
        obs = observe.check(cfg, probes, http_get=http_get, clock=clock)
        prev_end, gap = clock(), None if prev_end is None else begin - prev_end
        obs["t_s"] = round(prev_end - start, 1)
        stalled = observe.suspension(gap, obs, interval, cfg)
        if stalled:
            # The observer was frozen, so these checks prove nothing either way: no incident, no all-clear.
            obs.get("metrics", {}).pop("snapshot", None)
            result = {"run_id": run_id, "status": "invalid", **stalled, "last_check": obs,
                      "bad_checks_before": len(bad), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            out.mkdir(parents=True, exist_ok=True)
            (out / "detector.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        fired = evaluate(obs, cfg)
        snapshot = obs.get("metrics", {}).pop("snapshot", None)
        if snapshot is not None:
            history.append((prev_end, snapshot))
            older = [(t, s) for t, s in history if prev_end - t >= cfg["metrics"]["window_s"]]
            if older:
                base_t, base = older[-1]
                stats = window_stats(base, snapshot, prev_end - base_t)
                if stats is None:                      # counters reset: start the window again from here
                    history = [(prev_end, snapshot)]
                obs["metrics_window"] = stats
                fired += evaluate_metrics(stats, cfg)
                history = [(t, s) for t, s in history if t >= base_t]
        if on_check:
            on_check(obs, fired)
        bad = bad + [(obs, fired)] if fired else []
        if len(bad) >= needed:
            incident = build_incident(bad, cfg, run_id, fault_id, logs_fn)
            out.mkdir(parents=True, exist_ok=True)
            (out / "incident.json").write_text(json.dumps(incident, indent=2), encoding="utf-8")
            return incident
        k += 1
        sleep(max(0.0, start + k * interval - clock()))
    return None
