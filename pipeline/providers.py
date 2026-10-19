"""Provider interface: the only place a model may be used (diagnose and draft_patches).

Modes
-----
rule      diagnose only. Top application stack frame of the incident; for incidents without a stack
          trace (probe mismatch, health DOWN) the fault's diagnosed_file from faults/<id>/fault.json,
          i.e. oracle localisation. Never an LLM result.
scripted  draft_patches only. Patches read from faults/<id>/candidates/*.diff. Never an LLM result.
replay    diagnose and draft_patches from cache/, keyed by sha256(provider, model, prompt, temperature).
          No network. A missing entry is an error.
live      diagnose and draft_patches via an OpenAI-compatible /chat/completions endpoint configured by
          LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (Ollama or a hosted API). Every response is saved to cache/.

Each result carries provider_mode, so diag_mode and patch_mode are recorded separately by the caller.
Every patch is checked with `git apply --check` against a temp copy of app/ with the fault injected.

Integrity: this module never reads faults/<id>/hidden_tests/ or labels.json, and refuses source files
from those paths. candidate_kind (scripted only) is for records and analysis, never for the decision engine.

Incident contract (produced by detector.py):
    {"kind": "exception" | "probe_mismatch" | "health_down" | ..., "fault_id": "F01",
     "stack_trace": "...", "endpoint": "...", "status": 500, "expected": ..., "actual": ..., ...}
fault_id is used for rule-mode fallback and is stripped before any prompt is built.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import jsonschema
import requests

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[1]
APP_PACKAGE = "com.autoimmune."
JAVA_ROOT = "app/src/main/java/"
MAX_RETRIES = 2                      # retries after the first attempt
HTTP_TIMEOUT_S = 180
FORBIDDEN_PARTS = ("hidden_tests", "labels.json")


class ProviderError(RuntimeError):
    """A provider could not produce a valid result. The message says why."""


@dataclass
class Cause:
    rank: int
    file: str
    line: int | None
    reason: str
    evidence: str


@dataclass
class Diagnosis:
    causes: list[Cause]
    provider_mode: str
    model: str | None = None
    attempts: list[dict] = field(default_factory=list)
    oracle_localisation: bool = False   # rule mode from fault.json diagnosed_file (ground truth), not from evidence

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Patch:
    candidate_id: str
    diff: str
    explanation: str
    provider_mode: str
    candidate_kind: str | None = None   # scripted only: file name stem; never shown to the decision engine
    model: str | None = None
    applies: bool | None = None         # git apply --check on app/ + inject.diff
    apply_error: str | None = None
    attempts: list[dict] = field(default_factory=list)   # live/replay request attempts (bounded retries)

    def to_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    mode: str

    def diagnose(self, incident: dict, source_files: dict[str, str]) -> Diagnosis: ...

    def draft_patches(self, diagnosis: Diagnosis, source_files: dict[str, str], fault_id: str,
                      n: int | None = 3) -> list[Patch]: ...


def get_provider(mode: str, **kwargs) -> Provider:
    if mode == "rule":
        return RuleProvider(**kwargs)
    if mode == "scripted":
        return ScriptedProvider(**kwargs)
    if mode in ("live", "replay"):
        return LLMProvider(mode, **kwargs)
    raise ValueError(f"unknown provider mode {mode!r}; expected rule, scripted, replay or live")


# --------------------------------------------------------------------------- shared helpers

def _assert_allowed(path: str) -> None:
    norm = path.replace("\\", "/")
    if any(part in norm for part in FORBIDDEN_PARTS):
        raise ProviderError(f"refusing to use {path}: hidden tests and labels are never given to a provider")


def _load_fault(repo: Path, fault_id: str) -> dict:
    path = repo / "faults" / fault_id / "fault.json"
    if not path.is_file():
        raise ProviderError(f"unknown fault {fault_id!r}: {path} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def check_patches(patches: list[Patch], fault_id: str, repo: Path = REPO) -> list[Patch]:
    """Set applies/apply_error on each patch: git apply --check against app/ with inject.diff applied."""
    inject = repo / "faults" / fault_id / "inject.diff"
    with tempfile.TemporaryDirectory(prefix="autoimmune-apply-") as tmp:
        work = Path(tmp)
        shutil.copytree(repo / "app", work / "app", ignore=shutil.ignore_patterns("target"))
        res = subprocess.run(["git", "apply", str(inject)], cwd=work, capture_output=True, text=True)
        if res.returncode != 0:
            raise ProviderError(f"{inject} does not apply to app/: {res.stderr.strip()}")
        for p in patches:
            if not p.diff.strip():
                p.applies, p.apply_error = False, "empty diff"
                continue
            diff = p.diff if p.diff.endswith("\n") else p.diff + "\n"
            # Bytes, not text mode: on Windows text-mode stdin turns \n into \r\n and git apply rejects hunks.
            res = subprocess.run(["git", "apply", "--check", "-"], cwd=work, input=diff.encode("utf-8"),
                                 capture_output=True)
            p.applies = res.returncode == 0
            stderr = res.stderr.decode("utf-8", "replace").strip()
            p.apply_error = None if p.applies else (stderr or f"git apply exited {res.returncode}")
            if not p.applies:
                log.info("patch %s does not apply: %s", p.candidate_id, p.apply_error)
    return patches


# --------------------------------------------------------------------------- rule mode

FRAME_RE = re.compile(r"at\s+(?:[\w.$-]+/)*([\w$.]+)\.([\w$<>]+)\(([^:()]+\.java):(\d+)\)")
EXC_RE = re.compile(r"^\s*(?:Caused by:\s*)?([\w$.]+(?:Exception|Error)\b.*)$")


def _trace_sections(trace: str) -> list[tuple[str, list[str]]]:
    """Split a stack trace into (exception line, frame lines) sections; later sections are deeper causes."""
    sections: list[tuple[str, list[str]]] = []
    for line in trace.splitlines():
        if FRAME_RE.search(line):
            if not sections:
                sections.append(("", []))
            sections[-1][1].append(line.strip())
        elif EXC_RE.match(line) and "\tat " not in line:
            sections.append((EXC_RE.match(line).group(1).strip(), []))
    return sections


class RuleProvider:
    mode = "rule"

    def __init__(self, repo: Path = REPO):
        self.repo = Path(repo)

    def diagnose(self, incident: dict, source_files: dict[str, str]) -> Diagnosis:
        for path in source_files:
            _assert_allowed(path)
        causes = self._from_stack_trace(incident.get("stack_trace") or "", source_files)
        if causes:
            return Diagnosis(causes=causes, provider_mode=self.mode)
        return Diagnosis(causes=self._from_fault(incident), provider_mode=self.mode, oracle_localisation=True)

    def _from_stack_trace(self, trace: str, source_files: dict[str, str]) -> list[Cause]:
        # Deepest section (root cause) that has application frames.
        for exc_line, frames in reversed(_trace_sections(trace)):
            app = [(f, FRAME_RE.search(f)) for f in frames]
            app = [(f, m) for f, m in app if m.group(1).startswith(APP_PACKAGE)]
            if not app:
                continue
            causes: list[Cause] = []
            seen: set[tuple[str, int]] = set()
            for frame, m in app:
                cls, method, filename, line = m.group(1), m.group(2), m.group(3), int(m.group(4))
                path = self._source_path(cls, filename, source_files)
                if (path, line) in seen:
                    continue
                seen.add((path, line))
                rank = len(causes) + 1
                where = "top application frame" if rank == 1 else f"application frame #{rank} in the stack"
                causes.append(Cause(rank=rank, file=path, line=line,
                                    reason=f"{where}: {cls.rsplit('.', 1)[-1]}.{method}",
                                    evidence=f"{exc_line} | {frame}".strip(" |")))
                if len(causes) == 3:
                    break
            return causes
        return []

    @staticmethod
    def _source_path(cls: str, filename: str, source_files: dict[str, str]) -> str:
        package = cls.split("$")[0].rsplit(".", 1)[0].replace(".", "/")
        suffix = f"{package}/{filename}"
        for path in source_files:
            if path.replace("\\", "/").endswith(suffix):
                return path.replace("\\", "/")
        return JAVA_ROOT + suffix

    def _from_fault(self, incident: dict) -> list[Cause]:
        fault_id = incident.get("fault_id")
        if not fault_id:
            raise ProviderError("rule mode: incident has no stack trace with application frames and no fault_id")
        fault = _load_fault(self.repo, fault_id)
        detail = {k: incident[k] for k in ("kind", "endpoint", "status", "expected", "actual") if k in incident}
        return [Cause(rank=1, file=fault["diagnosed_file"], line=None,
                      reason=f"no stack trace for a {incident.get('kind', 'unknown')} incident; "
                             f"oracle localisation from faults/{fault_id}/fault.json diagnosed_file",
                      evidence=json.dumps(detail, sort_keys=True))]

    def draft_patches(self, diagnosis, source_files, fault_id, n=3):
        raise ProviderError("rule mode only diagnoses; use scripted, replay or live for draft_patches")


# --------------------------------------------------------------------------- scripted mode

class ScriptedProvider:
    mode = "scripted"

    def __init__(self, repo: Path = REPO):
        self.repo = Path(repo)

    def diagnose(self, incident, source_files):
        raise ProviderError("scripted mode only drafts patches; use rule, replay or live for diagnose")

    def draft_patches(self, diagnosis: Diagnosis, source_files: dict[str, str], fault_id: str,
                      n: int | None = 3) -> list[Patch]:
        """All of faults/<id>/candidates/*.diff in file-name order; n caps the count (None = all)."""
        cand_dir = self.repo / "faults" / fault_id / "candidates"
        files = sorted(cand_dir.glob("*.diff"))      # *.diff only: labels.json is never opened
        if not files:
            raise ProviderError(f"scripted mode: no candidates in {cand_dir}")
        if n is not None:
            files = files[:n]
        patches = []
        for f in files:
            diff = f.read_text(encoding="utf-8")
            digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:8]
            patches.append(Patch(candidate_id=f"{fault_id}-scripted-{digest}", diff=diff,
                                 explanation=f"scripted candidate {f.name}", provider_mode=self.mode,
                                 candidate_kind=f.stem))
        return check_patches(patches, fault_id, self.repo)


# --------------------------------------------------------------------------- live / replay

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "required": ["causes"],
    "properties": {
        "causes": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["rank", "file", "line", "reason", "evidence"],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1, "maximum": 3},
                    "file": {"type": "string", "minLength": 1},
                    "line": {"type": ["integer", "null"], "minimum": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string"},
                },
            },
        }
    },
}

PATCHES_SCHEMA = {
    "type": "object",
    "required": ["patches"],
    "properties": {
        "patches": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["diff", "explanation"],
                "properties": {"diff": {"type": "string", "minLength": 1}, "explanation": {"type": "string"}},
            },
        }
    },
}

DIAGNOSE_SYSTEM = (
    "You diagnose faults in order-service, a Java 17 Spring Boot service. "
    "Answer with JSON only, no prose and no code fences, in exactly this shape:\n"
    '{"causes": [{"rank": 1, "file": "<repository-relative path, e.g. app/src/main/java/...>", '
    '"line": <integer or null>, "reason": "<why this is the cause>", '
    '"evidence": "<incident detail or code that supports it>"}]}\n'
    "Give 1 to 3 causes, most likely first, with ranks 1..n."
)

PATCH_SYSTEM = (
    "You fix faults in order-service, a Java 17 Spring Boot service. "
    "Answer with JSON only, no prose and no code fences, in exactly this shape:\n"
    '{"patches": [{"diff": "<unified diff>", "explanation": "<one or two sentences>"}]}\n'
    "Each diff must be a git-style unified diff with paths relative to the repository root "
    "(--- a/app/... +++ b/app/...), apply cleanly to the files shown, and change as little as possible. "
    "Give exactly {n} distinct alternative patches."
)


def _sources_block(source_files: dict[str, str]) -> str:
    parts = []
    for path in sorted(source_files):
        _assert_allowed(path)
        parts.append(f"--- {path}\n{source_files[path]}")
    return "\n".join(parts)


def _parse_json(text: str):
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    return json.loads(stripped)


class LLMProvider:
    def __init__(self, mode: str, repo: Path = REPO, cache_dir: Path | None = None,
                 temperature: float = 0.2, env: dict | None = None):
        if mode not in ("live", "replay"):
            raise ValueError(f"LLMProvider mode must be live or replay, not {mode!r}")
        env = os.environ if env is None else env
        self.mode = mode
        self.repo = Path(repo)
        self.cache_dir = Path(cache_dir) if cache_dir else self.repo / "cache"
        self.temperature = temperature
        self.base_url = (env.get("LLM_BASE_URL") or "").rstrip("/")
        self.model = env.get("LLM_MODEL") or ""
        self._api_key = env.get("LLM_API_KEY") or ""
        missing = [k for k, v in (("LLM_BASE_URL", self.base_url), ("LLM_MODEL", self.model)) if not v]
        if missing:
            raise ProviderError(f"{mode} mode needs environment variable(s) {', '.join(missing)} "
                                f"(they also form the {'cache' if mode == 'replay' else 'request'} key)")

    # ---- prompts

    def diagnosis_messages(self, incident: dict, source_files: dict[str, str]) -> list[dict]:
        shown = {k: v for k, v in incident.items() if k != "fault_id"}
        user = ("Incident:\n" + json.dumps(shown, indent=2, sort_keys=True, default=str)
                + "\n\nSource files:\n" + _sources_block(source_files))
        return [{"role": "system", "content": DIAGNOSE_SYSTEM}, {"role": "user", "content": user}]

    def patch_messages(self, diagnosis: Diagnosis, source_files: dict[str, str], n: int) -> list[dict]:
        causes = [asdict(c) for c in diagnosis.causes]
        user = ("Diagnosis:\n" + json.dumps({"causes": causes}, indent=2, sort_keys=True)
                + "\n\nSource files:\n" + _sources_block(source_files))
        return [{"role": "system", "content": PATCH_SYSTEM.replace("{n}", str(n))},
                {"role": "user", "content": user}]

    # ---- cache

    def cache_key(self, messages: list[dict]) -> str:
        prompt = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        material = json.dumps([self.base_url, self.model, prompt, self.temperature], ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _fetch(self, messages: list[dict], key: str) -> str:
        if self.mode == "replay":
            path = self._cache_path(key)
            if not path.is_file():
                raise ProviderError(
                    f"replay cache miss: {path} not found (provider={self.base_url}, model={self.model}, "
                    f"temperature={self.temperature}). Record it first with mode=live.")
            return json.loads(path.read_text(encoding="utf-8"))["response"]

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"model": self.model, "messages": messages, "temperature": self.temperature}
        resp = requests.post(f"{self.base_url}/chat/completions", json=body, headers=headers,
                             timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entry = {"key": key, "provider": self.base_url, "model": self.model, "temperature": self.temperature,
                 "messages": messages, "response": text,
                 "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._cache_path(key).write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        return text

    # ---- bounded retry loop

    def _request_json(self, messages: list[dict], schema: dict, what: str, extra_check=None):
        attempts: list[dict] = []
        for attempt in range(1, MAX_RETRIES + 2):
            key = self.cache_key(messages)
            try:
                text = self._fetch(messages, key)
            except ProviderError:
                raise
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
                err = f"transport error: {type(e).__name__}: {e}"
                attempts.append({"attempt": attempt, "cache_key": key, "ok": False, "error": err})
                log.warning("%s attempt %d/%d failed: %s", what, attempt, MAX_RETRIES + 1, err)
                continue
            try:
                data = _parse_json(text)
                jsonschema.validate(data, schema)
                if extra_check:
                    extra_check(data)
            except (ValueError, jsonschema.ValidationError) as e:
                err = f"invalid response: {e.message if isinstance(e, jsonschema.ValidationError) else e}"
                attempts.append({"attempt": attempt, "cache_key": key, "ok": False, "error": err})
                log.warning("%s attempt %d/%d failed: %s", what, attempt, MAX_RETRIES + 1, err)
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That answer was invalid ({err}). "
                                                "Reply again with JSON only, in exactly the requested shape."}]
                continue
            attempts.append({"attempt": attempt, "cache_key": key, "ok": True, "error": None})
            return data, attempts
        summary = "; ".join(f"#{a['attempt']}: {a['error']}" for a in attempts)
        raise ProviderError(f"{what}: no valid response after {MAX_RETRIES + 1} attempts ({summary})")

    # ---- interface

    def diagnose(self, incident: dict, source_files: dict[str, str]) -> Diagnosis:
        def ranks_are_sequential(data):
            ranks = sorted(c["rank"] for c in data["causes"])
            if ranks != list(range(1, len(ranks) + 1)):
                raise ValueError(f"ranks must be 1..{len(ranks)}, got {ranks}")

        data, attempts = self._request_json(self.diagnosis_messages(incident, source_files),
                                            DIAGNOSIS_SCHEMA, "diagnose", ranks_are_sequential)
        causes = [Cause(**c) for c in sorted(data["causes"], key=lambda c: c["rank"])]
        return Diagnosis(causes=causes, provider_mode=self.mode, model=self.model, attempts=attempts)

    def draft_patches(self, diagnosis: Diagnosis, source_files: dict[str, str], fault_id: str,
                      n: int | None = 3) -> list[Patch]:
        n = 3 if n is None else n

        def at_most_n(data):
            if len(data["patches"]) > n:
                raise ValueError(f"asked for {n} patches, got {len(data['patches'])}")

        data, attempts = self._request_json(self.patch_messages(diagnosis, source_files, n),
                                            PATCHES_SCHEMA, "draft_patches", at_most_n)
        if len(data["patches"]) < n:
            log.warning("draft_patches: asked for %d patches, model returned %d", n, len(data["patches"]))
        patches = [Patch(candidate_id=f"{fault_id}-{self.mode}-{i}", diff=p["diff"], explanation=p["explanation"],
                         provider_mode=self.mode, model=self.model, attempts=attempts)
                   for i, p in enumerate(data["patches"], start=1)]
        return check_patches(patches, fault_id, self.repo)
