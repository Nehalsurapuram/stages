"""Detect an incident from repeated observations.

    watch(timeout_s) -> incident dict (also written to runs/<run_id>/incident.json), or None if quiet

Polls observe.check() every poll_interval_s. A check is bad if any rule fires:
  health_down     /actuator/health is not 200 + UP
  probe_5xx       a probe answered 5xx (or did not answer at all)
  probe_mismatch  a probe's status, or its value when expect_body is set, differs from config/probes.yaml
  probe_latency   a probe took latency_limit_ms or longer
An incident is raised after consecutive_failures bad checks in a row. Its stack trace comes from the
response body if the app returned one, otherwise from the last ERROR block of `docker logs autoimmune-app`.
The incident "kind" uses the vocabulary of providers.py (exception, probe_mismatch, health_down, latency).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline import observe

REPO = Path(__file__).resolve().parents[1]
APP_CONTAINER = "autoimmune-app"
RULE_PRIORITY = ["health_down", "probe_5xx", "probe_mismatch", "probe_latency"]
KIND = {"health_down": "health_down", "probe_5xx": "exception", "probe_mismatch": "probe_mismatch",
        "probe_latency": "latency"}
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def evaluate(obs: dict, cfg: dict) -> list[dict]:
    """Rules fired by one observation: [{"rule", "target", "evidence"}]. Empty list = good check."""
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


def last_error_block(logs: str) -> str | None:
    """The last log record containing ERROR plus its stack trace lines (Spring Boot / logback layout)."""
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


def build_incident(bad: list[tuple[dict, list[dict]]], cfg: dict, run_id: str, fault_id: str | None,
                   logs_fn=docker_logs) -> dict:
    obs, fired = bad[-1]
    rules = sorted({f["rule"] for f in fired}, key=RULE_PRIORITY.index)
    primary = next(f for f in fired if f["rule"] == rules[0])
    stack, source = _stack_from_body(obs), "response body"
    if stack is None:
        stack, source = last_error_block(logs_fn()), f"docker logs {APP_CONTAINER}"
    probe = next((p for p in obs["probes"] if p["path"] == primary["target"]), None)
    incident = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rule": rules[0],
        "rules_fired": rules,
        "kind": KIND[rules[0]],
        "endpoint": primary["target"],
        "status": probe["http"] if probe else obs["health"]["http"],
        "expected": (probe.get("expect_body") or probe["expect_status"]) if probe else "UP",
        "actual": (probe["value"] if probe else obs["health"].get("status")),
        "consecutive_bad_checks": len(bad),
        "failing_check": obs,
        "evidence": [f["evidence"] for f in fired],
        "stack_trace": stack,
        "stack_trace_source": source if stack else None,
    }
    if fault_id:
        incident["fault_id"] = fault_id     # experiment bookkeeping; providers strip it from prompts
    return incident


def watch(timeout_s: float, *, run_id: str | None = None, fault_id: str | None = None, cfg: dict | None = None,
          probes: list[dict] | None = None, http_get=requests.get, clock=time.monotonic, sleep=time.sleep,
          logs_fn=docker_logs, runs_dir: Path | None = None) -> dict | None:
    cfg = cfg or observe.load_config()
    probes = observe.load_probes() if probes is None else probes
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    interval, needed = cfg["poll_interval_s"], cfg["consecutive_failures"]
    start, k, bad = clock(), 0, []
    while clock() - start < timeout_s:
        obs = observe.check(cfg, probes, http_get=http_get)
        obs["t_s"] = round(clock() - start, 1)
        fired = evaluate(obs, cfg)
        bad = bad + [(obs, fired)] if fired else []
        if len(bad) >= needed:
            incident = build_incident(bad, cfg, run_id, fault_id, logs_fn)
            out = Path(runs_dir or REPO / "runs") / run_id
            out.mkdir(parents=True, exist_ok=True)
            (out / "incident.json").write_text(json.dumps(incident, indent=2), encoding="utf-8")
            return incident
        k += 1
        sleep(max(0.0, start + k * interval - clock()))
    return None
