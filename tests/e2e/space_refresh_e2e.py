#!/usr/bin/env python3
"""Space refresh end-to-end test: a black-box test of CodeKB's space refresh.

Design: space-refresh-e2e-test-design.md (workspace root). In short, the program
builds a fixture repository it fully controls, indexes it in CodeKB, pushes
scripted commits, refreshes after each one, and checks the result three ways:
against a hand-written expected change, against a fresh index of the same
commit, and against invariants that hold after every refresh. Every check names
the requirement IDs (space-refresh-requirements.md) it gives evidence for, and
the report rates each requirement and gives a release verdict.

It talks to CodeKB only through the REST API, and to the fixture repository
only through git. Standard library plus the git CLI; no other dependencies.

The fixture repository is your own scratch GitHub repo: the test pushes and
deletes branches under codekb-e2e/ in it. Pass its https URL, with no
credentials in it, and a token that can write to it:

    export CODEKB_E2E_GITHUB_TOKEN=...   # fine-grained token, contents read/write on the fixture repo
    python3 tests/e2e/space_refresh_e2e.py --base-url http://127.0.0.1:8000 --fixture-repo https://github.com/<owner>/<repo>.git

Implemented so far: the harness, the fixture seed, and chain steps S0 and S1.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

import space_refresh_fixture as fx  # noqa: E402
from codekb_regression import (  # noqa: E402
    CheckError,
    Client,
    Console,
    McpClient,
    Skipped,
    build_sample_zip,
    raw_put,
    expect,
    expect_dict,
    expect_list,
    expect_status,
)

DEFAULT_FIXTURE_REPO = "https://github.com/raj-jain-dev/raj-jain-aws-asa-demo.git"
BRANCH_PREFIX = "codekb-e2e/"
TOKEN_ENV = "CODEKB_E2E_GITHUB_TOKEN"
TOKEN_FILE_ENV = "CODEKB_E2E_GITHUB_TOKEN_FILE"
TERMINAL_STATES = ("READY", "FAILED", "REFRESH_FAILED")
CALLABLE_TYPES = ("function", "method")

# ── Requirements catalogue ───────────────────────────────────────────────────
# ID -> priority, from space-refresh-requirements.md. Must | Should | Could.

REQUIREMENTS: dict[str, str] = {
    **{f"A{i}": "Must" for i in range(1, 6)}, "A6": "Could",
    **{f"B{i}": "Must" for i in range(1, 5)}, "B5": "Should",
    "C1": "Must", "C2": "Must", "C3": "Must", "C4": "Should",
    "C5": "Must", "C6": "Must", "C7": "Must", "C8": "Should",
    **{f"D{i}": "Must" for i in range(1, 5)},
    **{f"E{i}": "Must" for i in range(1, 9)}, "E9": "Should", "E10": "Must",
    "F1": "Must", "F2": "Should", "F3": "Must", "F4": "Must", "F5": "Should", "F6": "Could",
    **{f"G{i}": "Must" for i in range(1, 5)},
    **{f"H{i}": "Must" for i in range(1, 9)},
    "I1": "Must", "I2": "Must", "I3": "Must", "I4": "Must", "I5": "Should", "I6": "Must", "I7": "Should",
    **{f"J{i}": "Must" for i in range(1, 7)},
}

# Requirements this program does not cover, with the reason (design doc, section 9).
NOT_COVERED: dict[str, str] = {
    "A6": "no service-identity trigger exists to call",
    "D1": "policy and documentation requirement; needs a person's review",
    "D2": "out of scope: needs a Space made by an older release",
    "D4": "out of scope: backward compatibility",
    "E9": "out of scope: the inconsistencies come from older refresh code",
    "F2": "the fixture is far below the proposed 10,000 components",
    "H7": "needs one description to fail in the middle of a run",
    "I4": "needs a partial failure, such as a failed vector delete",
    "I5": "operator metrics are not part of the API",
    "J5": "the source snapshot can't be listed through the API",
}

# ── Results and report ───────────────────────────────────────────────────────


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    requirements: tuple[str, ...]
    advisory: bool
    duration: float
    detail: str = ""


class Recorder:
    """Runs checks, records PASS / FAIL / SKIP with requirement tags. Never raises."""

    def __init__(self, console: Console, redact: Callable[[str], str]) -> None:
        self.console = console
        self.redact = redact
        self.results: list[CheckResult] = []

    @contextmanager
    def check(self, name: str, *reqs: str, advisory: bool = False) -> Iterator[None]:
        reqs = tuple(dict.fromkeys(reqs))
        unknown = [r for r in reqs if r not in REQUIREMENTS]
        if unknown:
            raise ValueError(f"check {name!r} names unknown requirement(s) {unknown}")
        start = time.time()
        try:
            yield
        except Skipped as exc:
            self._record(name, "SKIP", reqs, advisory, time.time() - start, str(exc))
        except CheckError as exc:
            self._record(name, "FAIL", reqs, advisory, time.time() - start, str(exc))
        except Exception as exc:  # harness or server error: still a failed check
            self._record(
                name, "FAIL", reqs, advisory, time.time() - start,
                f"unexpected {type(exc).__name__}: {exc}",
            )
        else:
            self._record(name, "PASS", reqs, advisory, time.time() - start)

    def skip(self, name: str, reason: str, *reqs: str, advisory: bool = False) -> None:
        self._record(name, "SKIP", reqs, advisory, 0.0, reason)

    def passed(self, name: str) -> bool:
        return any(r.name == name and r.status == "PASS" for r in self.results)

    def _record(
        self, name: str, status: str, reqs: tuple[str, ...], advisory: bool, duration: float, detail: str = ""
    ) -> None:
        detail = self.redact(detail)
        self.results.append(CheckResult(name, status, tuple(reqs), advisory, duration, detail))
        c = self.console
        tag = {"PASS": c.green("PASS"), "FAIL": c.red("FAIL"), "SKIP": c.yellow("SKIP")}[status]
        timing = c.dim(f"{duration:7.1f}s") if duration >= 0.05 else c.dim("       -")
        label = f"{name}  {c.dim('[' + ', '.join(reqs) + ']')}" if reqs else name
        if advisory:
            label += c.dim("  (advisory)")
        print(f"  [{tag}] {timing}  {label}")
        if detail:
            for line in detail.splitlines()[:25]:
                print(f"            {c.dim(line) if status != 'FAIL' else c.red(line)}")


def rate_requirements(results: list[CheckResult]) -> dict[str, dict[str, Any]]:
    """Rate each requirement from its non-advisory checks (design doc TR9)."""
    ratings: dict[str, dict[str, Any]] = {}
    for req, priority in REQUIREMENTS.items():
        if req in NOT_COVERED:
            ratings[req] = {"priority": priority, "rating": "Not covered", "reason": NOT_COVERED[req]}
            continue
        mine = [r for r in results if req in r.requirements and not r.advisory]
        failed = [r.name for r in mine if r.status == "FAIL"]
        passed = [r.name for r in mine if r.status == "PASS"]
        if failed:
            rating, reason = "Not met", "; ".join(failed)
        elif passed:
            rating, reason = "Met", f"{len(passed)} check(s) passed"
        elif mine:
            rating, reason = "Not verified", "all checks were skipped"
        else:
            rating, reason = "Not verified", "no checks ran"
        ratings[req] = {"priority": priority, "rating": rating, "reason": reason}
    return ratings


def verdict(ratings: dict[str, dict[str, Any]], harness_failed: bool, exceptions: set[str]) -> tuple[str, list[str]]:
    """READY / INCOMPLETE / NOT READY over the covered Must requirements (TR10)."""
    musts = {k: v for k, v in ratings.items() if v["priority"] == "Must" and k not in exceptions}
    not_met = sorted(k for k, v in musts.items() if v["rating"] == "Not met")
    if not_met:
        return "NOT READY", not_met
    unverified = sorted(k for k, v in musts.items() if v["rating"] == "Not verified")
    if unverified or harness_failed:
        return "INCOMPLETE", unverified
    return "READY", []


# ── Git fixture repository ───────────────────────────────────────────────────


class GitError(Exception):
    pass


class FixtureRepo:
    """A local working copy of the fixture repository.

    Pushes only refs under ``codekb-e2e/``; refuses anything else. The token is
    sent as an HTTP extra header set through the environment of each git
    command, so it never appears in argv, ``.git/config`` or output. Credential
    helpers and askpass programs are disabled so no other credential is used.
    """

    def __init__(self, remote_url: str, token: str, workdir: Path) -> None:
        parsed = urllib.parse.urlparse(remote_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise GitError(f"fixture repo must be an https URL, got {remote_url!r}")
        if parsed.username or parsed.password:
            raise GitError("fixture repo URL must not embed credentials")
        self.remote_url = remote_url
        self.host = parsed.hostname
        self._token = token
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self._secrets = (token, basic)
        self._env = {
            k: v for k, v in os.environ.items()
            if k not in (TOKEN_ENV, "GIT_ASKPASS", "SSH_ASKPASS") and not k.startswith("VSCODE_GIT_")
        }
        self._env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": f"http.https://{self.host}/.extraheader",
            "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {basic}",
        })
        self.dir = workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "codekb-e2e-local")
        self._git("config", "user.name", "codekb-e2e")
        self._git("config", "user.email", "codekb-e2e@users.noreply.github.com")
        self._git("config", "core.quotePath", "false")
        self._git("remote", "add", "origin", remote_url)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            if s:
                text = text.replace(s, "<redacted>")
        return text

    def _git(self, *args: str, timeout: float = 120.0) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.dir, env=self._env, capture_output=True, text=True, timeout=timeout, check=False,
        )
        if proc.returncode != 0:
            raise GitError(self.redact(f"git {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:600]}"))
        return proc.stdout

    @staticmethod
    def _guard(branch: str) -> None:
        if not branch.startswith(BRANCH_PREFIX) or ".." in branch:
            raise GitError(f"refusing to touch ref {branch!r}: only {BRANCH_PREFIX}* is allowed")

    def remote_branches(self) -> dict[str, str]:
        """Branches under the prefix on the remote: name -> sha."""
        out = self._git("ls-remote", "--heads", "origin", f"refs/heads/{BRANCH_PREFIX}*", timeout=60)
        branches = {}
        for line in out.splitlines():
            sha, ref = line.split("\t", 1)
            branches[ref.removeprefix("refs/heads/")] = sha
        return branches

    def write_tree(self, tree: dict[str, str]) -> None:
        """Make the working tree exactly ``tree`` (deletes files not in it)."""
        for child in self.dir.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for rel, content in tree.items():
            path = self.dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def commit_orphan(self, message: str) -> str:
        """Commit the working tree as a new root commit with no parent."""
        self._git("checkout", "-q", "--orphan", f"e2e-{secrets.token_hex(4)}")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self.head()

    def commit(self, message: str) -> str:
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def checkout(self, sha: str) -> None:
        """Put the working copy on ``sha`` (detached), so the next commit builds on it."""
        self._git("checkout", "-q", "-f", "--detach", sha)

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def push(self, sha: str, branch: str, *, force: bool = False) -> None:
        self._guard(branch)
        spec = f"{'+' if force else ''}{sha}:refs/heads/{branch}"
        self._git("push", "-q", "origin", spec, timeout=180)

    def delete_remote_branch(self, branch: str, *, missing_ok: bool = False) -> None:
        self._guard(branch)
        try:
            self._git("push", "-q", "origin", f":refs/heads/{branch}", timeout=120)
        except GitError as exc:
            if not (missing_ok and "remote ref does not exist" in str(exc)):
                raise


# ── CodeKB API ───────────────────────────────────────────────────────────────


class ScanningClient(Client):
    """A Client that passes every response body to the leak scanner (J1)."""

    scanner: "LeakScanner | None" = None

    def request(self, method: str, path: str, **kw: Any):
        resp = super().request(method, path, **kw)
        if ScanningClient.scanner is not None:
            ScanningClient.scanner.scan(f"{method} {path}", resp.text)
        return resp



def q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class CodeKB:
    """The REST calls the test uses, on top of codekb_regression.Client."""

    def __init__(self, client: Client, console: Console, timeout: float) -> None:
        self.client = client
        self.console = console
        self.timeout = timeout

    def create_space(self, git_url: str, branch: str, name: str, *, summarize: bool = True) -> str:
        resp = self.client.post(
            "/api/spaces",
            json_body={"git_url": git_url, "branch": branch, "space_name": name, "summarize": summarize},
            timeout=120,
        )
        expect_status(resp, 201)
        payload = expect_dict(resp)
        expect(payload.get("space_id"), f"no space_id in create response: {resp.preview()}")
        return payload["space_id"]

    def status(self, space_id: str) -> dict:
        return expect_dict(expect_status(self.client.get(f"/api/spaces/{q(space_id)}/status", timeout=60), 200))

    def detail(self, space_id: str) -> dict:
        return expect_dict(expect_status(self.client.get(f"/api/spaces/{q(space_id)}", timeout=120), 200))

    def knowledge_base(self, space_id: str) -> dict:
        return expect_dict(
            expect_status(self.client.get(f"/api/spaces/{q(space_id)}/knowledge-base", timeout=180), 200)
        )

    def component(self, space_id: str, component_id: str) -> dict | None:
        resp = self.client.get(f"/api/spaces/{q(space_id)}/components/{q(component_id)}", timeout=120)
        if resp.status == 404:
            return None
        return expect_dict(expect_status(resp, 200))

    def search(self, space_id: str, query: str, limit: int = 50) -> list[dict]:
        resp = self.client.get(
            f"/api/spaces/{q(space_id)}/search", params={"q": query, "limit": limit}, timeout=180
        )
        return expect_list(expect_status(resp, 200))

    def refresh(self, space_id: str):
        return self.client.post(f"/api/spaces/{q(space_id)}/refresh", timeout=120)

    def delete(self, space_id: str) -> int:
        """Delete a Space. Returns 202 when deleted or already gone.

        With authorization on, a Space that no longer exists answers 401
        read_denied instead of 404 (the gate won't confirm whether it exists),
        so that also counts as gone.
        """
        resp = self.client.delete(f"/api/spaces/{q(space_id)}", timeout=900)
        if resp.status == 404 or (resp.status == 401 and "read_denied" in resp.text):
            return 202
        return resp.status

    def wait(self, space_id: str, label: str, timeout: float | None = None, interval: float = 5.0) -> dict:
        """Poll status until a terminal state. Returns the final status."""
        deadline = time.time() + (timeout or self.timeout)
        last = ""
        while True:
            status = self.status(space_id)
            state = status.get("status", "UNKNOWN")
            done, total = status.get("summarize_done"), status.get("summarize_total")
            progress = f" {done}/{total}" if done is not None and total else ""
            render = f"{state} step={status.get('current_step') or '-'}{progress}"
            if render != last:
                self.console.note(f"{label}: {render}")
                last = render
            if state in TERMINAL_STATES:
                return status
            if time.time() >= deadline:
                raise CheckError(f"{label} did not finish within {timeout or self.timeout:.0f}s (last: {render})")
            time.sleep(interval)


# ── Snapshots ────────────────────────────────────────────────────────────────

# Text the component route returns when a component has no vector: the catalog's
# heuristic ("A function that provides functionality") or, failing that, the
# component's own documentation. Anything else came from the vector index.
_HEURISTIC = re.compile(
    r"^A (function that|method that|class that represents|module containing|variable storing|"
    r"constant defining|component that) "
)


@dataclass
class Snapshot:
    """What the API shows of one Space at one moment."""

    space_id: str
    status: dict
    detail: dict
    kb: dict
    components: dict[str, dict]  # unprefixed id -> KB component
    descriptions: dict[str, str | None]  # callable id -> vector description, or None
    sources: dict[str, str | None]  # callable id -> source_code from the component route
    vector_hits: dict[str, dict]  # unprefixed id -> search hit metadata
    hit_ids_raw: list[str]  # every search hit key, for duplicate checks
    taken_at: float = field(default_factory=time.time)

    def strip(self, value: str) -> str:
        return value.removeprefix(f"{self.space_id}::")

    def callables(self) -> set[str]:
        return {i for i, c in self.components.items() if c.get("type") in CALLABLE_TYPES}

    def described(self) -> set[str]:
        return {i for i, d in self.descriptions.items() if d}

    def keys(self) -> dict[str, str]:
        """Component ID -> fixture key (``fx.component_key``): repo-relative path and the ID after its file segment."""
        source_dir = str(self.kb.get("source_directory") or "")
        return {cid: f"{_rel_path(comp.get('file_path') or '', self.space_id, source_dir)}#{cid.partition('.')[2]}"
                for cid, comp in self.components.items()}

    def callable_keys(self) -> set[str]:
        keys = self.keys()
        return {keys[i] for i in self.callables()}

    def id_of(self, key: str) -> str | None:
        return next((cid for cid, k in self.keys().items() if k == key), None)


def take_snapshot(api: CodeKB, space_id: str, queries: list[str]) -> Snapshot:
    status = api.status(space_id)
    detail = api.detail(space_id)
    kb = api.knowledge_base(space_id)
    prefix = f"{space_id}::"
    components: dict[str, dict] = {}
    for comp in kb.get("components", []):
        components[comp["id"].removeprefix(prefix)] = comp

    vector_hits: dict[str, dict] = {}
    hit_ids_raw: list[str] = []
    for query in queries:
        for hit in api.search(space_id, query, limit=50):
            key = str(hit.get("key", ""))
            hit_ids_raw.append(key)
            vector_hits[key.removeprefix(prefix)] = hit.get("metadata") or {}

    descriptions: dict[str, str | None] = {}
    sources: dict[str, str | None] = {}
    for cid, comp in components.items():
        if comp.get("type") not in CALLABLE_TYPES:
            continue
        got = api.component(space_id, comp["id"])
        if got is None:
            descriptions[cid], sources[cid] = None, None
            continue
        sources[cid] = got.get("source_code")
        text = ((got.get("business_behavior") or {}).get("description") or "").strip()
        doc = ((comp.get("attributes") or {}).get("documentation") or "").strip()
        from_vector = bool(text) and text != doc and not _HEURISTIC.match(text)
        if cid in vector_hits:  # search proves a vector exists
            descriptions[cid] = str(vector_hits[cid].get("description") or text) or None
        else:
            descriptions[cid] = text if from_vector else None
    return Snapshot(space_id, status, detail, kb, components, descriptions, sources, vector_hits, hit_ids_raw)


# ── Normalization and comparison ─────────────────────────────────────────────


def _rel_path(path: str, space_id: str, source_dir: str) -> str:
    """Repo-relative form of a stored component path."""
    p = str(path)
    if source_dir and p.startswith(source_dir.rstrip("/") + "/"):
        return p[len(source_dir.rstrip("/")) + 1:]
    for marker in (f"/{space_id}-refresh/", f"/{space_id}-analysis/", f"/{space_id}/"):
        if marker in p:
            return p.split(marker, 1)[1]
    return p.lstrip("./")


# Scalar statistics in the KB's analysis_metadata, compared separately (E7).
KB_STATS = ("total_components", "files_analyzed", "skipped_file_count", "total_lines")

COMPONENT_FIELDS = (
    "type", "file_path", "line_number", "end_line_number", "language", "parent_component", "content_hash", "attributes",
)


def normalize(snap: Snapshot) -> dict[str, Any]:
    sid = snap.space_id
    source_dir = str(snap.kb.get("source_directory") or "")
    comps: dict[str, dict] = {}
    for cid, comp in snap.components.items():
        row = {k: comp.get(k) for k in COMPONENT_FIELDS}
        row["file_path"] = _rel_path(row["file_path"] or "", sid, source_dir)
        if row["parent_component"]:
            row["parent_component"] = snap.strip(row["parent_component"])
        comps[cid] = row
    edges = set()
    for caller, callees in (snap.kb.get("call_graph") or {}).items():
        for entry in callees or []:
            callee, line = (entry[0], entry[1]) if isinstance(entry, (list, tuple)) else (entry, None)
            edges.add((snap.strip(caller), snap.strip(str(callee)), line))
    meta = snap.kb.get("analysis_metadata") or {}

    def norm_records(rows: Any) -> list:
        if isinstance(rows, dict):
            return sorted(f"{k}={v}" for k, v in rows.items())
        out = []
        for r in rows or []:
            if isinstance(r, dict):
                r = dict(r)
                if "file" in r:
                    r["file"] = _rel_path(str(r["file"]), sid, source_dir)
                out.append(json.dumps(r, sort_keys=True))
            else:
                out.append(_rel_path(str(r), sid, source_dir))
        return sorted(out)

    return {
        "components": comps,
        "edges": edges,
        "languages": sorted(meta.get("languages_detected") or []),
        "analysis_errors": norm_records(meta.get("analysis_errors")),
        "skipped_files": norm_records(meta.get("skipped_files")),
        "stats": {k: meta.get(k) for k in KB_STATS if isinstance(meta.get(k), (int, float))},
        "described": snap.described(),
    }


def diff_structure(a: dict, b: dict, label_a: str, label_b: str, limit: int = 20) -> list[str]:
    """Structural differences between two normalized snapshots (E1)."""
    out: list[str] = []
    ca, cb = a["components"], b["components"]
    only_a, only_b = sorted(set(ca) - set(cb)), sorted(set(cb) - set(ca))
    if only_a:
        out.append(f"only in {label_a}: {', '.join(only_a[:10])}" + (" …" if len(only_a) > 10 else ""))
    if only_b:
        out.append(f"only in {label_b}: {', '.join(only_b[:10])}" + (" …" if len(only_b) > 10 else ""))
    for cid in sorted(set(ca) & set(cb)):
        for f in COMPONENT_FIELDS:
            if ca[cid].get(f) != cb[cid].get(f):
                va, vb = json.dumps(ca[cid].get(f), sort_keys=True), json.dumps(cb[cid].get(f), sort_keys=True)
                out.append(f"{cid}.{f}: {label_a}={va[:120]} {label_b}={vb[:120]}")
    ea, eb = a["edges"], b["edges"]
    for e in sorted(ea - eb, key=str)[:8]:
        out.append(f"edge only in {label_a}: {e[0]} -> {e[1]} (line {e[2]})")
    for e in sorted(eb - ea, key=str)[:8]:
        out.append(f"edge only in {label_b}: {e[0]} -> {e[1]} (line {e[2]})")
    for key in ("languages", "analysis_errors", "skipped_files"):
        if a[key] != b[key]:
            out.append(f"{key}: {label_a}={str(a[key])[:200]} {label_b}={str(b[key])[:200]}")
    if len(out) > limit:
        out = out[:limit] + [f"… and {len(out) - limit} more difference(s)"]
    return out


def diff_sets(a: set[str], b: set[str], label_a: str, label_b: str, limit: int = 12) -> list[str]:
    out = []
    if a - b:
        out.append(f"only in {label_a}: {', '.join(sorted(a - b)[:limit])}" + (" …" if len(a - b) > limit else ""))
    if b - a:
        out.append(f"only in {label_b}: {', '.join(sorted(b - a)[:limit])}" + (" …" if len(b - a) > limit else ""))
    return out


def diff_coverage(a: dict, b: dict, label_a: str, label_b: str) -> list[str]:
    """Which components have descriptions (not the text)."""
    out = []
    missing = sorted(b["described"] - a["described"])
    extra = sorted(a["described"] - b["described"])
    if missing:
        out.append(f"described in {label_b} but not in {label_a}: {', '.join(missing[:12])}")
    if extra:
        out.append(f"described in {label_a} but not in {label_b}: {', '.join(extra[:12])}")
    return out


# ── Invariants ───────────────────────────────────────────────────────────────


def expected_source(tree: dict[str, str], rel_path: str, start: int, end: int) -> str | None:
    content = tree.get(rel_path)
    if content is None:
        return None
    lines = content.splitlines()
    return "\n".join(lines[max(start - 1, 0):min(end, len(lines))])


def check_invariants(rec: Recorder, label: str, snap: Snapshot, tree: dict[str, str], reqs: dict[str, tuple[str, ...]]) -> None:
    """Rules that hold for every Space at every commit. ``reqs`` maps rule -> requirement IDs."""
    with rec.check(f"{label}: no component ID appears twice in the KB or in one search", *reqs["unique"]):
        raw = [c["id"] for c in snap.kb.get("components", [])]
        dupes = sorted({i for i in raw if raw.count(i) > 1})
        expect(not dupes, f"duplicate KB IDs: {', '.join(dupes[:10])}")

    with rec.check(f"{label}: every search hit names a component in the KB", *reqs["hits"]):
        orphans = sorted(set(snap.vector_hits) - set(snap.components))
        expect(not orphans, f"{len(orphans)} vector(s) for components not in the KB: {', '.join(orphans[:10])}")

    with rec.check(f"{label}: every function's source matches the file at this commit", *reqs["source"]):
        source_dir = str(snap.kb.get("source_directory") or "")
        bad = []
        for cid in sorted(snap.callables()):
            comp = snap.components[cid]
            rel = _rel_path(comp.get("file_path") or "", snap.space_id, source_dir)
            want = expected_source(tree, rel, int(comp.get("line_number") or 0), int(comp.get("end_line_number") or 0))
            got = snap.sources.get(cid)
            if want is None:
                bad.append(f"{cid}: file {rel!r} is not in the commit")
            elif got is None:
                bad.append(f"{cid}: no source returned")
            elif got.strip() != want.strip():
                bad.append(f"{cid}: source differs from {rel} lines {comp.get('line_number')}-{comp.get('end_line_number')}")
        expect(not bad, f"{len(bad)} component(s):\n" + "\n".join(bad[:12]))

    with rec.check(f"{label}: counts match the Space's contents", *reqs["counts"]):
        problems = []
        cc = snap.detail.get("component_count")
        if cc != len(snap.components):
            problems.append(f"component_count={cc}, KB holds {len(snap.components)}")
        vc = snap.detail.get("vector_count")
        nd = len(snap.described())
        if vc != nd:
            problems.append(f"vector_count={vc}, {nd} component(s) have a description")
        expect(not problems, "; ".join(problems))


def check_mcp_source(run: "Run", label: str, sid: str, snap: Snapshot, reqs: tuple[str, ...]) -> None:
    """The MCP call graph with source, which agents use, must read source (E6)."""
    probe = sorted(snap.callables())[0] if snap.callables() else None
    with run.rec.check(f"{label}: MCP call graph with source reads the source", *reqs):
        if probe is None:
            raise Skipped("no functions in the Space")
        text = run.mcp.call_tool("get_call_graph_with_source", {"component_id": f"{sid}::{probe}", "depth": 2})
        run.scanner.scan("MCP call graph", text)
        m = re.search(r"Error reading source[^\n]*|Source not available[^\n]*", text)
        expect(not m, f"{probe}: {m.group(0)[:200] if m else ''}")


def chain_probe(snap: Snapshot) -> str:
    """ID of the first Python chain hop, which every commit keeps; any function if it is missing."""
    py = fx.LANGUAGES[0]
    key = fx.component_key(py, fx.path_of(py, "ledger", "chain"), fx.hop_name(py, 0))
    return snap.id_of(key) or min(snap.callables(), default="")


# ── Run context ──────────────────────────────────────────────────────────────


@dataclass
class Run:
    args: argparse.Namespace
    console: Console
    rec: Recorder
    api: CodeKB
    repo: FixtureRepo | None
    run_id: str
    queries: list[str]
    spaces: list[str] = field(default_factory=list)
    deleted: set[str] = field(default_factory=set)
    branches: list[str] = field(default_factory=list)
    harness_failed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    groups: set[str] = field(default_factory=set)
    mcp: Any = None
    scanner: Any = None
    companion_id: str | None = None  # a second Space, for the hyperspace (G4) and overviews (K3, I7)
    companion_sha: str | None = None
    hyperspace_id: str | None = None
    cleanup_credentials: list[str] = field(default_factory=list)  # identities whose stored credential must go

    @property
    def max_file_bytes(self) -> int:
        return int(self.args.max_file_size_mb * 1024 * 1024)

    @property
    def hops(self) -> int:
        """Caller hops a change reaches: the prompt window minus the function itself."""
        return self.args.call_graph_depth - 1

    def branch(self, suffix: str) -> str:
        return f"{BRANCH_PREFIX}{self.run_id}/{suffix}"

    def push_branch(self, sha: str, suffix: str, *, force: bool = False) -> str:
        name = self.branch(suffix)
        assert self.repo is not None
        self.repo.push(sha, name, force=force)
        if name not in self.branches:
            self.branches.append(name)
        return name

    def create_space(self, branch: str, label: str) -> str:
        sid = self.api.create_space(self.args.fixture_repo, branch, f"e2e-{self.run_id}-{label}")
        self.spaces.append(sid)
        self.console.note(f"{label} space: {sid} (branch {branch})")
        return sid

    def delete_space(self, sid: str) -> None:
        """Delete a Space the run no longer needs; teardown skips it afterwards."""
        if self.args.keep:
            return
        status = self.api.delete(sid)
        if status == 202:
            self.deleted.add(sid)
        else:
            self.console.warn(f"delete {sid} returned {status}; teardown will retry")


# ── Preflight ────────────────────────────────────────────────────────────────


def preflight(run: Run) -> bool:
    c, rec, client = run.console, run.rec, run.api.client
    c.heading("Preflight")
    ok = True

    with rec.check("backend is healthy"):
        expect_status(client.get("/api/health", timeout=30), 200)
    if not rec.passed("backend is healthy"):
        return False

    with rec.check("fixture host is allowed and infrastructure is ready"):
        settings = expect_dict(expect_status(client.get("/api/config", timeout=60), 200))
        host = urllib.parse.urlparse(run.args.fixture_repo).hostname or ""
        hosts = settings.get("allowed_git_hosts") or []
        expect(any(host == h or host.endswith("." + h) for h in hosts), f"{host} not in allowed_git_hosts {hosts}")
        expect((settings.get("infrastructure") or {}).get("ready"), f"infrastructure not ready: {settings.get('infrastructure')}")
    ok &= rec.passed("fixture host is allowed and infrastructure is ready")

    with rec.check("identity has a working GitHub credential in CodeKB"):
        rows = expect_list(expect_status(client.get("/api/user-profile/gitcreds", timeout=60), 200))
        expect(any(r.get("provider") == "github" for r in rows),
               "no GitHub credential stored for this identity; save one on the Git credentials page, "
               "or pass --identity for the identity that has one")
        test = expect_dict(expect_status(client.post("/api/user-profile/gitcreds/github/test", timeout=60), 200))
        expect(test.get("status") == "ok", f"stored GitHub credential failed its test: {test}")
    ok &= rec.passed("identity has a working GitHub credential in CodeKB")

    with rec.check("push token can reach the fixture repository"):
        if run.repo is None:
            raise CheckError(f"no push token: export {TOKEN_ENV}, or pass --token-file / set {TOKEN_FILE_ENV}")
        run.repo.remote_branches()
    ok &= rec.passed("push token can reach the fixture repository")

    if not ok:
        run.harness_failed = True
    return ok


# ── Group C: change chain ────────────────────────────────────────────────────

INVARIANT_REQS_BASELINE = {"unique": (), "hits": (), "source": (), "counts": ()}
INVARIANT_REQS_REFRESH = {
    "unique": ("E3",), "hits": ("E2", "E6"), "source": ("E6",), "counts": ("E7",),
}


@dataclass
class ChainState:
    """The chain Space and the commit its branch points at."""

    space_id: str | None = None
    state: fx.State = field(default_factory=fx.State)
    tree: dict[str, str] = field(default_factory=dict)
    sha: str | None = None
    snapshot: Snapshot | None = None  # the chain Space after the last step
    last_fresh: Snapshot | None = None  # the fresh index of the last step's commit
    history: dict[str, tuple[str, dict[str, str], fx.State]] = field(default_factory=dict)  # step -> commit
    creation_gaps: set[str] = field(default_factory=set)  # undescribed after creation


def step_s0(run: Run, chain: ChainState) -> None:
    """Seed the chain Space and a second fresh index of the same commit."""
    c, rec, api = run.console, run.rec, run.api
    c.heading("S0  seed indexed")
    assert run.repo is not None

    with rec.check("S0: push the seed commit"):
        chain.state = fx.State()
        chain.tree = fx.build_tree(chain.state)
        run.repo.write_tree(chain.tree)
        chain.sha = run.repo.commit_orphan(f"codekb-e2e {run.run_id}: seed")
        run.push_branch(chain.sha, "chain")
        run.push_branch(chain.sha, "step-00")
        chain.history["S0"] = (chain.sha, chain.tree, chain.state)
        c.note(f"seed commit {chain.sha[:10]}")
    if not rec.passed("S0: push the seed commit"):
        run.harness_failed = True
        return

    fresh_id: str | None = None
    with rec.check("S0: chain Space and a fresh Space reach READY"):
        chain.space_id = run.create_space(run.branch("chain"), "chain")
        fresh_id = run.create_space(run.branch("step-00"), "fresh-00")
        for sid, label in ((chain.space_id, "chain"), (fresh_id, "fresh-00")):
            st = api.wait(sid, f"S0 {label}")
            expect(st.get("status") == "READY", f"{label} ended {st.get('status')}: {st.get('error')}")
            expect(st.get("git_commit_hash") == chain.sha,
                   f"{label} indexed {st.get('git_commit_hash')}, pushed {chain.sha}")
    if not rec.passed("S0: chain Space and a fresh Space reach READY"):
        run.harness_failed = True
        return

    snap = fresh = None
    with rec.check("S0: snapshot both Spaces"):
        snap = take_snapshot(api, chain.space_id, run.queries)
        fresh = take_snapshot(api, fresh_id, run.queries)
        c.note(f"chain: {len(snap.components)} components, {len(snap.callables())} callables, "
               f"{len(snap.described())} described, {len(snap.vector_hits)} vectors found by search")
    if snap is None or fresh is None:
        run.harness_failed = True
        return
    chain.snapshot = snap
    chain.last_fresh = fresh

    with rec.check("S0: the fixture model matches the fresh index (fixture check)"):
        model = fx.expected_callables(chain.state, max_file_bytes=run.max_file_bytes)
        expect(model == fresh.callable_keys(),
               "fixture error: " + "; ".join(diff_sets(model, fresh.callable_keys(), "model", "fresh")))

    with rec.check("S0: fixture call edges are resolved by the parser"):
        keys = snap.keys()
        edges = {(keys.get(a, a), keys.get(b, b)) for a, b, _ in normalize(snap)["edges"]}
        missing = [e for e in fx.expected_seed_edges() if e not in edges]
        expect(not missing, "fixture error: CodeKB does not resolve these call edges, so expectations that "
               "rely on them are unreliable:\n" + "\n".join(f"{a} -> {b}" for a, b in missing[:12]))

    with rec.check("S0: two indexes of the seed have identical structure (oracle check)"):
        diffs = diff_structure(normalize(snap), normalize(fresh), "chain", "fresh")
        expect(not diffs, "the fresh-index comparison is not deterministic, so it can't be used as an "
               "oracle:\n" + "\n".join(diffs))

    oracle_checks = {
        "S0: push the seed commit",
        "S0: the fixture model matches the fresh index (fixture check)",
        "S0: fixture call edges are resolved by the parser",
        "S0: two indexes of the seed have identical structure (oracle check)",
    }

    # Creation, not refresh: a gap here is a product finding outside this
    # program's requirements. Later steps still hold the refreshed Space to
    # E4, so a gap that refresh never fills fails there, with this as context.
    for label, s in (("chain", snap), ("fresh", fresh)):
        with rec.check(f"S0: creation described every function and method ({label} Space)"):
            undescribed = sorted(s.callables() - s.described())
            chain.creation_gaps |= set(undescribed) if label == "chain" else set()
            expect(not undescribed,
                   f"product finding (creation, not refresh): {len(undescribed)} function(s) left without a "
                   f"description although the Space ended READY: {', '.join(undescribed[:12])}")

    check_invariants(rec, "S0", snap, chain.tree, INVARIANT_REQS_BASELINE)
    check_mcp_source(run, "S0", chain.space_id, snap, ())
    for r in rec.results:
        if r.status == "FAIL" and (r.name in oracle_checks or r.name.startswith("S0: no component")
                                   or r.name.startswith("S0: every search hit") or r.name.startswith("S0: every function's source")
                                   or r.name.startswith("S0: counts match")):
            run.harness_failed = True

    if run.groups & {"G", "K"}:
        run.companion_id, run.companion_sha = fresh_id, chain.sha
        if "G" in run.groups:
            with rec.check("S0: create a hyperspace with the chain Space and a second Space"):
                resp = expect_status(api.client.post("/api/hyperspaces", json_body={
                    "name": f"e2e-{run.run_id}", "space_ids": [chain.space_id, fresh_id]}, timeout=120), 201)
                body = expect_dict(resp)
                run.hyperspace_id = body.get("hyperspace_id") or body.get("id")
                expect(run.hyperspace_id, f"no hyperspace id in {resp.preview()}")
    else:
        run.delete_space(fresh_id)


def step_s1(run: Run, chain: ChainState) -> None:
    """Refresh with no new commit: nothing changes, nothing is spent (C6, F1)."""
    c, rec, api = run.console, run.rec, run.api
    c.heading("S1  no new commit")
    if chain.snapshot is None or chain.space_id is None:
        rec.skip("S1: refresh with no new commit", "S0 did not complete", "C6", "F1")
        return
    before = chain.snapshot
    runs_before = len(before.status.get("refresh_pipeline_runs") or [])
    tokens_before = ((before.status.get("token_usage") or {}).get("space_refresh") or {})

    started = time.time()
    accepted = False
    with rec.check("S1: refresh is accepted with 202 SUBMITTED", "A1", "A3"):
        resp = api.refresh(chain.space_id)
        expect_status(resp, 202)
        body = expect_dict(resp)
        expect(body.get("status") == "SUBMITTED", f"expected SUBMITTED, got {body.get('status')!r}")
        accepted = True
    if not accepted:
        return

    final: dict = {}
    with rec.check("S1: refresh ends READY within 120 s", "C6"):
        final = api.wait(chain.space_id, "S1 refresh", timeout=600.0, interval=2.0)
        elapsed = time.time() - started
        c.note(f"finished in {elapsed:.1f}s")
        expect(final.get("status") == "READY", f"ended {final.get('status')}: {final.get('error')}")
        expect(elapsed <= 120, f"took {elapsed:.0f}s")
    if final.get("status") != "READY":
        return

    with rec.check("S1: the run is recorded and reports nothing to do", "C6", "I2"):
        runs = final.get("refresh_pipeline_runs") or []
        expect(len(runs) == runs_before + 1, f"refresh_pipeline_runs went from {runs_before} to {len(runs)}")
        steps = [s.get("step") for s in (runs[-1].get("steps") or [])]
        expect("unchanged" in steps, f"latest run's steps are {steps}; expected an 'unchanged' step")

    with rec.check("S1: the recorded commit is unchanged", "I3", "C6"):
        expect(final.get("git_commit_hash") == chain.sha,
               f"git_commit_hash is {final.get('git_commit_hash')}, expected {chain.sha}")

    after = take_snapshot(api, chain.space_id, run.queries)

    with rec.check("S1: the index is unchanged", "C6", "E10"):
        diffs = diff_structure(normalize(before), normalize(after), "before", "after")
        expect(not diffs, "\n".join(diffs))

    with rec.check("S1: every description is unchanged", "E10", "F1"):
        changed = sorted(i for i in before.callables() | after.callables()
                         if before.descriptions.get(i) != after.descriptions.get(i))
        expect(not changed, f"{len(changed)} description(s) changed: {', '.join(changed[:12])}")

    with rec.check("S1: no AI tokens were recorded", "F1", "C6"):
        tokens_after = ((after.status.get("token_usage") or {}).get("space_refresh") or {})
        expect(tokens_after == tokens_before,
               f"token_usage.space_refresh changed: before={json.dumps(tokens_before)[:200]} "
               f"after={json.dumps(tokens_after)[:200]}")

    with rec.check("S1: counts are unchanged", "E7"):
        for key in ("component_count", "vector_count"):
            expect(before.detail.get(key) == after.detail.get(key),
                   f"{key} went from {before.detail.get(key)} to {after.detail.get(key)}")

    check_invariants(rec, "S1", after, chain.tree, INVARIANT_REQS_REFRESH)
    chain.snapshot = after


# ── Chain steps S2 onward ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChainStep:
    """One commit on the chain branch and the checks that follow its refresh.

    ``change`` turns the current fixture state into the next one. ``mode`` is
    ``commit`` (a normal commit on the chain), ``orphan`` (a rewritten history,
    force-pushed) or ``reset`` (force-push the branch back to an earlier step's
    commit). ``reqs`` tag the step-specific checks. ``extra`` adds checks that
    only this step makes. ``searches`` are advisory relevance probes: a query
    and the function it should find in the top five.
    """

    name: str
    title: str
    change: Callable[[fx.State, argparse.Namespace], fx.State] | None = None
    reqs: tuple[str, ...] = ()
    mode: str = "commit"
    reset_to: str | None = None
    extra: Callable[["StepContext"], None] | None = None
    searches: Callable[[], list[tuple[str, str]]] | None = None


@dataclass
class StepContext:
    run: Run
    chain: ChainState
    step: ChainStep
    sha: str
    tree: dict[str, str]
    state: fx.State
    before: Snapshot
    after: Snapshot | None
    fresh: Snapshot | None
    final: dict
    token_entry: dict | None


def last_refresh_token_entry(status: dict) -> dict | None:
    """The most recent refresh's token record (orchestrator: token_usage.space_refresh.last_generation)."""
    return ((status.get("token_usage") or {}).get("space_refresh") or {}).get("last_generation")


def expected_redescriptions(before: Snapshot, fresh: Snapshot, hops: int) -> tuple[set[str], set[str], set[str]]:
    """What a refresh from ``before`` to ``fresh``'s commit must re-describe (design doc 5.6).

    Changed: functions and methods new at the commit, or whose content hash
    differs (a missing prior hash counts as changed). Removed: present before,
    gone now. Stale callers: reached by walking callers from Changed and
    Removed for up to ``hops`` hops, in the old **or** the new call graph, and
    still present. The truth for "now" is the fresh index, not the refreshed
    Space, so a refresh that picked the wrong component can't hide it.
    """
    p_calls, f_calls = before.callables(), fresh.callables()
    changed = set()
    for cid in f_calls:
        old = before.components.get(cid)
        if old is None or cid not in p_calls:
            changed.add(cid)
            continue
        h_old, h_new = old.get("content_hash"), fresh.components[cid].get("content_hash")
        if not h_old or h_old != h_new:
            changed.add(cid)
    removed = p_calls - f_calls
    callers_of: dict[str, set[str]] = {}
    for snap in (before, fresh):
        for caller, callee, _ in normalize(snap)["edges"]:
            callers_of.setdefault(callee, set()).add(caller)
    frontier, seen, stale = set(changed | removed), set(changed | removed), set()
    for _ in range(max(hops, 0)):
        nxt = set()
        for cid in frontier:
            for caller in callers_of.get(cid, ()):
                if caller not in seen:
                    seen.add(caller)
                    nxt.add(caller)
        stale |= nxt
        frontier = nxt
    stale = (stale & f_calls) - changed
    return changed, removed, stale


def _short(ids: set[str] | list[str], limit: int = 12) -> str:
    ids = sorted(ids)
    return ", ".join(ids[:limit]) + (f" … (+{len(ids) - limit})" if len(ids) > limit else "")


def run_chain_step(run: Run, chain: ChainState, step: ChainStep) -> None:
    c, rec, api = run.console, run.rec, run.api
    n = step.name
    c.heading(f"{n}  {step.title}")
    if chain.snapshot is None or chain.space_id is None or run.repo is None:
        rec.skip(f"{n}: {step.title}", "the chain Space is not available", *step.reqs)
        return
    before = chain.snapshot
    prior_tree = chain.tree
    prior_sha = before.status.get("git_commit_hash")
    prior_marker = chain.state.marker

    sha = tree = state = None
    with rec.check(f"{n}: push the commit"):
        if step.mode == "reset":
            if step.reset_to not in chain.history:
                raise Skipped(f"{step.reset_to} did not run, so there is no commit to reset to")
            sha, tree, state = chain.history[step.reset_to]
            run.push_branch(sha, "chain", force=True)
            run.repo.checkout(sha)
        else:
            assert step.change is not None
            state = step.change(chain.state, run.args).with_(marker=n)
            tree = fx.build_tree(state)
            if step.mode in ("commit", "two_commits"):
                run.repo.checkout(chain.sha)
            if step.mode == "two_commits":
                run.repo.write_tree(fx.build_tree(chain.state.with_(readme_rev=chain.state.readme_rev + 1, marker=f"{n}-1")))
                run.repo.commit(f"codekb-e2e {run.run_id}: {n} first of two commits")
            run.repo.write_tree(tree)
            message = f"codekb-e2e {run.run_id}: {n} {step.title}"
            sha = run.repo.commit_orphan(message) if step.mode == "orphan" else run.repo.commit(message)
            run.push_branch(sha, "chain", force=step.mode == "orphan")
        run.push_branch(sha, f"step-{n.lower()}")
        c.note(f"commit {sha[:10]}" + (f" (= {step.reset_to})" if step.mode == "reset" else ""))
    if sha is None:
        run.harness_failed = True
        return
    chain.sha, chain.tree, chain.state = sha, tree, state
    chain.history[n] = (sha, tree, state)

    fresh_id: str | None = None
    with rec.check(f"{n}: start a fresh index of the same commit"):
        fresh_id = run.create_space(run.branch(f"step-{n.lower()}"), f"fresh-{n.lower()}")

    runs_before = len(before.status.get("refresh_pipeline_runs") or [])
    final: dict = {}
    old_ids = before.callables()
    reader = None
    if "G" in run.groups:
        words = {qq.split()[0] for qq in run.queries}
        new_keys = fx.expected_callables(state, max_file_bytes=run.max_file_bytes)
        touched = {w for w in words if any(w in k.lower() for k in before.callable_keys() ^ new_keys)}
        queries = [qq for qq in run.queries if qq.split()[0] in touched][:4] or run.queries[:2]
        reader = RefreshReader(run, chain.space_id, queries, chain_probe(before), before.id_of(fx.MARKER_KEY), chat=True)
        reader.__enter__()
    try:
        with rec.check(f"{n}: refresh is accepted with 202 SUBMITTED", "A1", "A3"):
            resp = api.refresh(chain.space_id)
            expect_status(resp, 202)
            expect(expect_dict(resp).get("status") == "SUBMITTED", f"unexpected body: {resp.preview()}")
        if rec.passed(f"{n}: refresh is accepted with 202 SUBMITTED"):
            with rec.check(f"{n}: refresh ends READY at the pushed commit", "A3", "I3"):
                final = api.wait(chain.space_id, f"{n} refresh", interval=3.0)
                expect(final.get("status") == "READY", f"ended {final.get('status')}: {final.get('error')}")
                expect(final.get("git_commit_hash") == sha, f"git_commit_hash is {final.get('git_commit_hash')}, pushed {sha}")
        if reader:
            time.sleep(3)  # one or two samples after the switch
    finally:
        if reader:
            reader.__exit__(None, None, None)

    fresh_ok = False
    if fresh_id:
        with rec.check(f"{n}: the fresh index reaches READY (oracle)"):
            fst = api.wait(fresh_id, f"{n} fresh")
            expect(fst.get("status") == "READY", f"fresh index ended {fst.get('status')}: {fst.get('error')}")
            fresh_ok = True
        if not fresh_ok:
            run.harness_failed = True

    after = fresh = None
    with rec.check(f"{n}: snapshot the chain Space and the fresh index"):
        after = take_snapshot(api, chain.space_id, run.queries)
        if fresh_ok:
            fresh = take_snapshot(api, fresh_id, run.queries)
        c.note(f"chain: {len(after.components)} components, {len(after.callables())} callables, "
               f"{len(after.described())} described")
    if reader:
        # The new commit's IDs, from the fresh index of it when there is one.
        new_ids = fresh.callables() if fresh is not None else after.callables() if after is not None else old_ids
        evaluate_reader(rec, n, reader, old_ids=old_ids, new_ids=new_ids, old_marker=prior_marker,
                        new_marker=state.marker, prior_sha=prior_sha, refreshed=final.get("status") == "READY")
    if after is None:
        run.harness_failed = True
        return
    chain.snapshot = after
    if fresh is not None:
        chain.last_fresh = fresh

    ready = final.get("status") == "READY"
    token_entry = last_refresh_token_entry(after.status)
    ctx = StepContext(run, chain, step, sha, tree, state, before, after, fresh, final, token_entry)

    if not ready:
        # A failed refresh must leave the Space at its prior commit (H1), so the
        # source it serves is checked against the prior tree, not the new one.
        for what in ("index and descriptions", "re-description scope"):
            rec.skip(f"{n}: {what} checks", "the refresh did not end READY")
        with rec.check(f"{n}: after the failed refresh the index is unchanged", "H1"):
            diffs = diff_structure(normalize(before), normalize(after), "before", "after")
            expect(not diffs, "\n".join(diffs))
        check_invariants(rec, n, after, prior_tree, INVARIANT_REQS_REFRESH)
        if step.extra:
            step.extra(ctx)
        if fresh_id:
            run.delete_space(fresh_id)
        return

    with rec.check(f"{n}: one run is recorded, and the token entry names this commit", "I2"):
        runs = after.status.get("refresh_pipeline_runs") or []
        expect(len(runs) == runs_before + 1, f"refresh_pipeline_runs went from {runs_before} to {len(runs)}")
        expect(token_entry is not None, "no token_usage.space_refresh.last_generation entry")
        expect(token_entry.get("git_commit_hash") == sha,
               f"latest token entry names {token_entry.get('git_commit_hash')}, not {sha}")

    model = fx.expected_callables(state, max_file_bytes=run.max_file_bytes)
    if fresh is not None:
        with rec.check(f"{n}: the fixture model matches the fresh index (fixture check)"):
            if model != fresh.callable_keys():
                run.meta.setdefault("fixture_errors", []).append(n)
            expect(model == fresh.callable_keys(),
                   "fixture error: " + "; ".join(diff_sets(model, fresh.callable_keys(), "model", "fresh")))

    detect_reqs = [r for r in step.reqs if r in ("C1", "C2", "C7", "E3")]
    with rec.check(f"{n}: the refreshed functions and methods match the fixture", "C1", *detect_reqs):
        expect(after.callable_keys() == model, "; ".join(diff_sets(after.callable_keys(), model, "refreshed", "fixture")))

    if fresh is None:
        rec.skip(f"{n}: comparison with a fresh index", "the fresh index is not available", "E1", "E4", "E5", "E10", "F1")
    else:
        na, nf = normalize(after), normalize(fresh)
        index_reqs = [r for r in step.reqs if r in ("H3", "C3", "E3")]
        with rec.check(f"{n}: the index matches a fresh index of the same commit", "E1", *index_reqs):
            diffs = diff_structure(na, nf, "refreshed", "fresh")
            expect(not diffs, "\n".join(diffs))

        with rec.check(f"{n}: the statistics shown for the Space match a fresh index", "E7"):
            stat_diffs = [f"KB {k}: refreshed={na['stats'].get(k)} fresh={nf['stats'].get(k)}"
                          for k in sorted(set(na["stats"]) | set(nf["stats"])) if na["stats"].get(k) != nf["stats"].get(k)]
            for where, a, b in (("status.statistics", after.status.get("statistics"), fresh.status.get("statistics")),
                                ("detail.statistics", after.detail.get("statistics"), fresh.detail.get("statistics"))):
                a, b = a or {}, b or {}
                for k in sorted(set(a) | set(b)):
                    if isinstance(a.get(k), (int, float)) or isinstance(b.get(k), (int, float)):
                        if a.get(k) != b.get(k):
                            stat_diffs.append(f"{where}.{k}: refreshed={a.get(k)} fresh={b.get(k)}")
            expect(not stat_diffs, "; ".join(stat_diffs[:12]))

        with rec.check(f"{n}: every function the fresh index describes is described", "E4"):
            missing = fresh.described() - after.described()
            carried = missing & chain.creation_gaps
            expect(not missing, f"{len(missing)} without a description: {_short(missing)}"
                   + (f" ({len(carried)} of them were already missing after creation and never retried)" if carried else ""))

        removed_now = before.callables() - fresh.callables()
        with rec.check(f"{n}: removed functions are gone from the index and from search", "E2",
                       *[r for r in step.reqs if r in ("C2", "C7")]):
            still = {i for i in removed_now if i in after.components or i in after.vector_hits}
            expect(not still, f"still present after removal: {_short(still)}")

        changed, removed, stale = expected_redescriptions(before, fresh, run.hops)
        expected = changed | stale
        c.note(f"expected re-descriptions: {len(changed)} changed, {len(stale)} dependent callers, {len(removed)} removed")
        with rec.check(f"{n}: every changed function has a new description", "E4"):
            bad = [i for i in changed if not after.descriptions.get(i) or after.descriptions.get(i) == before.descriptions.get(i)]
            expect(not bad, f"{len(bad)} changed function(s) kept the old text or have none: {_short(bad)}")

        with rec.check(f"{n}: unaffected functions keep their exact description", "E10", "F1"):
            moved = [i for i in (after.callables() & before.callables()) - expected
                     if before.descriptions.get(i) and after.descriptions.get(i) != before.descriptions.get(i)]
            expect(not moved, f"{len(moved)} description(s) changed although nothing they depend on changed: {_short(moved)}")

        reprocessed = (token_entry or {}).get("components_reprocessed")
        with rec.check(f"{n}: no more functions were re-described than the change needs", "F1"):
            expect(isinstance(reprocessed, int), f"components_reprocessed missing from the token entry: {token_entry}")
            expect(reprocessed <= len(expected),
                   f"re-described {reprocessed}, the change needs {len(expected)} ({len(changed)} changed + {len(stale)} callers)")

        with rec.check(f"{n}: every dependent caller was re-described", "E5"):
            expect(isinstance(reprocessed, int), f"components_reprocessed missing from the token entry: {token_entry}")
            # Changed functions the refresh never parsed are an E4/C1 failure,
            # reported above; here only the callers count against the total.
            needed = len(changed & after.callables()) + len(stale)
            unchanged_text = [i for i in stale if after.descriptions.get(i) == before.descriptions.get(i)]
            expect(reprocessed >= needed,
                   f"re-described {reprocessed}, the change needs {needed} "
                   f"({len(changed & after.callables())} changed + {len(stale)} dependent callers: {_short(stale)})"
                   + (f"; callers whose text did not change: {_short(unchanged_text)}" if unchanged_text else ""))

        if stale:
            with rec.check(f"{n}: dependent callers got new text", "E5", advisory=True):
                same = [i for i in stale if after.descriptions.get(i) == before.descriptions.get(i)]
                expect(not same, f"{len(same)} of {len(stale)} kept the same text: {_short(same)}")

    check_invariants(rec, n, after, tree, INVARIANT_REQS_REFRESH)
    check_mcp_source(run, n, chain.space_id, after, ("E6",))

    if step.searches:
        for query, key in step.searches():
            with rec.check(f"{n}: a search for the new behaviour finds {key}", "E4", advisory=True):
                cid = after.id_of(key)
                expect(cid is not None, f"{key} is not in the index")
                hits = [str(h.get("key", "")).removeprefix(f"{chain.space_id}::")
                        for h in api.search(chain.space_id, query, limit=5)]
                expect(cid in hits, f"top 5 for {query!r}: {hits}")

    if step.extra:
        step.extra(ctx)
    if fresh_id:
        run.delete_space(fresh_id)


# Step-specific checks.


def _extra_s14(ctx: StepContext) -> None:
    with ctx.run.rec.check(f"{ctx.step.name}: a commit with no code change spends no AI tokens", "F1"):
        entry = ctx.token_entry or {}
        total = ((entry.get("totals") or {}).get("total"))
        expect(entry.get("components_reprocessed") == 0, f"components_reprocessed={entry.get('components_reprocessed')}")
        expect(total == 0, f"token entry total={total}")


def _extra_languages(present: bool) -> Callable[[StepContext], None]:
    def check(ctx: StepContext) -> None:
        with ctx.run.rec.check(f"{ctx.step.name}: the language list {'includes' if present else 'drops'} JavaScript", "E7"):
            langs = normalize(ctx.after)["languages"]
            expect(("javascript" in langs) == present, f"languages_detected={langs}")
            stats_langs = (ctx.after.status.get("statistics") or {}).get("languages")
            if isinstance(stats_langs, (list, dict)):
                expect(("javascript" in stats_langs) == present, f"statistics.languages={stats_langs}")
    return check


def _extra_s21(ctx: StepContext) -> None:
    rec, n = ctx.run.rec, ctx.step.name
    ready = ctx.final.get("status") == "READY"
    with rec.check(f"{n}: after a rewritten history the Space is correct, or fails clearly with its prior state", "C3"):
        if ready:
            expect(rec.passed(f"{n}: the index matches a fresh index of the same commit"),
                   "refresh ended READY but the index does not match a fresh index of the new commit")
        else:
            expect(ctx.final.get("error"), "refresh failed without an error message")
            diffs = diff_structure(normalize(ctx.before), normalize(ctx.after), "before", "after")
            expect(not diffs, "the failed refresh changed the index: " + "; ".join(diffs[:5]))
    with rec.check(f"{n}: a rewritten history stops before spending and offers a rebuild", "C4"):
        error = str(ctx.final.get("error") or "")
        tokens = ((ctx.token_entry or {}).get("totals") or {}).get("total") if ctx.token_entry else 0
        expect(not ready, "the refresh ran to READY without saying the history was rewritten or offering a rebuild")
        expect(re.search(r"rewrit|history", error, re.I), f"the error doesn't say the history was rewritten: {error[:200]}")
        expect(not tokens, f"tokens were spent before stopping: {tokens}")


def _extra_s23(ctx: StepContext) -> None:
    rec, n = ctx.run.rec, ctx.step.name
    code_files = [p for p in ctx.tree if not p.startswith(("docs/", "marker/"))]
    with rec.check(f"{n}: the refresh records whether it chose a delta or a full rebuild", "F5"):
        runs = ctx.after.status.get("refresh_pipeline_runs") or []
        steps = [str(s.get("step")) for s in (runs[-1].get("steps") or [])] if runs else []
        mode = ctx.after.status.get("refresh_mode") or (ctx.after.status.get("refresh_result") or {}).get("mode")
        # rebuild_call_graph is part of every space refresh, so it doesn't count.
        expect(mode or any(re.search(r"(^|_)full(_|$)|^full_rebuild$|^rebuild$", s) for s in steps),
               f"{len(code_files)} of {len(code_files)} code files changed, and the run shows no choice "
               f"between delta and full rebuild (steps: {steps})")


def _searches_s4() -> list[tuple[str, str]]:
    out = []
    for lang in fx.LANGUAGES:
        norm = fx.accounts_names(lang)[0]
        out.append((f"normalize a {lang.accounts} amount to tenths capped at a ceiling",
                    fx.component_key(lang, fx.path_of(lang, "ledger", "accounts"), norm)))
    return out


def _searches_s19() -> list[tuple[str, str]]:
    return [(f"fuel surcharge on a {lang.rates} freight rate",
             fx.component_key(lang, fx.path_of(lang, "freight", "surcharges"), fx.surcharge_name(lang)))
            for lang in fx.LANGUAGES]


def _pad(state: fx.State, args: argparse.Namespace) -> fx.State:
    """Pad the tariffs file a quarter MiB past the size limit."""
    return state.with_(tariffs=True, tariffs_padding=int((args.max_file_size_mb + 0.25) * 1024 * 1024))


CHAIN_STEPS: list[ChainStep] = [
    ChainStep("S2", "add a new file with three functions", lambda s, a: s.with_(fees=True), ("C1",)),
    ChainStep("S3", "add a function to an existing file", lambda s, a: s.with_(postings_void=True), ("C1",)),
    ChainStep("S4", "change a function that has two callers", lambda s, a: s.with_(normalize_v2=True), ("E5",),
              searches=_searches_s4),
    ChainStep("S5", "change the last function of a 12-function chain", lambda s, a: s.with_(chain_leaf_v2=True), ("E5",)),
    ChainStep("S6", "move a method within its file, unchanged", lambda s, a: s.with_(shipping_reordered=True), ("E10",)),
    ChainStep("S7", "delete a function whose caller stays unchanged", lambda s, a: s.with_(postings_format=False), ("E2", "E5")),
    ChainStep("S8", "delete a whole file", lambda s, a: s.with_(fees=False), ("C1", "E2")),
    ChainStep("S9", "rename a file", lambda s, a: s.with_(accounts_stem="balances"), ("C1", "E2")),
    ChainStep("S10", "move a file to another directory", lambda s, a: s.with_(postings_area="journal"), ("C1", "E6")),
    ChainStep("S11", "rename a function and update its callers", lambda s, a: s.with_(validate_name="check"), ("E2", "E3")),
    ChainStep("S12a", "add files with unusual names", lambda s, a: s.with_(weird=1), ("C2",)),
    ChainStep("S12b", "modify files with unusual names", lambda s, a: s.with_(weird=2), ("C2",)),
    ChainStep("S12c", "delete files with unusual names", lambda s, a: s.with_(weird=0), ("C2",)),
    ChainStep("S13a", "move a file into an excluded directory", lambda s, a: s.with_(shipping_area="build"), ("C7",)),
    ChainStep("S13b", "move the file back out", lambda s, a: s.with_(shipping_area="freight"), ("C7",)),
    ChainStep("S14", "change only a non-code file", lambda s, a: s.with_(readme_rev=s.readme_rev + 1), ("F1",),
              extra=_extra_s14),
    ChainStep("S15", "change a class field and a class comment only", lambda s, a: s.with_(carrier_v2=True), ("E5",)),
    ChainStep("S16a", "delete every JavaScript file", lambda s, a: s.with_(js_present=False), ("E7",),
              extra=_extra_languages(False)),
    ChainStep("S16b", "add the JavaScript files back", lambda s, a: s.with_(js_present=True), ("E7",),
              extra=_extra_languages(True)),
    ChainStep("S17", "add, modify, delete and rename in one commit",
              lambda s, a: s.with_(tariffs=True, base_rate_v2=True, describe_present=False, accounts_stem="ledgers"), ("C1",)),
    ChainStep("S18a", "grow a file past the size limit", _pad, ("C7",)),
    ChainStep("S18b", "shrink the file back under the limit", lambda s, a: s.with_(tariffs_padding=0), ("C7",)),
    ChainStep("S19", "define a function an unchanged caller already calls", lambda s, a: s.with_(surcharges=True), ("E5",),
              searches=_searches_s19),
    ChainStep("S20", "add files whose stem collides with an existing file", lambda s, a: s.with_(collisions=True), ("E3",)),
    ChainStep("S21", "force-push a rewritten history", lambda s, a: s.with_(chain_mid_v2=True), ("C3",), mode="orphan",
              extra=_extra_s21),
    ChainStep("S22", "reset the branch to an earlier commit (S10)", reqs=("H3",), mode="reset", reset_to="S10"),
    ChainStep("S23", "change more than half of all files", lambda s, a: s.with_(header_rev=s.header_rev + 1), ("F5",),
              extra=_extra_s23),
]


def step_s24(run: Run, chain: ChainState) -> None:
    """End of the chain: after every refresh, the Space still matches a fresh index (E8)."""
    c, rec = run.console, run.rec
    c.heading("S24  end of chain")
    after, fresh = chain.snapshot, chain.last_fresh
    if after is None or fresh is None or len(chain.history) < 2:
        rec.skip("S24: after the whole chain the Space matches a fresh index", "the chain did not run", "E8")
        return
    with rec.check("S24: after the whole chain the Space matches a fresh index", "E8"):
        problems = diff_structure(normalize(after), normalize(fresh), "refreshed", "fresh")
        problems += diff_coverage(normalize(after), normalize(fresh), "refreshed", "fresh")
        orphans = set(after.vector_hits) - set(after.components)
        if orphans:
            problems.append(f"vectors for components not in the index: {_short(orphans)}")
        if after.detail.get("vector_count") != len(after.described()):
            problems.append(f"vector_count={after.detail.get('vector_count')}, described={len(after.described())}")
        if after.detail.get("component_count") != len(after.components):
            problems.append(f"component_count={after.detail.get('component_count')}, KB holds {len(after.components)}")
        expect(not problems, f"after {len(chain.history) - 1} refreshed commit(s):\n" + "\n".join(problems))
    usage = (after.status.get("token_usage") or {}).get("space_refresh") or {}
    run.meta["chain_refresh_tokens"] = (usage.get("cumulative") or {}).get("totals")
    run.meta["chain_refreshes"] = usage.get("generation_count")
    c.note(f"refresh tokens over the chain: {run.meta['chain_refresh_tokens']} in {run.meta['chain_refreshes']} refresh(es)")


def _step_runner(step: ChainStep) -> Callable[[Run, ChainState], None]:
    return lambda run, chain: run_chain_step(run, chain, step)


STEPS: dict[str, Callable[[Run, ChainState], None]] = {
    "S0": step_s0,
    "S1": step_s1,
    **{s.name: _step_runner(s) for s in CHAIN_STEPS},
    "S24": step_s24,
}


def parse_steps(spec: str) -> list[str]:
    names = list(STEPS)
    if not spec:
        return names
    by_upper = {name.upper(): name for name in names}
    chosen: list[str] = []
    for part in spec.split(","):
        part = part.strip().upper()
        if "-" in part:
            a, b = (by_upper.get(x) for x in part.split("-", 1))
            if a is None or b is None:
                raise SystemExit(f"unknown step range {part!r}; steps: {', '.join(names)}")
            chosen += names[names.index(a): names.index(b) + 1]
        elif part in by_upper:
            chosen.append(by_upper[part])
        else:
            raise SystemExit(f"unknown step {part!r}; steps: {', '.join(names)}")
    if chosen and chosen[0] != "S0":
        chosen.insert(0, "S0")  # every step needs the seeded Space
    return list(dict.fromkeys(chosen))


# ── Token-leak scanner (J1) ──────────────────────────────────────────────────

_TOKEN_SHAPES = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}")


