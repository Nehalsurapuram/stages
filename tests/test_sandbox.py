import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import sandbox
from pipeline.providers import get_provider
from pipeline.sandbox import Exec

REPO = Path(__file__).resolve().parents[1]
F06_CORRECT = (REPO / "faults/F06/candidates/correct.diff").read_text(encoding="utf-8")


def _docker_ready() -> str | None:
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode != 0:
            return "docker daemon not reachable"
        for image in (sandbox.MAVEN_IMAGE, sandbox.SEMGREP_IMAGE):
            if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
                return f"image {image} not pulled"
        if subprocess.run(["docker", "volume", "inspect", sandbox.M2_VOLUME], capture_output=True).returncode != 0:
            return f"volume {sandbox.M2_VOLUME} missing"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"docker unavailable: {e}"
    return None


DOCKER_SKIP = _docker_ready()


def _sbx_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("autoimmune-sbx-*")}


# ------------------------------------------------------------- unit tests (Docker faked)

class FakeDocker:
    """Stands in for run_container: records every container's args and the files in its mounted tree."""

    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or (lambda gate: Exec(0, 0.1, '{"results": [], "errors": []}', ""))

    def __call__(self, name, args, timeout):
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        files = []
        for m in mounts:
            host = m.rsplit(":", 2)[0]
            if Path(host).is_dir():
                files += [p.relative_to(host).as_posix() for p in Path(host).rglob("*") if p.is_file()]
        gate = name.rsplit("-", 1)[-1].upper()
        self.calls.append({"gate": gate, "args": args, "files": files, "timeout": timeout})
        return self.outcome(gate)


def test_trees_never_contain_hidden_tests_and_v2_excludes_target(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "run_container", fake)
    res = sandbox.run_gates("F06", {"candidate_id": "c1", "diff": F06_CORRECT}, "unit", runs_dir=tmp_path)

    assert [c["gate"] for c in fake.calls] == ["V0", "V1", "V2", "V3", "V3"]
    for c in fake.calls:
        assert not [f for f in c["files"] if "Hidden" in f or "hidden_tests" in f or "labels.json" in f]
        a = c["args"]
        assert a[a.index("--network") + 1] == "none" and a[a.index("--memory") + 1] == "2g"
        assert a[a.index("--cpus") + 1] == "2" and a[a.index("--user") + 1] != "0:0"
        assert "--cap-drop" in a
    v1, v2 = fake.calls[1], fake.calls[2]
    assert "src/test/java/com/autoimmune/orders/F06TargetTest.java" in v1["files"]
    assert "-Dtest=F06TargetTest" in v1["args"]
    assert not [f for f in v2["files"] if "TargetTest" in f]
    assert f"{sandbox.M2_VOLUME}:/m2:ro" in v1["args"]
    assert [c["timeout"] for c in fake.calls] == [300, 300, 600, 300, 300]
    assert res["passed_all"] is True
    saved = json.loads((tmp_path / "unit/c1/gates.json").read_text(encoding="utf-8"))
    assert saved == res and "candidate_kind" not in json.dumps(saved)


def test_bad_diff_fails_v0_without_docker_and_cleans_up(tmp_path, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "run_container", fake)
    before = _sbx_dirs()
    bad = F06_CORRECT.replace("compareTo(DISCOUNT_THRESHOLD) > 0", "compareTo(NOPE) > 0")
    res = sandbox.run_gates("F06", {"candidate_id": "bad", "diff": bad}, "unit", runs_dir=tmp_path)
    g = res["gates"]
    assert g["V0"]["status"] == "fail" and "patch does not apply" in g["V0"]["log_tail"]
    assert [g[x]["status"] for x in ("V1", "V2", "V3")] == ["not_run"] * 3
    assert fake.calls == [] and res["passed_all"] is False
    assert _sbx_dirs() == before


