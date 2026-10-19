"""One observation of the running service: /actuator/health plus every probe in config/probes.yaml.

    check() -> {"at", "health": {...}, "probes": [{...}, ...]}

Each entry records the HTTP status, the returned value, the latency in ms and, for 4xx/5xx or transport
errors, the error body or exception. observe.py only measures; detector.py decides what is bad.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
BODY_LIMIT = 500
HEALTH_TIMEOUT_S = 5.0
SUSPEND_FACTOR = 3


def request_timeout_s(cfg: dict) -> float:
    return max(HEALTH_TIMEOUT_S, 2.5 * cfg["latency_limit_ms"] / 1000)


def suspension(gap_s: float | None, check: dict, interval_s: float, cfg: dict) -> dict | None:
    """Was the observer itself frozen (e.g. host sleep)? Then the check proves nothing about the service.

    Two signs, both a gap much longer than the check interval (SUSPEND_FACTOR x):
      - the time from the end of the previous check to the start of this one, normally <= interval_s;
      - one request lasting longer than its own timeout plus that margin, which a slow or hung service
        cannot cause (the request would have timed out first).
    """
    margin = SUSPEND_FACTOR * interval_s
    if gap_s is not None and gap_s > margin:
        return {"invalid": "observer_suspended", "sign": "gap between checks",
                "gap_s": round(gap_s, 1), "limit_s": margin}
    worst_ms = max([check["health"]["ms"]] + [p["ms"] for p in check["probes"]])
    limit_s = request_timeout_s(cfg) + margin
    if worst_ms / 1000 > limit_s:
        return {"invalid": "observer_suspended", "sign": "request outlasted its timeout",
                "request_s": round(worst_ms / 1000, 1), "limit_s": limit_s}
    return None


def load_config(repo: Path = REPO) -> dict:
    return yaml.safe_load((Path(repo) / "config" / "decision.yaml").read_text(encoding="utf-8"))


def load_probes(repo: Path = REPO) -> list[dict]:
    return yaml.safe_load((Path(repo) / "config" / "probes.yaml").read_text(encoding="utf-8")) or []


def base_url(cfg: dict) -> str:
    return f"http://localhost:{cfg['host_port']}"


def same_value(expected: str, actual: str) -> bool:
    """Compare as JSON with exact decimals when both parse, else as stripped text."""
    try:
        return json.loads(expected, parse_float=Decimal) == json.loads(actual, parse_float=Decimal)
    except ValueError:
        return expected.strip() == actual.strip()


def _get(url: str, timeout: float, http_get, clock) -> dict:
    start = clock()
    try:
        r = http_get(url, timeout=timeout)
    except requests.RequestException as e:
        return {"http": None, "value": None, "ms": round((clock() - start) * 1000),
                "error": f"{type(e).__name__}: {e}"[:BODY_LIMIT]}
    out = {"http": r.status_code, "value": r.text[:BODY_LIMIT], "ms": round((clock() - start) * 1000)}
    if r.status_code >= 400:
        out["error_body"] = r.text[:BODY_LIMIT]
    return out


def check(cfg: dict | None = None, probes: list[dict] | None = None, *, http_get=requests.get,
          clock=time.monotonic) -> dict:
    cfg = cfg or load_config()
    probes = load_probes() if probes is None else probes
    timeout = request_timeout_s(cfg)
    health = _get(base_url(cfg) + "/actuator/health", HEALTH_TIMEOUT_S, http_get, clock)
    try:
        health["status"] = json.loads(health["value"]).get("status") if health["value"] else None
    except (ValueError, AttributeError):
        health["status"] = None
    results = []
    for p in probes:
        res = {"probe": p["name"], "path": p["path"], **_get(base_url(cfg) + p["path"], timeout, http_get, clock),
               "expect_status": p["expect_status"], "expect_body": p.get("expect_body")}
        results.append(res)
    return {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "health": health, "probes": results}
