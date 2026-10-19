"""Validation gates V0-V3 for one candidate patch, each in a locked-down Docker container.

    run_gates(fault_id, patch, run_id) -> dict, also written to runs/<run_id>/<candidate_id>/gates.json

Workspace: a temp copy of app/ with faults/<id>/inject.diff applied (the "baseline"), then the candidate
diff (the "patched" tree). Each gate gets a fresh copy of the tree it needs:
  V0  git apply --check on the host, then `mvn -o -q compile`
  V1  `mvn -o test -Dtest=<target test class>` with faults/<id>/target_test/*.java copied in
  V2  `mvn -o test`: the existing app/ suite only (no target test)
  V3  Semgrep (config/semgrep.yml, offline) on baseline and patched production code; only NEW findings
      count. fail = a new ERROR finding; new WARNING/INFO findings are recorded and leave the status pass
      (decide.py turns them into ESCALATE).
Gates run in order and stop at the first that does not pass; the rest are "not_run".
Status is pass / fail / error / timeout / not_run. A timeout or tool error is never a pass.

Container: --network none, --memory 2g, --cpus 2, --pids-limit 512, --cap-drop ALL, no-new-privileges,
non-root user, the autoimmune-m2 Maven cache mounted read-only (a candidate cannot poison it).
hidden_tests/ and labels.json are never copied, mounted or read. gates.json carries no candidate_kind.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
MAVEN_IMAGE = "maven:3.9-eclipse-temurin-17"
SEMGREP_IMAGE = "semgrep/semgrep:1.100.0"
M2_VOLUME = "autoimmune-m2"
TEST_DIR = "app/src/test/java/com/autoimmune/orders"
GATES = ("V0", "V1", "V2", "V3")
LOG_TAIL_LINES = 25
MVN = ["-o", "-B", "-Dmaven.repo.local=/m2/repository", "-Duser.home=/tmp"]
# Output that means the environment, not the candidate, failed.
TOOL_ERROR_MARKERS = (
    "in offline mode", "Non-resolvable parent POM", "Could not resolve dependencies",
    "Unable to find image", "Error response from daemon", "OCI runtime", "Cannot connect to the Docker daemon",
)


class SandboxError(RuntimeError):
    """The sandbox itself could not be set up (e.g. inject.diff does not apply)."""


def sandbox_user() -> str:
    """Host uid:gid on Linux/macOS (so the host can delete what the container writes), else 1000:1000; never root."""
    if hasattr(os, "getuid") and os.getuid() != 0:
        return f"{os.getuid()}:{os.getgid()}"
    return "1000:1000"


def load_config(repo: Path = REPO) -> dict:
    return yaml.safe_load((repo / "config" / "decision.yaml").read_text(encoding="utf-8"))


def sandbox_args(name: str, mounts: list[tuple[str, str, str]], workdir: str | None = None,
                 network: str = "none") -> list[str]:
    """`docker run` arguments up to (not including) the image. mounts: (host path or volume, container path, ro|rw)."""
    args = ["docker", "run", "--rm", "--name", name, "--network", network,
            "--memory", "2g", "--cpus", "2", "--pids-limit", "512",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", sandbox_user(), "-e", "HOME=/tmp"]
    for host, container, mode in mounts:
        args += ["-v", f"{host}:{container}:{mode}"]
    if workdir:
        args += ["-w", workdir]
    return args


@dataclass
class Exec:
    exit_code: int | None
    seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        return self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr


def _decode(data) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def run_container(name: str, args: list[str], timeout: float) -> Exec:
    """Run `docker run --name <name> ...`; on any outcome force-remove the container."""
    start = time.monotonic()
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return Exec(r.returncode, time.monotonic() - start, r.stdout, r.stderr)
    except subprocess.TimeoutExpired as e:
        return Exec(None, time.monotonic() - start, _decode(e.stdout), _decode(e.stderr), timed_out=True)
    except OSError as e:
        return Exec(None, time.monotonic() - start, "", f"could not start docker: {e}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def classify(ex: Exec) -> str:
    if ex.timed_out:
        return "timeout"
    if ex.exit_code == 0:
        return "pass"
    if ex.exit_code is None or ex.exit_code in (125, 126, 127, 137) \
            or any(m in ex.output for m in TOOL_ERROR_MARKERS):
        return "error"
    return "fail"


def _tail(text: str, n: int = LOG_TAIL_LINES) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


def _record(status: str, seconds: float, exit_code, log: str, **extra) -> dict:
    return {"status": status, "seconds": round(seconds, 1), "exit_code": exit_code, "log_tail": _tail(log), **extra}


NOT_RUN = {"status": "not_run", "seconds": 0.0, "exit_code": None, "log_tail": ""}


def _container_name(gate: str) -> str:
    return f"autoimmune-{uuid.uuid4().hex[:12]}-{gate.lower()}"


def _git_apply(cwd: Path, diff: str, check: bool) -> subprocess.CompletedProcess:
    args = ["git", "apply"] + (["--check"] if check else []) + ["-"]
    text = diff if diff.endswith("\n") else diff + "\n"
    # Bytes, not text mode: on Windows text-mode stdin turns \n into \r\n and git apply then rejects hunks.
    r = subprocess.run(args, cwd=cwd, input=text.encode("utf-8"), capture_output=True)
    return subprocess.CompletedProcess(r.args, r.returncode, r.stdout.decode("utf-8", "replace"),
                                       r.stderr.decode("utf-8", "replace"))


def _copy_tree(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("target"))
    return dst


def _maven(tree: Path, gate: str, mvn_args: list[str], timeout: float) -> Exec:
    name = _container_name(gate)
    args = sandbox_args(name, [(str(tree), "/work", "rw"), (M2_VOLUME, "/m2", "ro")], workdir="/work")
    return run_container(name, args + ["--entrypoint", "mvn", MAVEN_IMAGE] + MVN + mvn_args, timeout)


# --------------------------------------------------------------------------- V3

def _semgrep(tree: Path, repo: Path, timeout: float) -> tuple[Exec, list[dict] | None, str | None]:
    """Scan tree/src/main. Returns (exec, findings or None, error message or None)."""
    name = _container_name("v3")
    args = sandbox_args(name, [(str(tree), "/src", "ro"), (str(repo / "config"), "/rules", "ro")])
    ex = run_container(name, args + [SEMGREP_IMAGE, "semgrep", "scan", "--config", "/rules/semgrep.yml",
                                     "--metrics=off", "--disable-version-check", "--quiet", "--json",
                                     "/src/src/main"], timeout)
    if ex.timed_out or ex.exit_code != 0:
        return ex, None, "semgrep timed out" if ex.timed_out else f"semgrep exited {ex.exit_code}"
    try:
        data = json.loads(ex.stdout)
    except ValueError as e:
        return ex, None, f"semgrep output is not JSON: {e}"
    errors = [e for e in data.get("errors", []) if e.get("level") == "error"]
    if errors:
        return ex, None, f"semgrep reported {len(errors)} error(s): {errors[0].get('message', '')[:200]}"
    findings = []
    for r in data.get("results", []):
        rel = r["path"].removeprefix("/src/")
        lines = (tree / rel).read_text(encoding="utf-8").splitlines()
        snippet = " ".join(l.strip() for l in lines[r["start"]["line"] - 1:r["end"]["line"]])
        findings.append({"rule_id": r["check_id"].rsplit(".", 1)[-1], "file": f"app/{rel}",
                         "line": r["start"]["line"], "severity": r["extra"]["severity"],
                         "message": r["extra"].get("message", ""), "_key": snippet})
    return ex, findings, None


def new_findings(baseline: list[dict], patched: list[dict]) -> list[dict]:
    """Findings in patched that are not in baseline, matched on (rule, file, code) so line shifts don't count."""
    remaining = Counter((f["rule_id"], f["file"], f["_key"]) for f in baseline)
    new = []
    for f in patched:
        k = (f["rule_id"], f["file"], f["_key"])
        if remaining[k]:
            remaining[k] -= 1
        else:
            new.append({k2: v for k2, v in f.items() if k2 != "_key"})
    return new


