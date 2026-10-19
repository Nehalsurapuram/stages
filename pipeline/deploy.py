"""Deploy a patched build, watch it through a health window, then promote or roll back.

    start_baseline()                       build app/ as order-service:baseline and run it as autoimmune-app
    record_probes()                        write config/probes.yaml with expected values from the running (healthy) app
    deploy(patched_source_dir, run_id)     build order-service:<run_id>, swap the running container, wait until it answers
    health_window()                        every check_interval_s for health_window_s: /actuator/health UP,
                                           each probe returns its expected value in under latency_limit_ms
    release(decision, src, run_id, ...)    approval gate + deploy + health window + promote (deploy.json)
                                           or roll back (rollback.json)
    approve_cli(decision, diff)            human approve / reject for ESCALATE candidates

Rollback swaps the container back to the previous image tag. This module never runs git.
A REJECT never reaches deploy; an ESCALATE needs an "approve" record first. Rollback (after a deploy)
and reject (before any deploy) are separate events, recorded by different modules.
"""
from __future__ import annotations

import getpass
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
APP_CONTAINER = "autoimmune-app"
BASELINE_TAG = "order-service:baseline"
STARTUP_TIMEOUT_S = 120
BUILD_TIMEOUT_S = 900


class DeployError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(repo: Path = REPO) -> dict:
    return yaml.safe_load((Path(repo) / "config" / "decision.yaml").read_text(encoding="utf-8"))


def load_probes(repo: Path = REPO) -> list[dict]:
    return yaml.safe_load((Path(repo) / "config" / "probes.yaml").read_text(encoding="utf-8")) or []


def base_url(cfg: dict) -> str:
    return f"http://localhost:{cfg['host_port']}"


# --------------------------------------------------------------------------- docker

def _docker(args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def image_tag(run_id: str) -> str:
    tag = re.sub(r"[^a-z0-9_.-]", "-", run_id.lower()).strip(".-")[:120]
    return f"order-service:{tag}"


def current_image() -> str | None:
    r = _docker(["inspect", "--format", "{{.Config.Image}}", APP_CONTAINER])
    return r.stdout.strip() if r.returncode == 0 else None


def build_image(source_dir: Path, tag: str) -> float:
    start = time.monotonic()
    r = _docker(["build", "-t", tag, str(source_dir)], timeout=BUILD_TIMEOUT_S)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-15:])
        raise DeployError(f"docker build of {tag} failed:\n{tail}")
    return time.monotonic() - start


def _stop_app() -> None:
    _docker(["rm", "-f", APP_CONTAINER])


def _run_app(tag: str, cfg: dict) -> None:
    _stop_app()
    r = _docker(["run", "-d", "--name", APP_CONTAINER, "-p", f"{cfg['host_port']}:{cfg['app_port']}",
                 "--memory", "1g", "--cpus", "2", tag])
    if r.returncode != 0:
        raise DeployError(f"docker run {tag} failed: {r.stderr.strip()}")


# --------------------------------------------------------------------------- HTTP checks

def _health(cfg: dict, http_get=requests.get) -> dict:
    url = base_url(cfg) + "/actuator/health"
    start = time.monotonic()
    try:
        r = http_get(url, timeout=5)
        ms = (time.monotonic() - start) * 1000
        status = r.json().get("status") if "json" in r.headers.get("Content-Type", "") else None
        return {"http": r.status_code, "status": status, "ms": round(ms), "ok": r.status_code == 200 and status == "UP"}
    except requests.RequestException as e:
        return {"http": None, "status": None, "ms": round((time.monotonic() - start) * 1000), "ok": False,
                "error": type(e).__name__}