def test_timeout_is_never_a_pass_and_stops_later_gates(tmp_path, monkeypatch):
    def outcome(gate):
        return Exec(None, 300.0, "partial", "", timed_out=True) if gate == "V1" else Exec(0, 1.0, "", "")
    monkeypatch.setattr(sandbox, "run_container", FakeDocker(outcome))
    before = _sbx_dirs()
    res = sandbox.run_gates("F06", {"candidate_id": "t", "diff": F06_CORRECT}, "unit", runs_dir=tmp_path)
    g = res["gates"]
    assert (g["V0"]["status"], g["V1"]["status"], g["V2"]["status"], g["V3"]["status"]) == \
        ("pass", "timeout", "not_run", "not_run")
    assert res["passed_all"] is False and _sbx_dirs() == before


@pytest.mark.parametrize("ex, status", [
    (Exec(0, 1, "", ""), "pass"),
    (Exec(1, 1, "[ERROR] Tests run: 1, Failures: 1", ""), "fail"),
    (Exec(1, 1, "[ERROR] COMPILATION ERROR", ""), "fail"),
    (Exec(1, 1, "Cannot access central (https://repo.maven.apache.org/maven2) in offline mode", ""), "error"),
    (Exec(125, 1, "", "docker: Error response from daemon: ..."), "error"),
    (Exec(137, 1, "", ""), "error"),
    (Exec(None, 1, "", "could not start docker"), "error"),
    (Exec(None, 300, "", "", timed_out=True), "timeout"),
])
def test_classify(ex, status):
    assert sandbox.classify(ex) == status


def test_new_findings_ignore_line_shifts():
    f = {"rule_id": "r", "file": "app/X.java", "severity": "ERROR", "message": "m", "_key": "String s = a + b;"}
    base = [dict(f, line=10)]
    patched = [dict(f, line=14), dict(f, line=20, _key="String t = c + d;")]
    assert [x["line"] for x in sandbox.new_findings(base, patched)] == [20]


# ------------------------------------------------------------- Docker: network and cleanup

NET_CHECK = """
import java.net.*;
public class NetCheck {
    public static void main(String[] a) {
        int reached = 0;
        try { InetAddress.getByName("repo.maven.apache.org"); System.out.println("dns: resolved"); reached++; }
        catch (Exception e) { System.out.println("dns: blocked (" + e.getClass().getSimpleName() + ")"); }
        try (Socket s = new Socket()) { s.connect(new InetSocketAddress("1.1.1.1", 443), 3000);
             System.out.println("tcp 1.1.1.1:443: connected"); reached++; }
        catch (Exception e) { System.out.println("tcp 1.1.1.1:443: blocked (" + e.getMessage() + ")"); }
        System.exit(reached == 0 ? 0 : 3);
    }
}
"""


def _net_check(tmp_path, network):
    (tmp_path / "NetCheck.java").write_text(NET_CHECK, encoding="utf-8")
    name = sandbox._container_name("net")
    args = sandbox.sandbox_args(name, [(str(tmp_path), "/work", "ro")], workdir="/work", network=network)
    java = sandbox.run_container(name, args + ["--entrypoint", "java", sandbox.MAVEN_IMAGE, "/work/NetCheck.java"], 120)
    name = sandbox._container_name("curl")
    args = sandbox.sandbox_args(name, [], network=network)
    curl = sandbox.run_container(name, args + ["--entrypoint", "curl", sandbox.MAVEN_IMAGE, "-sS", "-m", "5",
                                               "-o", "/dev/null", "https://repo.maven.apache.org/maven2/"], 60)
    return java, curl


@pytest.mark.docker
@pytest.mark.skipif(DOCKER_SKIP is not None, reason=DOCKER_SKIP or "")
def test_network_is_blocked_inside_sandbox(tmp_path):
    java, curl = _net_check(tmp_path, "none")
    print(java.stdout, curl.stderr)
    assert java.exit_code == 0, java.output            # NetCheck exits 0 only if DNS and TCP both failed
    assert "dns: blocked" in java.stdout and "tcp 1.1.1.1:443: blocked" in java.stdout
    assert curl.exit_code not in (0, None), curl.output  # curl could not fetch Maven Central