class LeakScanner:
    """Looks for the push token, its basic-auth form, and GitHub token shapes
    in every API response the program receives."""

    def __init__(self, secrets_: tuple[str, ...]) -> None:
        self._secrets = tuple(s for s in secrets_ if s)
        self.responses = 0
        self.bytes = 0
        self.leaks: list[str] = []
        self._lock = threading.Lock()

    def scan(self, where: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.responses += 1
            self.bytes += len(text)
            for s in self._secrets:
                if s in text:
                    self.leaks.append(f"{where}: contains the push token")
            m = _TOKEN_SHAPES.search(text)
            if m:
                self.leaks.append(f"{where}: contains a GitHub token shape ({m.group(0)[:12]}…)")


# ── Reader during a refresh (G1, G2, G4, I1, I3) ─────────────────────────────


@dataclass
class ReadSample:
    t: float
    state: str | None = None
    step: str | None = None
    summarize_total: Any = None
    commit: str | None = None
    kb_ids: set[str] | None = None  # callables in the components list
    marker: str | None = None  # step marker in the served source snapshot
    hit_ids: set[str] = field(default_factory=set)  # from scoped, global and MCP search
    errors: list[str] = field(default_factory=list)
    hyperspace_errors: list[str] = field(default_factory=list)


def _ids_from_text(text: str, space_id: str) -> set[str]:
    """Component IDs of one Space in an MCP tool's JSON (or plain) output."""
    prefix = f"{space_id}::"
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("results", []) if isinstance(data, dict) else []
        return {str(r.get("id", "")).removeprefix(prefix) for r in rows
                if isinstance(r, dict) and str(r.get("id", "")).startswith(prefix)}
    except (ValueError, AttributeError):
        return {m.removeprefix(prefix) for m in re.findall(re.escape(prefix) + r'[^"\s,\]]+', text)}


class RefreshReader:
    """Queries a Space about once a second, from another thread, while it refreshes.

    Each sample reads the status, the components list, the step marker from the
    served source snapshot, scoped search, global search, MCP search, a call
    graph with source, and (if given) a hyperspace. One agent chat runs once per
    refresh in its own thread, because it takes tens of seconds.
    """

    def __init__(self, run: Run, space_id: str, queries: list[str], probe: str, marker_id: str | None, *,
                 chat: bool) -> None:
        self.run, self.sid, self.queries, self.probe = run, space_id, queries or run.queries[:1], probe
        self.marker_id = marker_id
        self.samples: list[ReadSample] = []
        self.chat_result: str | None = None  # None = not run; "" = ok; text = error
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._loop, daemon=True)]
        if chat:
            self._threads.append(threading.Thread(target=self._chat, daemon=True))

    def __enter__(self) -> "RefreshReader":
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=360)

    def _get(self, sample: ReadSample, label: str, path: str, params: dict | None = None, *, hyper: bool = False):
        client, scan = self.run.api.client, self.run.scanner
        try:
            resp = client.get(path, params=params, timeout=60)
        except Exception as exc:  # a failed read is the finding
            (sample.hyperspace_errors if hyper else sample.errors).append(f"{label}: {exc}")
            return None
        scan.scan(label, resp.text)
        if resp.status != 200:
            (sample.hyperspace_errors if hyper else sample.errors).append(f"{label}: HTTP {resp.status} {resp.preview(120)}")
            return None
        try:
            return resp.json()
        except CheckError as exc:
            sample.errors.append(f"{label}: {exc}")
            return None

    def _mcp(self, sample: ReadSample, label: str, tool: str, args: dict, *, hyper: bool = False) -> str | None:
        try:
            text = self.run.mcp.call_tool(tool, args, timeout=120)
        except Exception as exc:
            (sample.hyperspace_errors if hyper else sample.errors).append(f"{label}: {exc}")
            return None
        self.run.scanner.scan(label, text)
        return text

    def _loop(self) -> None:
        i = 0
        sid, prefix = self.sid, f"{self.sid}::"
        base = f"/api/spaces/{q(sid)}"
        while not self._stop.is_set():
            s = ReadSample(time.time())
            query = self.queries[i % len(self.queries)]
            i += 1
            st = self._get(s, "status", f"{base}/status")
            if isinstance(st, dict):
                s.state, s.step = st.get("status"), st.get("current_step")
                s.summarize_total, s.commit = st.get("summarize_total"), st.get("git_commit_hash")
            comps = self._get(s, "components", f"{base}/components")
            if isinstance(comps, list):
                s.kb_ids = {str(c.get("id", "")).removeprefix(prefix) for c in comps if c.get("type") in CALLABLE_TYPES}
            marker = None
            if self.marker_id is None:
                s.errors.append("source (marker): the marker constant is not in the index")
            else:
                marker = self._get(s, "source (marker)", f"{base}/components/{q(prefix + self.marker_id)}")
            if isinstance(marker, dict):
                m = re.search(r'FIXTURE_STEP = "([^"]+)"', str(marker.get("source_code") or ""))
                s.marker = m.group(1) if m else None
                if not m:
                    s.errors.append("source (marker): no source returned for the marker constant")
            for label, path, params in (("scoped search", f"{base}/search", {"q": query, "limit": 50}),
                                        ("global search", "/api/search", {"q": query, "spaces": sid, "limit": 50})):
                hits = self._get(s, label, path, params)
                if isinstance(hits, list):
                    s.hit_ids |= {str(h.get("key", "")).removeprefix(prefix) for h in hits
                                  if str(h.get("key", "")).startswith(prefix)}
            text = self._mcp(s, "MCP search", "get_component_from_description",
                             {"query": query, "space_id": sid, "n_results": 20})
            if text is not None:
                s.hit_ids |= _ids_from_text(text, sid)
            graph = self._mcp(s, "MCP call graph with source", "get_call_graph_with_source", {"component_id": prefix + self.probe})
            if graph is not None and re.search(r"Error reading source|Source not available|not found", graph):
                s.errors.append(f"MCP call graph with source: {graph[:160]}")
            if self.run.hyperspace_id:
                self._mcp(s, "MCP hyperspace search", "get_component_from_description",
                          {"query": query, "hyperspace_id": self.run.hyperspace_id, "n_results": 10}, hyper=True)
            self.samples.append(s)
            self._stop.wait(0.5)

    def _chat(self) -> None:
        try:
            resp = self.run.api.client.post(
                "/api/agent/chat",
                json_body={"message": f"In one sentence, what does {self.probe.split('.')[-1]} do?", "space_ids": [self.sid]},
                timeout=300,
            )
            self.run.scanner.scan("agent chat", resp.text)
            body = resp.json() if resp.status == 200 else {}
            self.chat_result = "" if resp.status == 200 and str(body.get("response", "")).strip() else \
                f"HTTP {resp.status} {resp.preview(160)}"
        except Exception as exc:
            self.chat_result = f"{type(exc).__name__}: {exc}"