def prepare_patched_app(fault_id: str, diff: str | None, dest: Path, repo: Path = REPO) -> Path:
    """dest/app = app/ + inject.diff (+ candidate diff): a buildable source tree for deploy.py. Returns dest/app.
    diff=None gives the faulty build (fault injected, no fix), used to trigger the detector."""
    dest = Path(dest)
    _copy_tree(Path(repo) / "app", dest / "app")
    inject = (Path(repo) / "faults" / fault_id / "inject.diff").read_text(encoding="utf-8")
    for label, d in (("inject.diff", inject), ("candidate diff", diff)):
        if d is None:
            continue
        r = _git_apply(dest, d, check=False)
        if r.returncode != 0:
            raise SandboxError(f"{label} does not apply: {r.stderr.strip()}")
    return dest / "app"


# --------------------------------------------------------------------------- run_gates

def _get(patch, attr: str):
    return patch[attr] if isinstance(patch, dict) else getattr(patch, attr)


def run_gates(fault_id: str, patch, run_id: str | None = None, *, repo: Path = REPO,
              runs_dir: Path | None = None, config: dict | None = None) -> dict:
    repo = Path(repo)
    cfg = config or load_config(repo)
    timeouts = cfg["gate_timeouts_s"]
    candidate_id, diff = _get(patch, "candidate_id"), _get(patch, "diff")
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(runs_dir or repo / "runs") / run_id / candidate_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "candidate.diff").write_text(diff, encoding="utf-8", newline="\n")

    fault_dir = repo / "faults" / fault_id
    fault = json.loads((fault_dir / "fault.json").read_text(encoding="utf-8"))
    target_class = fault["target_test"].rsplit(".", 1)[-1]
    target_files = sorted((fault_dir / "target_test").glob("*.java"))
    if not target_files:
        raise SandboxError(f"no target test in {fault_dir / 'target_test'}")

    result = {
        "run_id": run_id, "fault_id": fault_id, "candidate_id": candidate_id,
        "provider_mode": _get(patch, "provider_mode") if not isinstance(patch, dict) else patch.get("provider_mode"),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sandbox": {"maven_image": MAVEN_IMAGE, "semgrep_image": SEMGREP_IMAGE, "network": "none",
                    "memory": "2g", "cpus": "2", "user": sandbox_user(), "m2_volume": f"{M2_VOLUME} (read-only)",
                    "timeouts_s": timeouts},
        "target_test": target_class,
        "gates": {},
    }
    gates = result["gates"]
    logs: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="autoimmune-sbx-") as tmp:
        tmp = Path(tmp)
        base = _copy_tree(repo / "app", tmp / "base" / "app").parent
        res = subprocess.run(["git", "apply", str(fault_dir / "inject.diff")], cwd=base,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise SandboxError(f"inject.diff does not apply to app/: {res.stderr.strip()}")
        patched = _copy_tree(base / "app", tmp / "patched" / "app").parent

        # V0: apply check + compile
        start = time.monotonic()
        chk = _git_apply(patched, diff, check=True)
        if chk.returncode != 0:
            logs["V0"] = chk.stderr
            gates["V0"] = _record("fail", time.monotonic() - start, chk.returncode, chk.stderr,
                                  step="git apply --check")
        else:
            _git_apply(patched, diff, check=False)
            ex = _maven(_copy_tree(patched / "app", tmp / "v0" / "app"), "V0", ["-q", "compile"], timeouts["V0"])
            logs["V0"] = ex.output
            gates["V0"] = _record(classify(ex), time.monotonic() - start, ex.exit_code, ex.output, step="mvn compile")

        # V1: target test only
        if gates["V0"]["status"] == "pass":
            tree = _copy_tree(patched / "app", tmp / "v1" / "app")
            for f in target_files:
                shutil.copy2(f, tree / TEST_DIR.removeprefix("app/") / f.name)
            ex = _maven(tree, "V1", ["test", f"-Dtest={target_class}"], timeouts["V1"])
            logs["V1"] = ex.output
            gates["V1"] = _record(classify(ex), ex.seconds, ex.exit_code, ex.output, test_class=target_class)

        # V2: existing suite only
        if gates.get("V1", {}).get("status") == "pass":
            ex = _maven(_copy_tree(patched / "app", tmp / "v2" / "app"), "V2", ["test"], timeouts["V2"])
            logs["V2"] = ex.output
            gates["V2"] = _record(classify(ex), ex.seconds, ex.exit_code, ex.output)

        # V3: new Semgrep findings, baseline (fault only) vs patched
        if gates.get("V2", {}).get("status") == "pass":
            b_ex, b_find, b_err = _semgrep(base / "app", repo, timeouts["V3"])
            p_ex, p_find, p_err = _semgrep(patched / "app", repo, timeouts["V3"])
            seconds = b_ex.seconds + p_ex.seconds
            logs["V3"] = f"--- baseline\n{b_ex.output}\n--- patched\n{p_ex.output}"
            err = b_err or p_err
            if err:
                status = "timeout" if (b_ex.timed_out or p_ex.timed_out) else "error"
                gates["V3"] = _record(status, seconds, p_ex.exit_code if b_err is None else b_ex.exit_code,
                                      f"{err}\n{_tail(b_ex.stderr if b_err else p_ex.stderr)}", new_findings=None)
            else:
                new = new_findings(b_find, p_find)
                status = "fail" if any(f["severity"] == "ERROR" for f in new) else "pass"
                summary = "; ".join(f"{f['severity']} {f['rule_id']} {f['file']}:{f['line']}" for f in new)
                gates["V3"] = _record(status, seconds, p_ex.exit_code, f"new findings: {summary or 'none'}",
                                      new_findings=new, baseline_findings=len(b_find), patched_findings=len(p_find))

    for g in GATES:
        gates.setdefault(g, dict(NOT_RUN))
    result["passed_all"] = all(gates[g]["status"] == "pass" for g in GATES)
    result["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for g, text in logs.items():
        (out_dir / f"{g.lower()}.log").write_text(text, encoding="utf-8")
    (out_dir / "gates.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