def wait_until(cfg: dict, healthy: bool, timeout: float = STARTUP_TIMEOUT_S, http_get=requests.get) -> float:
    """Seconds until /actuator/health answers at all (healthy=False) or answers UP (healthy=True)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        h = _health(cfg, http_get)
        if (h["ok"] if healthy else h["http"] is not None):
            return time.monotonic() - start
        time.sleep(1)
    raise DeployError(f"{APP_CONTAINER} not {'healthy' if healthy else 'answering'} after {timeout:.0f}s")


def _same_body(expected: str, actual: str) -> bool:
    try:
        return json.loads(expected, parse_float=Decimal) == json.loads(actual, parse_float=Decimal)
    except ValueError:
        return expected.strip() == actual.strip()


def check_once(cfg: dict, probes: list[dict], http_get=requests.get) -> dict:
    limit = cfg["latency_limit_ms"]
    health = _health(cfg, http_get)
    results, breaches = [], []
    if not health["ok"]:
        breaches.append(f"/actuator/health not UP (http={health['http']}, status={health['status']})")
    for p in probes:
        start = time.monotonic()
        try:
            r = http_get(base_url(cfg) + p["path"], timeout=max(5.0, 2.5 * limit / 1000))
            ms = round((time.monotonic() - start) * 1000)
            body_ok = r.status_code == p["expect_status"] and _same_body(p["expect_body"], r.text)
            res = {"probe": p["name"], "http": r.status_code, "ms": ms, "body_ok": body_ok,
                   "latency_ok": ms < limit, "body": r.text[:200]}
        except requests.RequestException as e:
            ms = round((time.monotonic() - start) * 1000)
            res = {"probe": p["name"], "http": None, "ms": ms, "body_ok": False, "latency_ok": False,
                   "error": type(e).__name__}
        if not res["body_ok"]:
            breaches.append(f"probe {p['name']} returned http={res['http']} body={res.get('body', res.get('error'))!r}, "
                            f"expected http={p['expect_status']} body={p['expect_body']!r}")
        if not res["latency_ok"]:
            breaches.append(f"probe {p['name']} took {res['ms']} ms, limit {limit} ms")
        results.append(res)
    return {"at": _now(), "health": health, "probes": results, "ok": not breaches, "breaches": breaches}


def health_window(cfg: dict | None = None, probes: list[dict] | None = None, *, http_get=requests.get,
                  clock=time.monotonic, sleep=time.sleep) -> dict:
    """Check every check_interval_s for health_window_s; stop at the first breach. Every check is recorded."""
    cfg = cfg or load_config()
    probes = load_probes() if probes is None else probes
    window, interval = cfg["health_window_s"], cfg["check_interval_s"]
    start = clock()
    checks = []
    k = 0
    while True:
        c = check_once(cfg, probes, http_get)
        c["t_s"] = round(clock() - start, 1)
        checks.append(c)
        if not c["ok"]:
            return {"passed": False, "checks": checks, "breach": c, "breach_clock": clock()}
        k += 1
        if k * interval > window:
            return {"passed": True, "checks": checks, "breach": None, "breach_clock": None}
        sleep(max(0.0, start + k * interval - clock()))


# --------------------------------------------------------------------------- baseline and probes

def start_baseline(repo: Path = REPO, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config(repo)
    build_s = build_image(Path(repo) / "app", BASELINE_TAG)
    _run_app(BASELINE_TAG, cfg)
    startup_s = wait_until(cfg, healthy=True)
    return {"image": BASELINE_TAG, "container": APP_CONTAINER, "url": base_url(cfg),
            "build_s": round(build_s, 1), "startup_s": round(startup_s, 1)}


PROBE_PATHS = [
    ("order1-total", "/api/orders/1/total"),
    ("order2-discount-at-threshold", "/api/orders/2/discount"),
    ("search-alice", "/api/orders/search?customer=alice"),
]


def record_probes(repo: Path = REPO, cfg: dict | None = None, http_get=requests.get) -> list[dict]:
    """Record expected status/body of each probe from the running app. Run against the healthy baseline only."""
    cfg = cfg or load_config(repo)
    if current_image() != BASELINE_TAG:
        raise DeployError(f"record_probes must run against {BASELINE_TAG}, not {current_image()}")
    probes = []
    for name, path in PROBE_PATHS:
        r = http_get(base_url(cfg) + path, timeout=10)
        probes.append({"name": name, "path": path, "expect_status": r.status_code, "expect_body": r.text})
    header = (f"# Health-window probes. Expected values recorded from the healthy app ({BASELINE_TAG}) on {_now()}\n"
              "# by pipeline.deploy.record_probes(). Bodies are compared as JSON (exact decimals) or as text.\n")
    (Path(repo) / "config" / "probes.yaml").write_text(
        header + yaml.safe_dump(probes, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8", newline="\n")
    return probes


# --------------------------------------------------------------------------- deploy / rollback / release

def deploy(patched_source_dir: Path, run_id: str, cfg: dict | None = None) -> dict:
    """Build the patched source, swap the running container to it, wait until it answers."""
    cfg = cfg or load_config()
    previous = current_image()
    if previous is None:
        raise DeployError(f"no running {APP_CONTAINER} to replace; call start_baseline() first")
    tag = image_tag(run_id)
    build_s = build_image(Path(patched_source_dir), tag)   # a failed build leaves the running app untouched
    start = time.monotonic()
    _run_app(tag, cfg)
    try:
        answer_s, answered = round(wait_until(cfg, healthy=False), 1), True
    except DeployError:
        answer_s, answered = None, False
    return {"run_id": run_id, "new_image": tag, "previous_image": previous, "build_s": round(build_s, 1),
            "swap_to_answer_s": answer_s, "answered": answered, "swap_started_at": _now(),
            "swap_clock": start}


def rollback(previous_image: str, failed_image: str, breach: dict, breach_clock: float, cfg: dict) -> dict:
    _stop_app()
    _run_app(previous_image, cfg)
    wait_until(cfg, healthy=True)
    return {"event": "rollback", "at": _now(), "reason": "; ".join(breach.get("breaches", [])) or "unknown",
            "failed_check": breach, "rolled_back_image": failed_image, "restored_image": previous_image,
            "seconds_breach_to_healthy": round(time.monotonic() - breach_clock, 1), "healthy_after": True}


def _decision_dict(decision) -> dict:
    return decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)


def may_deploy(decision, approval: dict | None = None) -> tuple[bool, str]:
    d = _decision_dict(decision)["decision"]
    if d == "AUTO_APPLY":
        return True, "AUTO_APPLY"
    if d == "ESCALATE":
        if approval and approval.get("answer") == "approve":
            return True, f"ESCALATE approved by {approval.get('user')} at {approval.get('time')}"
        return False, "ESCALATE without an approve record: staged for human review, not deployed"
    return False, f"{d} candidates are never deployed"


def release(decision, patched_source_dir: Path, run_id: str, *, approval: dict | None = None,
            run_dir: Path | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    run_dir = Path(run_dir or REPO / "runs" / run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    ok, why = may_deploy(decision, approval)
    if not ok:
        return {"deployed": False, "staged": _decision_dict(decision)["decision"] == "ESCALATE",
                "rolled_back": None, "reason": why, "recovery_s": None}

    dep = deploy(patched_source_dir, run_id, cfg)
    if dep["answered"]:
        window = health_window(cfg)
    else:
        breach = {"at": _now(), "ok": False, "breaches": [f"new container did not answer within {STARTUP_TIMEOUT_S}s"]}
        window = {"passed": False, "checks": [breach], "breach": breach, "breach_clock": time.monotonic()}

    record = {**{k: v for k, v in dep.items() if k != "swap_clock"}, "authorised_by": why,
              "decision": _decision_dict(decision), "approval": approval,
              "health_window": {"passed": window["passed"], "window_s": cfg["health_window_s"],
                                "interval_s": cfg["check_interval_s"], "latency_limit_ms": cfg["latency_limit_ms"],
                                "checks": window["checks"]}}
    if window["passed"]:
        record.update(outcome="promoted", promoted_image=dep["new_image"], finished_at=_now())
        (run_dir / "deploy.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return {"deployed": True, "staged": False, "rolled_back": False, "outcome": "promoted",
                "image": dep["new_image"], "checks": len(window["checks"]), "recovery_s": None}

    rb = rollback(dep["previous_image"], dep["new_image"], window["breach"], window["breach_clock"], cfg)
    record.update(outcome="rolled_back", finished_at=_now())
    (run_dir / "deploy.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (run_dir / "rollback.json").write_text(json.dumps(rb, indent=2), encoding="utf-8")
    return {"deployed": True, "staged": False, "rolled_back": True, "outcome": "rolled_back",
            "image": rb["restored_image"], "reason": rb["reason"], "checks": len(window["checks"]),
            "recovery_s": rb["seconds_breach_to_healthy"]}


# --------------------------------------------------------------------------- human approval

def approve_cli(decision, diff: str, *, input_fn=input, out=print, user: str | None = None,
                run_dir: Path | None = None, max_tries: int = 3) -> dict:
    d = _decision_dict(decision)
    out("=" * 72)
    out(f"Decision: {d['decision']}   tier: {d['tier']}   policy: {d.get('policy_version')}")
    out("Reasons:")
    for r in d["reasons"]:
        out(f"  - {r}")
    out("-" * 72)
    out(diff.rstrip())
    out("=" * 72)
    answer = "no_answer"
    for _ in range(max_tries):
        try:
            raw = input_fn("approve / reject: ").strip().lower()
        except EOFError:
            break
        if raw in ("approve", "reject"):
            answer = raw
            break
        out(f"please type 'approve' or 'reject' (got {raw!r})")
    record = {"answer": answer, "user": user or getpass.getuser(), "time": _now(),
              "decision": d["decision"], "tier": d["tier"], "reasons": d["reasons"]}
    if run_dir:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        (Path(run_dir) / "approval.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record