def evaluate_reader(rec: Recorder, n: str, reader: RefreshReader, *, old_ids: set[str], new_ids: set[str],
                    old_marker: str, new_marker: str, prior_sha: str | None, refreshed: bool) -> None:
    """Checks on what readers saw while the Space refreshed."""
    samples = reader.samples
    during = [s for s in samples if s.state == "REFRESHING"]
    with rec.check(f"{n}: every query during the refresh succeeded", "G1"):
        expect(samples, "the reader took no samples")
        errors = [e for s in samples for e in s.errors]
        if reader.chat_result:
            errors.append(f"agent chat: {reader.chat_result}")
        expect(not errors, f"{len(errors)} failed read(s) in {len(samples)} sample(s): " + "; ".join(sorted(set(errors))[:6]))

    if reader.run.hyperspace_id:
        with rec.check(f"{n}: the hyperspace kept working during the refresh", "G4"):
            errors = [e for s in samples for e in s.hyperspace_errors]
            expect(not errors, "; ".join(sorted(set(errors))[:4]))

    def version_of_ids(ids: set[str] | None) -> str | None:
        if ids is None or old_ids == new_ids:
            return None
        return "old" if ids == old_ids else "new" if ids == new_ids else "mixed"

    added, removed = new_ids - old_ids, old_ids - new_ids
    timeline = []
    for s in samples:
        kb = version_of_ids(s.kb_ids) or "-"
        src = "old" if s.marker == old_marker else "new" if s.marker == new_marker else "-"
        vec = "new" if s.hit_ids & added else "old" if s.hit_ids & removed else "-"
        timeline.append(f"t+{s.t - samples[0].t:4.0f}s {str(s.state):<14} {str(s.step):<22} index={kb:<5} source={src:<4} search={vec}")
    reader.run.meta.setdefault("reader_timelines", {})[n] = timeline
    with rec.check(f"{n}: every response during the refresh came from one commit", "G2"):
        problems: list[str] = []
        seen_new = {"index": False, "source": False}
        for s in samples:
            kb = version_of_ids(s.kb_ids)
            src = "old" if s.marker == old_marker else "new" if s.marker == new_marker else None
            vec_new, vec_old = bool(s.hit_ids & added), bool(s.hit_ids & removed)
            when = f"t+{s.t - samples[0].t:.0f}s ({s.state}, {s.step})"
            if kb == "mixed":
                problems.append(f"{when}: the components list is neither the old nor the new commit")
            if kb and src and kb != src:
                problems.append(f"{when}: the components list is the {kb} commit but the source snapshot is the {src} one")
            if vec_new and kb == "old":
                problems.append(f"{when}: search returns functions of the new commit while the index is the old one: "
                                f"{_short(s.hit_ids & added, 4)}")
            if vec_new and src == "old" and kb is None:
                problems.append(f"{when}: search returns functions of the new commit while the source snapshot is the old one")
            if vec_old and (kb == "new" or src == "new"):
                problems.append(f"{when}: search still returns removed functions after the switch: {_short(s.hit_ids & removed, 4)}")
            for dim, v in (("index", kb), ("source", src)):
                if v == "new":
                    seen_new[dim] = True
                elif v == "old" and seen_new[dim]:
                    problems.append(f"{when}: the {dim} went back to the old commit after showing the new one")
        expect(not problems, f"{len(problems)} inconsistent sample(s) of {len(samples)}:\n" + "\n".join(problems[:6])
               + "\ntimeline:\n" + "\n".join(timeline[:16]))

    with rec.check(f"{n}: status shows the current step and progress during the refresh", "I1"):
        if not during:
            raise Skipped("the refresh finished before the reader saw it running")
        no_step = [s for s in during if not s.step]
        no_progress = [s for s in during if s.step == "summarize_changed" and s.summarize_total is None]
        expect(not no_step, f"{len(no_step)} sample(s) while REFRESHING had no current_step")
        expect(not no_progress, f"{len(no_progress)} sample(s) in summarize_changed had no summarize_total")

    with rec.check(f"{n}: the recorded commit stays at the prior commit until the refresh succeeds", "I3"):
        if not during:
            raise Skipped("the refresh finished before the reader saw it running")
        moved = [s for s in during if s.commit != prior_sha]
        expect(not moved, f"{len(moved)} sample(s) while REFRESHING showed git_commit_hash {moved[0].commit if moved else ''}")