@pytest.mark.docker
@pytest.mark.skipif(DOCKER_SKIP is not None, reason=DOCKER_SKIP or "")
def test_network_check_detects_access_when_network_allowed(tmp_path):
    """Positive control: the same probe on Docker's default bridge must see the network, or the test above proves nothing."""
    java, curl = _net_check(tmp_path, "bridge")
    if java.exit_code == 0 and curl.exit_code != 0:
        pytest.skip("host has no internet access; control not possible")
    assert java.exit_code == 3 and curl.exit_code == 0, java.output + curl.output


@pytest.mark.docker
@pytest.mark.skipif(DOCKER_SKIP is not None, reason=DOCKER_SKIP or "")
def test_timeout_kills_and_removes_container():
    name = sandbox._container_name("sleep")
    args = sandbox.sandbox_args(name, []) + ["--entrypoint", "sleep", sandbox.MAVEN_IMAGE, "120"]
    ex = sandbox.run_container(name, args, timeout=5)
    assert ex.timed_out and sandbox.classify(ex) == "timeout"
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={name}"], capture_output=True, text=True)
    assert left.stdout.strip() == ""


# ------------------------------------------------------------- Docker: real gates for every scripted candidate

CASES = [(f, k) for f in ("F06", "F01", "F04")
         for k in ("correct", "overfitting", "regressive", "insecure", "slow")
         if (REPO / "faults" / f / "candidates" / f"{k}.diff").exists()]


@pytest.fixture(scope="module")
def gate_results():
    """Run V0-V3 for every scripted candidate (2 sandboxes at a time). Real artifacts go to runs/<run_id>/."""
    if DOCKER_SKIP:
        pytest.skip(DOCKER_SKIP)
    run_id = "pytest-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jobs = []
    for fault in ("F06", "F01", "F04"):
        for p in get_provider("scripted").draft_patches(None, {}, fault, n=None):
            jobs.append((fault, p))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {(f, p.candidate_kind): pool.submit(sandbox.run_gates, f, p, run_id) for f, p in jobs}
        results = {k: fut.result() for k, fut in futures.items()}
    (REPO / "runs" / run_id / "index.json").write_text(json.dumps(
        {f"{f}/{k}": r["candidate_id"] for (f, k), r in results.items()}, indent=2), encoding="utf-8")
    return results


def _statuses(res):
    return tuple(res["gates"][g]["status"] for g in sandbox.GATES)


@pytest.mark.docker
@pytest.mark.skipif(DOCKER_SKIP is not None, reason=DOCKER_SKIP or "")
@pytest.mark.parametrize("fault, kind", CASES)
def test_scripted_candidate_gates(gate_results, fault, kind):
    res = gate_results[(fault, kind)]
    st = _statuses(res)
    v3 = res["gates"]["V3"]
    if kind in ("correct", "slow"):
        assert st == ("pass",) * 4, res
        assert v3["new_findings"] == []
    elif kind == "overfitting":
        # Overfitting candidates are built to pass every pipeline gate; only hidden tests (outside the
        # sandbox, labelling only) catch them. That is the gap the research measures.
        assert st == ("pass",) * 4, res
    elif kind == "regressive":
        assert st == ("pass", "pass", "fail", "not_run"), res
        assert "FAILURE" in res["gates"]["V2"]["log_tail"]
    elif kind == "insecure":
        assert st == ("pass", "pass", "pass", "fail"), res
        errors = [f for f in v3["new_findings"] if f["severity"] == "ERROR"]
        assert [f["rule_id"] for f in errors] == ["sql-string-concatenation"]
        assert errors[0]["file"].startswith("app/src/main/java/") and isinstance(errors[0]["line"], int)
    for g in sandbox.GATES:
        assert res["gates"][g]["status"] != "pass" or res["gates"][g]["exit_code"] == 0