# ── Group S: history after the chain (I2, I3, I6, J6) ────────────────────────

RUN_FIELDS = {
    "who started it": ("actor", "refresh_actor", "started_by", "created_by"),
    "commit it went from": ("from_commit", "prior_commit", "from_sha", "prior_commit_hash"),
    "commit it went to": ("to_commit", "commit", "to_sha", "git_commit_hash", "new_commit_hash"),
    "outcome": ("status", "outcome", "result"),
    "duration": ("duration_s", "duration", "completed_at", "finished_at"),
    "files changed": ("files_added", "files_modified", "files_removed", "file_changes"),
    "components re-described": ("components_reprocessed", "components_redescribed"),
    "AI usage": ("token_usage", "tokens"),
}


def group_s_history(run: Run, chain: ChainState) -> None:
    c, rec = run.console, run.rec
    c.heading("History and visibility (S-5 to S-7)")
    if chain.space_id is None:
        rec.skip("S-5: each refresh is recorded with who, what and how much", "no chain Space", "I2", "J6")
        return
    status = run.api.status(chain.space_id)
    detail = run.api.detail(chain.space_id)
    runs = status.get("refresh_pipeline_runs") or []

    with rec.check("S-5: each refresh is recorded with who, what and how much", "I2"):
        if not runs:
            raise Skipped("no refresh runs recorded")
        missing = sorted({label for r in runs for label, keys in RUN_FIELDS.items() if not any(k in r for k in keys)})
        expect(not missing, f"{len(runs)} run record(s) lack: {', '.join(missing)}. A run record has: {sorted(runs[-1])}")

    with rec.check("S-5: every refresh records who started it", "J6"):
        if not runs:
            raise Skipped("no refresh runs recorded")
        without = [i for i, r in enumerate(runs) if not any(k in r for k in RUN_FIELDS["who started it"])]
        expect(not without, f"{len(without)} of {len(runs)} run record(s) don't name who started them")

    with rec.check("S-6: the Space shows when its current state was indexed", "I3"):
        keys = ("refresh_completed_at", "indexed_at", "last_indexed_at", "analyzed_at", "last_refreshed_at")
        found = [k for k in keys if status.get(k) or detail.get(k)]
        expect(found, f"neither /status nor the Space detail has any of {keys}")

    with rec.check("S-7: the refresh result is reported", "I6"):
        result = status.get("refresh_result")
        expect(isinstance(result, dict), "refresh_result is declared in the status API but empty")
        entry = last_refresh_token_entry(status) or {}
        if "components_reprocessed" in result and "components_reprocessed" in entry:
            expect(result["components_reprocessed"] == entry["components_reprocessed"],
                   f"refresh_result says {result['components_reprocessed']}, token entry {entry['components_reprocessed']}")


# ── Group R: failure and recovery (Tier 1) ───────────────────────────────────

GUIDANCE = re.compile(r"recreate|re-create|push|restore|check (that|the)|credential|retry|try again|wait|re-?upload|"
                      r"create a new|delete and|instead", re.I)


def availability_while_failed(run: Run, sid: str, label: str, query: str, probe: str) -> None:
    """The Space must stay usable at its last good state after a failure (H1, G1, G4)."""
    rec, client = run.rec, run.api.client
    prefix = f"{sid}::"
    with rec.check(f"{label}: scoped search still works", "H1", "G1"):
        hits = run.api.search(sid, query, limit=10)
        expect(hits, "no hits")
    with rec.check(f"{label}: global search still includes the Space", "H1", "G1"):
        resp = expect_status(client.get("/api/search", params={"q": query, "spaces": sid, "limit": 10}, timeout=120), 200)
        run.scanner.scan("global search", resp.text)
        hits = expect_list(resp)
        expect(any(str(h.get("key", "")).startswith(prefix) for h in hits), f"global search returned no hit from {sid}")
    with rec.check(f"{label}: global search across all Spaces still includes the Space", "H1", "G1"):
        hits = expect_list(expect_status(client.get("/api/search", params={"q": query, "limit": 50}, timeout=120), 200))
        expect(any(str(h.get("key", "")).startswith(prefix) for h in hits), f"no hit from {sid} in the top 50")
    with rec.check(f"{label}: MCP search still includes the Space", "H1", "G1"):
        text = run.mcp.call_tool("get_component_from_description", {"query": query, "space_id": sid, "n_results": 10})
        expect(_ids_from_text(text, sid), f"no components of {sid} in: {text[:200]}")
    if run.hyperspace_id:
        with rec.check(f"{label}: the hyperspace still includes the Space", "H1", "G4"):
            text = run.mcp.call_tool("get_component_from_description",
                                     {"query": query, "hyperspace_id": run.hyperspace_id, "n_results": 10})
            expect(_ids_from_text(text, sid), f"no components of {sid} in the hyperspace search: {text[:200]}")
    with rec.check(f"{label}: agent chat scoped to the Space still answers", "H1", "G1"):
        resp = client.post("/api/agent/chat", json_body={"message": f"In one sentence, what does {probe.split('.')[-1]} do?",
                                                          "space_ids": [sid]}, timeout=300)
        expect_status(resp, 200)
        text = str(expect_dict(resp).get("response", ""))
        expect(text.strip(), "empty response")
        expect("have access to any spaces" not in text and "have access to all the selected" not in text,
               f"the agent refused the Space: {text[:200]}")
    with rec.check(f"{label}: the agent's all-Spaces search still includes the Space", "H1", "G1", advisory=True):
        # Advisory: whether the model names the Space depends on its answer.
        word = probe.split(".")[-1]
        resp = client.post("/api/agent/chat", json_body={"message": f"Which space defines the function {word}? Answer with the space id."},
                           timeout=300)
        run.scanner.scan("agent chat", resp.text)
        expect_status(resp, 200)
        text = str(expect_dict(resp).get("response", ""))
        expect(sid in text or word in text, f"the answer names neither {sid} nor {word}: {text[:200]}")


def group_r_failures(run: Run, chain: ChainState) -> None:
    """R1 and R2 on the chain Space: a deleted branch, then recovery with new commits."""
    c, rec, api = run.console, run.rec, run.api
    c.heading("R1  refresh after the branch was deleted")
    if chain.space_id is None or chain.snapshot is None or run.repo is None:
        rec.skip("R1: refresh of a deleted branch fails clearly", "no chain Space", "C5")
        return
    sid, before = chain.space_id, chain.snapshot
    prior_sha = before.status.get("git_commit_hash")
    with rec.check("R1: delete the chain branch on GitHub"):
        run.repo.delete_remote_branch(run.branch("chain"))
    if not rec.passed("R1: delete the chain branch on GitHub"):
        run.harness_failed = True
        return

    final: dict = {}
    with rec.check("R1: refresh is accepted", "A3"):
        expect_status(api.refresh(sid), 202)
    if rec.passed("R1: refresh is accepted"):
        final = api.wait(sid, "R1 refresh", interval=2.0)
    error = str(final.get("error") or "")
    c.note(f"R1 ended {final.get('status')}: {error[:200]}")
    with rec.check("R1: refresh of a deleted branch fails with a specific message", "C5", "H5"):
        expect(final.get("status") == "REFRESH_FAILED", f"ended {final.get('status')}")
        expect(re.search(r"branch|not found|couldn.t find|does not exist", error, re.I),
               f"the error doesn't say the branch is missing: {error[:300]}")
    with rec.check("R1: the message says what to do", "H4", "H5"):
        expect(GUIDANCE.search(error), f"no next step in the message: {error[:300]}")
    with rec.check("R1: the message is in user terms, without internal step names", "H5"):
        internal = re.findall(r"\[refresh\]|\bclone\b:|remediate_depth|commit_kb|CopyObject|GitCommandError|fatal:", error)
        expect(not internal, f"internal terms {sorted(set(internal))} in: {error[:300]}")
    after = take_snapshot(api, sid, run.queries)
    with rec.check("R1: the index, descriptions and commit are unchanged", "H1", "C5"):
        diffs = diff_structure(normalize(before), normalize(after), "before", "after")
        changed = sorted(i for i in before.callables() if before.descriptions.get(i) != after.descriptions.get(i))
        if changed:
            diffs.append(f"{len(changed)} description(s) changed: {_short(changed)}")
        if after.status.get("git_commit_hash") != prior_sha:
            diffs.append(f"git_commit_hash moved to {after.status.get('git_commit_hash')}")
        expect(not diffs, "\n".join(diffs))
    with rec.check("R1: the failure is visible and the Space doesn't look current", "H1", "I3"):
        expect(after.status.get("status") == "REFRESH_FAILED", f"status is {after.status.get('status')}")
        expect(after.status.get("git_commit_hash") == prior_sha, "the recorded commit moved")
    availability_while_failed(run, sid, "R1", run.queries[0], chain_probe(after))
    chain.snapshot = after

    # R2: the branch comes back with two new commits; the retry must converge.
    r2 = ChainStep("R2", "recreate the branch with two new commits and retry",
                   lambda s, a: s.with_(fees=not s.fees), ("H3", "E8"), mode="two_commits")
    run_chain_step(run, chain, r2)


def group_r3_credentials(run: Run) -> None:
    """R3: the credential the Space was cloned with is removed (C5, H4, H5)."""
    c, rec, api = run.console, run.rec, run.api
    c.heading("R3  refresh after the git credential was removed")
    if run.repo is None:
        rec.skip("R3: refresh without a credential", "no push token", "C5")
        return
    ident = f"codekb-e2e-{run.run_id}@codekb.test"
    other = ScanningClient(api.client.base_url, user_identity=ident)
    base = "/api/user-profile/gitcreds"
    sid = None
    with rec.check("R3: store a credential for a throwaway identity and index a Space as it"):
        resp = other.put(f"{base}/github", json_body={"username": "codekb-e2e", "token": run.repo._token}, timeout=60)
        expect_status(resp, 200)
        run.cleanup_credentials.append(ident)
        run.repo.write_tree(fx.build_tree(fx.State(marker="R3")))
        sha = run.repo.commit_orphan(f"codekb-e2e {run.run_id}: R3 seed")
        branch = run.push_branch(sha, "r3")
        resp = other.post("/api/spaces", json_body={"git_url": run.args.fixture_repo, "branch": branch,
                                                    "space_name": f"e2e-{run.run_id}-r3", "summarize": False}, timeout=120)
        expect_status(resp, 201)
        sid = expect_dict(resp)["space_id"]
        run.spaces.append(sid)
        st = CodeKB(other, c, run.args.analysis_timeout).wait(sid, "R3 index")
        expect(st.get("status") == "READY", f"ended {st.get('status')}: {st.get('error')}")
    if sid is None or not rec.passed("R3: store a credential for a throwaway identity and index a Space as it"):
        run.harness_failed = True
        return
    prior = run.api.status(sid).get("git_commit_hash")
    with rec.check("R3: remove the credential"):
        expect_status(other.delete(f"{base}/github", timeout=60), 200)
    resp = other.post(f"/api/spaces/{q(sid)}/refresh", timeout=120)
    run.scanner.scan("refresh response", resp.text)
    message, outcome = "", ""
    if resp.status == 202:
        final = run.api.wait(sid, "R3 refresh", interval=2.0)
        message, outcome = str(final.get("error") or ""), str(final.get("status"))
    else:
        message, outcome = resp.text, f"HTTP {resp.status}"
    c.note(f"R3: {outcome}: {message[:200]}")
    with rec.check("R3: the refresh is refused or fails, and the Space keeps its state", "C5"):
        expect(outcome in ("REFRESH_FAILED",) or outcome.startswith("HTTP 4"), f"outcome {outcome}")
        expect(run.api.status(sid).get("git_commit_hash") == prior, "the recorded commit moved")
    with rec.check("R3: the message points to the git-credentials page", "H4", "H5"):
        expect(re.search(r"credential", message, re.I), f"the message doesn't mention credentials: {message[:300]}")


def group_r4_leaks(run: Run) -> None:
    rec = run.rec
    run.console.heading("R4  credentials never appear in stored or returned data")
    with rec.check("R4: the push token never appears in any API response", "J1"):
        expect(run.scanner.responses > 0, "no responses were scanned")
        expect(not run.scanner.leaks, f"{len(run.scanner.leaks)} leak(s): " + "; ".join(sorted(set(run.scanner.leaks))[:6]))
    run.console.note(f"scanned {run.scanner.responses} responses, {run.scanner.bytes / 1e6:.1f} MB")


# ── Group K: concurrency (Tier 1) ────────────────────────────────────────────


def _fire_together(calls: list[Callable[[], Any]]) -> list[Any]:
    """Run calls at the same moment from separate threads."""
    barrier = threading.Barrier(len(calls))
    out: list[Any] = [None] * len(calls)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            out[i] = calls[i]()
        except Exception as exc:
            out[i] = exc
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    return out


def _quick_space(run: Run, suffix: str, state: fx.State) -> tuple[str, str, str]:
    """A Space without summaries on its own branch. Returns (space_id, branch, sha)."""
    assert run.repo is not None
    run.repo.write_tree(fx.build_tree(state))
    sha = run.repo.commit_orphan(f"codekb-e2e {run.run_id}: {suffix}")
    branch = run.push_branch(sha, suffix)
    sid = run.api.create_space(run.args.fixture_repo, branch, f"e2e-{run.run_id}-{suffix}", summarize=False)
    run.spaces.append(sid)
    return sid, branch, sha


def _push_change(run: Run, sha: str, suffix: str, state: fx.State) -> str:
    assert run.repo is not None
    run.repo.checkout(sha)
    run.repo.write_tree(fx.build_tree(state))
    new = run.repo.commit(f"codekb-e2e {run.run_id}: {suffix} {state.marker}")
    run.push_branch(new, suffix)
    return new


def group_k_concurrency(run: Run) -> None:
    c, rec, api = run.console, run.rec, run.api
    c.heading("K1, K2  concurrent refresh requests, and delete during a refresh")
    if run.repo is None:
        return
    sid = None
    with rec.check("K: index a Space for the concurrency checks"):
        sid, branch, sha = _quick_space(run, "k", fx.State(marker="K0"))
        st = api.wait(sid, "K index")
        expect(st.get("status") == "READY", f"ended {st.get('status')}: {st.get('error')}")
    if sid is None or not rec.passed("K: index a Space for the concurrency checks"):
        run.harness_failed = True
        return

    sha = _push_change(run, sha, "k", fx.State(marker="K1", fees=True))
    runs_before = len(api.status(sid).get("refresh_pipeline_runs") or [])
    results = _fire_together([lambda: api.refresh(sid) for _ in range(5)])
    codes = [r.status if hasattr(r, "status") else repr(r) for r in results]
    for r in results:
        if hasattr(r, "text"):
            run.scanner.scan("refresh response", r.text)
    c.note(f"K1 responses: {codes}")
    with rec.check("K1: of five simultaneous refresh requests exactly one is accepted", "B1"):
        expect(codes.count(202) == 1, f"responses {codes}")
    with rec.check("K1: the others are refused with a clear busy message", "B2"):
        refused = [r for r in results if hasattr(r, "status") and r.status != 202]
        expect(refused and all(r.status == 409 for r in refused), f"responses {codes}")
        expect(all(re.search(r"busy|in progress|already", r.text, re.I) for r in refused),
               f"a refusal doesn't say the Space is busy: {refused[0].preview(200) if refused else ''}")
    final = api.wait(sid, "K1 refresh", interval=2.0)
    with rec.check("K1: one refresh ran", "B1"):
        runs = final.get("refresh_pipeline_runs") or []
        expect(len(runs) == runs_before + 1, f"refresh_pipeline_runs went from {runs_before} to {len(runs)}")
        expect(final.get("status") == "READY", f"ended {final.get('status')}: {final.get('error')}")

    # K2: delete during a refresh must be refused; afterwards it works and sticks.
    sha = _push_change(run, sha, "k", fx.State(marker="K2", fees=True, postings_void=True))
    resp = api.refresh(sid)
    running = api.status(sid).get("status") == "REFRESHING"
    delete_during = api.client.delete(f"/api/spaces/{q(sid)}", timeout=120)
    with rec.check("K2: delete is refused while a refresh runs", "B3"):
        if resp.status != 202:
            raise CheckError(f"the refresh was not accepted: {resp.status} {resp.preview(160)}")
        if not running and delete_during.status == 202:
            raise Skipped("the refresh finished before the delete was sent")
        expect(delete_during.status == 409, f"delete returned {delete_during.status} while the Space was REFRESHING: "
               f"{delete_during.preview(160)}")
    if delete_during.status != 202:
        api.wait(sid, "K2 refresh", interval=2.0)
        with rec.check("K2: after the refresh, delete works and the Space stays deleted", "B3"):
            expect(api.delete(sid) == 202, "delete after the refresh was refused")
            run.deleted.add(sid)
            deadline = time.time() + 90
            while time.time() < deadline:
                r = api.client.get(f"/api/spaces/{q(sid)}/status", timeout=30)
                if r.status == 200 and (r.json() or {}).get("status") not in ("DELETING", "NOT_FOUND"):
                    raise CheckError(f"the deleted Space came back as {(r.json() or {}).get('status')}")
                time.sleep(5)
    else:
        run.deleted.add(sid)
        # Wait for the abandoned refresh to end, then make sure the delete stuck.
        time.sleep(60)
        with rec.check("K2: a Space deleted during a refresh stays deleted", "B3"):
            r = api.client.get(f"/api/spaces/{q(sid)}/status", timeout=30)
            expect(not (r.status == 200 and (r.json() or {}).get("status") in ("READY", "REFRESH_FAILED", "REFRESHING")),
                   f"the Space came back as {(r.json() or {}).get('status') if r.status == 200 else r.status}")


def _overview_state(status: dict) -> str:
    return str(status.get("overview_status") or "").lower()


def group_k3_overviews(run: Run, chain: ChainState) -> None:
    """K3 and I7 on the companion Space: overview generation and refresh exclude each other."""
    c, rec, api = run.console, run.rec, run.api
    c.heading("K3  overview generation and refresh, and stale overviews (I7)")
    sid = run.companion_id
    if sid is None or run.repo is None:
        rec.skip("K3: refresh is refused while an overview is generated", "no companion Space", "B1", "B2")
        return
    cfg = expect_dict(api.client.get("/api/config", timeout=60))
    if not cfg.get("overview_generation_enabled"):
        rec.skip("K3: refresh is refused while an overview is generated", "overview generation is disabled", "B1", "B2")
        return

    gen = api.client.post(f"/api/spaces/{q(sid)}/generate-overview", timeout=120)
    # Wait until generation is visibly running, so a 409 can't come from the
    # status write racing the refresh's conditional claim instead of a busy check.
    seen_running = False
    for _ in range(30):
        if _overview_state(api.status(sid)) == "in_progress":
            seen_running = True
            break
        time.sleep(1)
    time.sleep(3)
    ref = api.refresh(sid)
    c.note(f"K3a: generate-overview {gen.status}, running={seen_running}, then refresh {ref.status}: {ref.preview(160)}")
    with rec.check("K3: refresh is refused while an overview is being generated", "B1", "B2", "B3"):
        expect(gen.status == 202, f"generate-overview returned {gen.status}: {gen.preview(160)}")
        if not seen_running:
            raise Skipped("overview generation never showed in_progress")
        expect(ref.status == 409, f"refresh returned {ref.status} while overview generation was in progress")
        expect(re.search(r"busy|overview|in progress", ref.text, re.I), f"the refusal doesn't say why: {ref.preview(200)}")
    # Let both finish.
    deadline = time.time() + run.args.analysis_timeout
    while time.time() < deadline:
        st = api.status(sid)
        if st.get("status") in TERMINAL_STATES and _overview_state(st) not in ("in_progress", "generating", "pending", "running"):
            break
        time.sleep(10)
    st = api.status(sid)
    docs = st.get("space_overview_docs") or []
    c.note(f"overview: {_overview_state(st)}, {len(docs)} document(s); space {st.get('status')}")

    sha = run.companion_sha
    sha = _push_change(run, sha, "step-00", fx.State(marker="K3", normalize_v2=True))
    run.companion_sha = sha
    ref = api.refresh(sid)
    gen = api.client.post(f"/api/spaces/{q(sid)}/generate-overview", timeout=120)
    c.note(f"K3b: refresh {ref.status}, then generate-overview {gen.status}")
    with rec.check("K3: overview generation is refused while a refresh runs", "B2", "B3"):
        expect(ref.status == 202, f"refresh returned {ref.status}: {ref.preview(160)}")
        expect(gen.status == 409, f"generate-overview returned {gen.status} during the refresh")
    final = api.wait(sid, "K3 refresh", interval=3.0)
    with rec.check("I7: overviews are marked stale after a refresh that changes code", "I7"):
        if not docs:
            raise Skipped("no overview documents were generated")
        expect(final.get("status") == "READY", f"refresh ended {final.get('status')}")
        st = api.status(sid)
        stale = [d for d in st.get("space_overview_docs") or [] if d.get("stale")]
        expect(stale or _overview_state(st) == "stale",
               f"overview_status={st.get('overview_status')}, no document flagged stale")


def group_k5_many(run: Run, count: int = 10) -> None:
    """K5: many Spaces refreshing at once (B5, advisory unless a limit is given)."""
    c, rec, api = run.console, run.rec, run.api
    c.heading(f"K5  {count} Spaces refreshing at once")
    if run.repo is None:
        return
    sids: list[str] = []
    with rec.check(f"K5: index {count} Spaces"):
        run.repo.write_tree(fx.build_tree(fx.State(marker="K5")))
        sha = run.repo.commit_orphan(f"codekb-e2e {run.run_id}: K5 seed")
        branch = run.push_branch(sha, "k5")
        for i in range(count):
            sid = api.create_space(run.args.fixture_repo, branch, f"e2e-{run.run_id}-k5-{i}", summarize=False)
            run.spaces.append(sid)
            sids.append(sid)
        for sid in sids:
            st = api.wait(sid, f"K5 index {sid[-8:]}", interval=5.0)
            expect(st.get("status") == "READY", f"{sid} ended {st.get('status')}")
    if len(sids) != count:
        run.harness_failed = True
        return
    _push_change(run, sha, "k5", fx.State(marker="K5b", fees=True, tariffs=True))
    results = _fire_together([lambda s=s: api.refresh(s) for s in sids])
    accepted = sum(1 for r in results if getattr(r, "status", None) == 202)
    peak, deadline = 0, time.time() + run.args.analysis_timeout
    while time.time() < deadline:
        states = [api.status(s) for s in sids]
        working = sum(1 for st in states if st.get("status") == "REFRESHING" and st.get("current_step") not in (None, "clone"))
        peak = max(peak, working)
        if all(st.get("status") in TERMINAL_STATES for st in states):
            break
        time.sleep(1.0)
    c.note(f"K5: {accepted} of {count} accepted; at most {peak} refreshing past the clone at once")
    run.meta["k5_peak_concurrent_refreshes"] = peak
    limit = run.args.max_concurrent_refreshes
    with rec.check(f"K5: concurrent refreshes are limited ({'limit ' + str(limit) if limit else 'no limit given'})", "B5",
                   advisory=limit is None):
        expect(limit is not None and peak <= limit,
               f"{peak} of {count} refreshes ran past the clone at the same time" + ("" if limit else "; no limit is applied"))


# ── Group S-8: eligibility (A2, A4) ──────────────────────────────────────────


def group_s8_eligibility(run: Run, chain: ChainState) -> None:
    c, rec, api, client = run.console, run.rec, run.api, run.api.client
    c.heading("S-8  refresh requests that must be refused")

    missing = f"codekb-e2e-no-such-space-{run.run_id}"
    unknown = client.post(f"/api/spaces/{q(missing)}/refresh", timeout=60)
    c.note(f"unknown Space: HTTP {unknown.status} {unknown.preview(160)}")
    if chain.space_id:
        outsider = ScanningClient(client.base_url, user_identity=f"codekb-e2e-outsider-{run.run_id}@codekb.test")
        denied = outsider.post(f"/api/spaces/{q(chain.space_id)}/refresh", timeout=60)
        c.note(f"identity without repository access: HTTP {denied.status} {denied.preview(160)}")
        with rec.check("S-8: a user without access cannot refresh", "A2"):
            expect(denied.status in (401, 403, 404), f"HTTP {denied.status}")
        with rec.check("S-8: a user without access can't tell the Space exists", "A2"):
            expect((denied.status, denied.text) == (unknown.status, unknown.text.replace(missing, chain.space_id)),
                   f"unknown Space: {unknown.status} {unknown.preview(120)}; existing Space: {denied.status} {denied.preview(120)}")

    def refused_with_guidance(label: str, resp, expected: tuple[int, ...]) -> None:
        detail = ""
        try:
            detail = str((resp.json() or {}).get("detail") or resp.text)
        except CheckError:
            detail = resp.text
        with rec.check(f"S-8: refresh of {label} is refused", "A4"):
            expect(resp.status in expected, f"HTTP {resp.status}: {detail[:200]}")
        with rec.check(f"S-8: the refusal for {label} says why and what to do", "A4"):
            expect(detail.strip(), "empty message")
            expect(GUIDANCE.search(detail), f"no next step in: {detail[:300]}")

    # A Space whose first index failed: a branch that doesn't exist.
    with rec.check("S-8: create a Space whose first index fails"):
        sid = api.create_space(run.args.fixture_repo, run.branch("no-such-branch"), f"e2e-{run.run_id}-failed", summarize=False)
        run.spaces.append(sid)
        st = api.wait(sid, "failed index")
        expect(st.get("status") == "FAILED", f"expected FAILED, got {st.get('status')}")
    if rec.passed("S-8: create a Space whose first index fails"):
        refused_with_guidance("a Space whose first index failed", api.refresh(run.spaces[-1]), (409,))

    # An upload Space.
    cfg = expect_dict(client.get("/api/config", timeout=60))
    if not cfg.get("zip_upload_enabled"):
        rec.skip("S-8: refresh of an upload Space is refused", "zip upload is disabled", "A4")
        return
    sid = None
    with rec.check("S-8: create an upload Space"):
        resp = expect_status(client.post("/api/spaces/upload-url",
                                         json_body={"space_name": f"e2e-{run.run_id}-upload", "summarize": False}, timeout=60), 200)
        body = expect_dict(resp)
        sid = body["space_id"]
        run.spaces.append(sid)
        expect_status(raw_put(body["upload_url"], build_sample_zip(), "application/zip"), 200)
        expect_status(client.post("/api/spaces/upload", json_body={"space_id": sid, "space_name": f"e2e-{run.run_id}-upload",
                                                                  "summarize": False}, timeout=120), 201)
        st = api.wait(sid, "upload index")
        expect(st.get("status") == "READY", f"ended {st.get('status')}")
    if sid and rec.passed("S-8: create an upload Space"):
        refused_with_guidance("an upload Space", api.refresh(sid), (400,))


# ── Teardown and report ──────────────────────────────────────────────────────


def teardown(run: Run) -> list[str]:
    c = run.console
    c.heading("Teardown")
    leftovers: list[str] = []
    if run.args.keep:
        c.note("--keep: leaving Spaces and branches in place")
        return [*run.spaces, *run.branches]
    if run.hyperspace_id:
        try:
            r = run.api.client.delete(f"/api/hyperspaces/{q(run.hyperspace_id)}", timeout=120)
            if r.status not in (200, 202, 204, 404):
                leftovers.append(f"hyperspace {run.hyperspace_id}")
        except Exception as exc:
            leftovers.append(f"hyperspace {run.hyperspace_id}")
            c.warn(f"delete hyperspace failed: {exc}")
    for ident in run.cleanup_credentials:
        try:
            r = ScanningClient(run.api.client.base_url, user_identity=ident).delete(
                "/api/user-profile/gitcreds/github", timeout=60)
            if r.status not in (200, 204, 404):
                leftovers.append(f"credential of {ident}")
        except Exception:
            leftovers.append(f"credential of {ident}")
    pending = [sid for sid in run.spaces if sid not in run.deleted]
    for sid in pending:
        try:
            status = run.api.delete(sid)
            if status not in (202, 404):
                leftovers.append(sid)
                c.warn(f"delete {sid} returned {status}")
        except Exception as exc:  # keep going: cleanup is best effort, but reported
            leftovers.append(sid)
            c.warn(f"delete {sid} failed: {exc}")
    if run.repo is not None:
        for branch in run.branches:
            try:
                run.repo.delete_remote_branch(branch, missing_ok=True)
            except Exception as exc:
                leftovers.append(branch)
                c.warn(f"delete branch {branch} failed: {exc}")
    c.note(f"deleted {len(run.spaces)} Space(s) and {len(run.branches)} branch(es)"
           + (f"; left behind: {', '.join(leftovers)}" if leftovers else ""))
    return leftovers


def report(run: Run, elapsed: float, leftovers: list[str]) -> int:
    c = run.console
    results = run.rec.results
    exceptions: set[str] = set()
    if run.args.exceptions:
        for entry in json.loads(Path(run.args.exceptions).read_text()):
            exceptions.add(entry["requirement"])
    ratings = rate_requirements(results)
    v, blocking = verdict(ratings, run.harness_failed, exceptions)

    c.heading("Requirements")
    silent = 0
    for req, r in ratings.items():
        if r["rating"] == "Not covered" or r["reason"] == "no checks ran":
            silent += 1
            continue
        color = {"Met": c.green, "Not met": c.red}.get(r["rating"], c.yellow)
        print(f"  {req:<4} {r['priority']:<6} {color(r['rating']):<22} {c.dim(r['reason'][:110])}")
    c.note(f"{silent} requirement(s) had no checks in this run or are not covered; see the JSON report")

    counts = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "SKIP")}
    c.heading("Verdict")
    print(f"  checks: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped "
          f"in {elapsed / 60:.1f} min")
    color = {"READY": c.green, "NOT READY": c.red}.get(v, c.yellow)
    print(f"  verdict: {c.bold(color(v))}")
    if blocking:
        label = "blocking" if v == "NOT READY" else "not verified"
        shown = ", ".join(blocking) if len(blocking) <= 20 else f"{len(blocking)} Must requirements (see JSON report)"
        print(f"  {label}: {shown}")
    if run.harness_failed:
        print(c.yellow("  the harness or fixture failed a setup check; see FAIL lines above"))
    nc = [k for k, r in ratings.items() if r["rating"] == "Not covered" and r["priority"] == "Must"]
    print(f"  needs separate sign-off (Must, not covered): {', '.join(nc)}")
    if exceptions:
        print(f"  shipped with exceptions: {', '.join(sorted(exceptions))}")

    if run.args.report_json:
        payload = {
            "run_id": run.run_id,
            "base_url": run.api.client.base_url,
            "fixture_repo": run.args.fixture_repo,
            "elapsed_s": round(elapsed, 1),
            "verdict": v,
            "blocking": blocking,
            "harness_failed": run.harness_failed,
            "exceptions": sorted(exceptions),
            "requirements": ratings,
            "checks": [r.__dict__ for r in results],
            "meta": run.meta,
            "leftovers": leftovers,
        }
        Path(run.args.report_json).write_text(run.rec.redact(json.dumps(payload, indent=2, default=str)))
        c.note(f"JSON report: {run.args.report_json}")
    return 0 if v == "READY" else 1


# ── Entry point ──────────────────────────────────────────────────────────────


def read_push_token(token_file: str | None) -> tuple[str, str]:
    """The push token and where it came from. Environment first, then a file.

    A token file must be readable only by its owner, like an SSH key; a file
    other users can read is refused rather than used.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token, TOKEN_ENV
    path = token_file or os.environ.get(TOKEN_FILE_ENV)
    if not path:
        return "", ""
    f = Path(path).expanduser()
    if not f.is_file():
        raise SystemExit(f"token file {f} does not exist")
    mode = f.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"token file {f} is readable by other users (mode {mode:o}); run: chmod 600 {f}")
    return f.read_text().strip(), str(f)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Black-box end-to-end test of CodeKB space refresh.")
    p.add_argument("--base-url", default=os.environ.get("CODEKB_BASE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--identity", default=os.environ.get("CODEKB_E2E_IDENTITY", "local-dev"),
                   help="X-User-Identity to send; must have a GitHub credential stored (default: %(default)s)")
    p.add_argument("--token", default=os.environ.get("CODEKB_TOKEN"), help="JWT for a Cognito-enforced deployment")
    p.add_argument("--fixture-repo", default=DEFAULT_FIXTURE_REPO)
    p.add_argument("--token-file", help=f"file holding the push token, mode 600 (or ${TOKEN_FILE_ENV}); "
                   f"used only when ${TOKEN_ENV} is unset")
    p.add_argument("--group", default="C,G,S,R,K",
                   help="groups: C change chain, G availability during refreshes, S history and eligibility, "
                        "R failure and recovery, K concurrency (default: %(default)s)")
    p.add_argument("--max-concurrent-refreshes", type=int, default=None,
                   help="the product's limit on refreshes running at once, if it has one (K5)")
    p.add_argument("--steps", default="", help=f"chain steps to run, e.g. S0-S1 (available: {', '.join(STEPS)})")
    p.add_argument("--analysis-timeout", type=float, default=1800.0)
    p.add_argument("--call-graph-depth", type=int, default=10,
                   help="the backend's analysis.call_graph_depth; the API doesn't expose it (default: %(default)s)")
    p.add_argument("--max-file-size-mb", type=float, default=10.0,
                   help="the backend's analysis.max_file_size_mb, for S18 (default: %(default)s)")
    p.add_argument("--exceptions", help="JSON file of agreed exceptions (design doc 7.4)")
    p.add_argument("--report-json", help="write the JSON report here")
    p.add_argument("--keep", action="store_true", help="leave Spaces and branches for inspection")
    p.add_argument("--no-color", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args(argv)
    console = Console(color=False if args.no_color else None)
    token, token_source = read_push_token(args.token_file)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    workdir = Path(tempfile.mkdtemp(prefix=f"codekb-e2e-{run_id}-"))
    repo = FixtureRepo(args.fixture_repo, token, workdir / "repo") if token else None
    redact = repo.redact if repo else (lambda s: s)
    scanner = LeakScanner(repo._secrets if repo else ())
    ScanningClient.scanner = scanner
    client = ScanningClient(args.base_url, token=args.token, user_identity=None if args.token else args.identity)
    run = Run(
        args=args, console=console, rec=Recorder(console, redact), api=CodeKB(client, console, args.analysis_timeout),
        repo=repo, run_id=run_id, queries=fx.search_queries(),
        groups={g.strip().upper() for g in args.group.split(",") if g.strip()}, scanner=scanner,
    )
    run.mcp = McpClient(client)
    steps = parse_steps(args.steps)

    console.line(console.bold("CodeKB space refresh end-to-end test"))
    console.note(f"target   : {client.base_url} as {'JWT' if args.token else args.identity}")
    console.note(f"fixture  : {args.fixture_repo} under {BRANCH_PREFIX}{run_id}/")
    console.note(f"groups   : {', '.join(sorted(run.groups))}")
    console.note(f"steps    : {', '.join(steps)}")
    console.note(f"push auth: {token_source or 'none'}")

    start = time.time()
    leftovers: list[str] = []
    try:
        if preflight(run):
            chain = ChainState()
            if "C" in run.groups or run.groups & {"G", "S", "R", "K"}:
                for name in (steps if "C" in run.groups else ["S0", "S1"]):
                    STEPS[name](run, chain)
            if "S" in run.groups:
                group_s_history(run, chain)
            if "R" in run.groups:
                group_r_failures(run, chain)
            if "K" in run.groups:
                group_k_concurrency(run)
                group_k3_overviews(run, chain)
                group_k5_many(run)
            if "S" in run.groups:
                group_s8_eligibility(run, chain)
            if "R" in run.groups:
                group_r3_credentials(run)
                group_r4_leaks(run)
    except KeyboardInterrupt:
        console.warn("interrupted")
    finally:
        leftovers = teardown(run)
        shutil.rmtree(workdir, ignore_errors=True)
    return report(run, time.time() - start, leftovers)


if __name__ == "__main__":
    sys.exit(main())
