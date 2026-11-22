#!/usr/bin/env python3
#
# ── Invocation cheat sheet ────────────────────────────────────────────────────
# Run from anywhere; only stdlib is required (python3). Full flag reference:
#   python3 codekb_regression.py --help
#
# The whole suite in one pass — this is the normal run. Everything except the
# two slow Overview phases executes, including summarization (so semantic search
# is asserted for real), the agent, and the REST + MCP authorization checks. The
# suite detects whether the deployment enforces authz and asserts accordingly,
# so this same command passes whether authz is on or off.
#   python3 codekb_regression.py --base-url http://localhost:8000
#
# Add the heavy Space + Hyperspace Overview generation (Bedrock, tens of minutes)
#   python3 codekb_regression.py --base-url http://localhost:8000 --with-overviews
#
# Keep created spaces/hyperspaces instead of deleting them at the end
#   python3 codekb_regression.py --base-url http://localhost:8000 --keep
#
# Fast no-cost subset (read-only, makes no writes); phases, in order:
#   readonly, negative, headers, git-space, git-credentials, queries, authz,
#   upload, hyperspaces, mcp, refresh, agent, overview, hyperspace-overview
#   python3 codekb_regression.py --base-url http://localhost:8000 --phase readonly,negative,headers
#
# Reuse an already-analyzed space to iterate quickly on one phase (never deleted)
#   python3 codekb_regression.py --base-url http://localhost:8000 --phase agent --reuse-space my-repo-a1b2c3d4
#
# A different repository needs a matching semantic-search query
#   python3 codekb_regression.py --base-url http://localhost:8000 \
#       --git-url https://github.com/org/repo.git --search-query 'parse an incoming webhook payload'
#
# Private-repo clone via the git-credentials phase (stores a PAT, clones a private repo)
#   python3 codekb_regression.py --base-url http://localhost:8000 \
#       --github-pat-token ghp_xxx --github-username <user> \
#       --github-private-git-url https://github.com/<org>/<private-repo>.git
#
# Against a Cognito/JWT-enforced deployment (X-User-Identity is ignored there).
# --second-token is a JWT for a DIFFERENT principal, used by the authz/MCP-authz checks.
#   python3 codekb_regression.py --base-url https://codekb.example.com \
#       --token "$CODEKB_JWT" --second-token "$CODEKB_JWT_OTHER"
#
# Every flag also reads an env var: CODEKB_BASE_URL, CODEKB_TOKEN, CODEKB_SECOND_TOKEN,
# CODEKB_USER_IDENTITY, CODEKB_GITHUB_PRIVATE_GIT_URL, CODEKB_GITHUB_USERNAME, etc.
# ──────────────────────────────────────────────────────────────────────────────
"""CodeKB backend regression suite.

Standalone, dependency-free (stdlib only) black-box test harness. Point it at a
running CodeKB container and it exercises every REST endpoint and every backend
feature, then prints a PASS/FAIL report and exits non-zero if anything broke.

    # the whole suite in one pass (everything except the slow overviews):
    python3 codekb_regression.py --base-url http://localhost:8000

    # add Space + Hyperspace Overview generation (heavy Bedrock, tens of minutes):
    python3 codekb_regression.py --base-url http://localhost:8000 --with-overviews

    # also exercise the git-credentials phase (store a PAT, clone a private repo):
    python3 codekb_regression.py --base-url http://localhost:8000 --github-pat-token ghp_xxx --github-username <user> --github-private-git-url https://github.com/<org>/<private-repo>.git

There is one default run: every phase executes in a single pass, including
analysis-time summarization (so semantic search is asserted against real
vectors), the agent, and the REST + MCP authorization checks. Only the two
Overview-generation phases are held back, because they are slow and make heavy
Bedrock calls; add --with-overviews to include them. The suite adapts to the
deployment it is pointed at: it detects whether authorization is enforced and
asserts the matching outcome, so the same command passes whether authz is on or
off.

Useful flags:

  --with-overviews   Also run Space + Hyperspace Overview generation (slow).
  --reuse-space ID   Skip creating a git space and run the read/query/agent
                     checks against an existing READY space. Fast iteration,
                     and the reused space is never deleted.
  --keep             Don't delete the spaces/hyperspaces created by the run.
  --phase a,b        Run only the named phases (see --help for the list); handy
                     for a fast no-cost subset, e.g. --phase readonly,negative,headers.
                     Combine with --reuse-space when a phase needs a space.

The container must be reachable; anything past the read-only checks needs real
AWS credentials plus S3 / S3 Vectors / Bedrock access.

Auth: by default the script sends `X-User-Identity`, which the backend accepts
only when it runs with CODEKB_REQUIRE_AUTH=false (local dev). Against a
Cognito-enforced deployment pass `--token <JWT>` instead.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# The four Space Overview documents the backend generates.
# Mirrors OVERVIEW_DOCUMENTS in codekb/vector/space_overview_generator.py.
OVERVIEW_DOC_KEYS = [
    "business_overview",
    "application_architecture",
    "technology_stack",
    "deployment_architecture",
]

# The two Hyperspace Overview (summary-of-summaries) documents the backend
# synthesizes from its member spaces' Space Overviews. Mirrors
# HYPERSPACE_OVERVIEW_DOCUMENTS in codekb/vector/hyperspace_overview_generator.py.
HYPERSPACE_OVERVIEW_DOC_KEYS = [
    "system_overview",
    "space_interconnections",
]

# Small Java/Maven sample: fast to clone, real classes and methods for the
# parser, and a narrow enough domain (CSR signing via a KMS asymmetric key) that
# semantic-search hits are easy to eyeball for relevance.
DEFAULT_GIT_URL = "https://github.com/aws-samples/csr-signing-using-kms.git"

# Default semantic-search query, phrased for DEFAULT_GIT_URL's domain. Override
# with --search-query when pointing --git-url at a different repository.
DEFAULT_SEARCH_QUERY = "sign a certificate signing request with a KMS asymmetric key"

# A space ID that must never exist, for negative-path checks.
MISSING_SPACE_ID = "codekb-regression-no-such-space-0000"
MISSING_HYPERSPACE_ID = "codekb-regression-no-such-hyperspace-0000"


# ── Result model and console output ──────────────────────────────────────────


class CheckError(Exception):
    """An assertion inside a check failed."""


class Skipped(Exception):
    """Raised inside a check body to record it as skipped rather than failed."""


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    duration: float
    detail: str = ""


class Console:
    def __init__(self, color: bool | None = None) -> None:
        if color is None:
            color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.color = color

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def green(self, t: str) -> str:
        return self._c("32", t)

    def red(self, t: str) -> str:
        return self._c("31", t)

    def yellow(self, t: str) -> str:
        return self._c("33", t)

    def dim(self, t: str) -> str:
        return self._c("2", t)

    def bold(self, t: str) -> str:
        return self._c("1", t)

    def heading(self, text: str) -> None:
        print()
        print(self.bold(f"── {text} " + "─" * max(0, 68 - len(text))))

    def line(self, text: str = "") -> None:
        print(text)

    def note(self, text: str) -> None:
        print(self.dim(f"    {text}"))

    def warn(self, text: str) -> None:
        print(self.yellow(f"  !  {text}"))


# ── Minimal HTTP client (stdlib only) ────────────────────────────────────────


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except Exception as exc:
            raise CheckError(
                f"response body is not valid JSON ({exc}): {self.preview()}"
            ) from exc

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def preview(self, limit: int = 300) -> str:
        text = self.text.replace("\n", " ").strip()
        return text[:limit] + ("…" if len(text) > limit else "")


class Client:
    """Thin wrapper over urllib that never raises on HTTP status."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        user_identity: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_identity = user_identity
        self.timeout = timeout

    def auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.user_identity:
            return {"X-User-Identity": self.user_identity}
        return {}

    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> Response:
        url = self.url(path, params)
        headers = {"Accept": "application/json", **self.auth_headers()}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:  # nosec B310
                return Response(
                    resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
                )
        except urllib.error.HTTPError as exc:
            return Response(
                exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()
            )
        except urllib.error.URLError as exc:
            raise CheckError(f"could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CheckError(f"request to {url} timed out") from exc

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Response:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> Response:
        return self.request("DELETE", path, **kw)

    def open_stream(self, method: str, path: str, json_body: Any = None, timeout: float = 300.0):
        """Open a streaming response (for SSE). Caller must close it."""
        url = self.url(path)
        headers = {"Accept": "text/event-stream", **self.auth_headers()}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout)  # nosec B310
        except urllib.error.HTTPError as exc:
            raise CheckError(
                f"stream request failed with {exc.code}: {exc.read().decode(errors='replace')[:300]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CheckError(f"could not reach {url}: {exc.reason}") from exc


class McpClient:
    """Minimal MCP client over the streamable-HTTP transport.

    MCP is the primary way agents consume CodeKB, and some capabilities exist
    *only* as tools — ``get_call_graph_with_source`` has no HTTP route, which is
    why an HTTP-only suite could not reach the code path that reads source from
    disk. This speaks just enough JSON-RPC to initialize, list tools, and call
    them.

    The server is mounted with ``stateless_http=True``, so there is no session to
    establish or carry: each request stands alone and ``Mcp-Session-Id`` is not
    required. The endpoint is ``/mcp/mcp`` — the FastAPI mount prefix plus the
    transport's own path.

    Responses arrive either as JSON or as a one-event SSE stream depending on the
    server's content negotiation, so both are unwrapped here.
    """

    ENDPOINT = "/mcp/mcp"
    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, client: Client) -> None:
        self._client = client
        self._next_id = 0

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, timeout: float) -> Any:
        """Send one JSON-RPC request and return its ``result``.

        Raises:
            CheckError: On transport failure, a non-200 status, an unparseable
                body, or a JSON-RPC error object.
        """
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params

        url = self._client.base_url + self.ENDPOINT
        headers = {
            # Streamable HTTP requires the client to accept both.
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.PROTOCOL_VERSION,
            **self._client.auth_headers(),
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise CheckError(f"{method} failed with HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CheckError(f"could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CheckError(f"{method} timed out after {timeout}s") from exc

        if status != 200:
            raise CheckError(f"{method} returned HTTP {status}")

        payload = self._unwrap(raw, method)
        if "error" in payload:
            err = payload["error"]
            raise CheckError(f"{method} returned JSON-RPC error {err.get('code')}: {err.get('message')}")
        if "result" not in payload:
            raise CheckError(f"{method} response had neither result nor error: {str(payload)[:200]}")
        return payload["result"]

    @staticmethod
    def _unwrap(raw: bytes, method: str) -> dict:
        """Parse a JSON body, or pull the JSON out of an SSE ``data:`` event."""
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise CheckError(f"{method} returned an empty body")
        if not text.startswith("{"):
            # SSE: one or more "event:"/"data:" lines. The last data line wins.
            data_lines = [
                line[len("data:") :].strip()
                for line in text.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                raise CheckError(f"{method} returned an unrecognized body: {text[:200]}")
            text = data_lines[-1]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CheckError(f"{method} returned invalid JSON: {text[:200]}") from exc
        if not isinstance(payload, dict):
            raise CheckError(f"{method} returned {type(payload).__name__}, expected an object")
        return payload

    def initialize(self, timeout: float = 60.0) -> dict:
        """Perform the MCP handshake and return the server's capabilities."""
        return self._rpc(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "codekb-regression", "version": "1.0"},
            },
            timeout=timeout,
        )

    def list_tools(self, timeout: float = 60.0) -> list[dict]:
        result = self._rpc("tools/list", {}, timeout=timeout)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise CheckError(f"tools/list returned no tool array: {str(result)[:200]}")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = 180.0) -> str:
        """Call a tool and return its text content joined into one string.

        Every CodeKB tool returns text (JSON or formatted), so collapsing the
        content blocks is lossless here and keeps assertions simple.
        """
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if result.get("isError"):
            raise CheckError(f"tool {name} reported isError: {str(result)[:300]}")
        blocks = result.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise CheckError(f"tool {name} returned no content: {str(result)[:200]}")
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            raise CheckError(f"tool {name} returned no text content: {str(blocks)[:200]}")
        return "\n".join(texts)


def raw_put(url: str, body: bytes, content_type: str, timeout: float = 300.0) -> Response:
    """PUT raw bytes to an absolute URL with no auth headers (presigned S3 upload)."""
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="PUT"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return Response(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            )
    except urllib.error.HTTPError as exc:
        return Response(
            exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()
        )
    except urllib.error.URLError as exc:
        raise CheckError(f"presigned upload to S3 failed: {exc.reason}") from exc


def raw_preflight(
    url: str,
    origin: str,
    request_method: str,
    request_headers: str,
    timeout: float = 30.0,
) -> Response:
    """Send a browser-style CORS preflight (OPTIONS) to an absolute URL.

    Mirrors what a browser does before the cross-origin zip PUT: an
    unauthenticated OPTIONS carrying Origin and Access-Control-Request-*. S3
    answers preflights without validating the presigned signature, so the URL's
    query string is irrelevant here. A bucket with no matching CORS rule answers
    403 and omits Access-Control-Allow-Origin — which is exactly what makes the
    SPA upload fail with "Failed to fetch". A raw_put() skips the preflight, so
    it cannot see this; this can.
    """
    req = urllib.request.Request(
        url,
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": request_method,
            "Access-Control-Request-Headers": request_headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return Response(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            )
    except urllib.error.HTTPError as exc:
        return Response(
            exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read()
        )
    except urllib.error.URLError as exc:
        raise CheckError(f"CORS preflight to S3 failed: {exc.reason}") from exc


# ── Assertions ───────────────────────────────────────────────────────────────


def expect(condition: Any, message: str) -> None:
    if not condition:
        raise CheckError(message)


def expect_status(resp: Response, *allowed: int) -> Response:
    if resp.status not in allowed:
        wanted = " or ".join(str(s) for s in allowed)
        raise CheckError(f"expected HTTP {wanted}, got {resp.status} — {resp.preview()}")
    return resp


def expect_dict(resp: Response) -> dict:
    payload = resp.json()
    expect(isinstance(payload, dict), f"expected a JSON object, got {type(payload).__name__}")
    return payload


def expect_list(resp: Response) -> list:
    payload = resp.json()
    expect(isinstance(payload, list), f"expected a JSON array, got {type(payload).__name__}")
    return payload


def expect_keys(payload: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in payload]
    expect(not missing, f"missing expected field(s): {', '.join(missing)}")


_WORK_DIR_MARKERS = ("-analysis/", "-refresh/")


def expect_no_working_tree_path(text: str, where: str) -> None:
    """Fail if ``text`` contains a path naming a pipeline working tree.

    Analysis and refresh run in disposable trees (``<space_id>-analysis``,
    ``<space_id>-refresh``) that are deleted when the pipeline finishes. A stored
    component path naming one is a dead pointer: nothing recreates that directory,
    so every later read of the component fails. This shipped once undetected,
    because nothing in this suite asserted on paths at all.
    """
    for marker in _WORK_DIR_MARKERS:
        if marker in text:
            raise CheckError(
                f"{where} names the {marker.strip('-/')} working tree, which is deleted "
                f"when the pipeline ends — stored paths must name the durable cache location"
            )


def expect_real_source(text: str, where: str) -> None:
    """Fail if ``text`` carries a swallowed read error instead of source code.

    The call-graph reader turns a failed ``open`` into its own return value, so a
    broken source read surfaces as a successful response whose body is the error
    message. Checking the status code cannot detect that; only the content can.
    """
    if "Error reading source" in text:
        excerpt = text[text.index("Error reading source") :][:200]
        raise CheckError(f"{where} returned a read error as content: {excerpt}")
    if "Source not available" in text:
        raise CheckError(f"{where} could not read source for at least one component")


def expect_authz_denied(resp: Response, status: int, code: str) -> dict:
    """Assert an authorization denial: the HTTP status plus the body error code.

    Authz failures are rendered by ``authz_exception_handler`` as
    ``{"error": <code>, "message": ...}`` (the key is ``error``, not FastAPI's
    ``detail``), so checking only the status would let a same-status failure
    raised elsewhere pass as an authz denial. A 503 ``authorization_unavailable``
    is reported through the same handler; callers that can legitimately hit it
    (the git-permission path when a provider is unreachable) should catch it
    before calling this and downgrade to a skip.
    """
    expect_status(resp, status)
    body = expect_dict(resp)
    got = body.get("error")
    expect(
        got == code,
        f"expected authz error code {code!r}, got {got!r} (status {resp.status}) — {resp.preview()}",
    )
    return body


def expect_unknown_space(ctx: Context, resp: Response, *allowed_when_open: int) -> None:
    """Assert the response for a space-scoped route hit with an unknown space id.

    The right answer depends on the deployment, which is why this is detected
    once (preflight) rather than hard-coded. When authorization is enforced the
    read gate denies an unknown space with 401 ``read_denied`` *before* the route
    runs — it will not confirm whether the space exists — so the route's own
    404/422/200 never appears. With authorization off, the route's own status
    stands.
    """
    if ctx.authz_enforced:
        expect_authz_denied(resp, 401, "read_denied")
    else:
        expect_status(resp, *allowed_when_open)


# ── Runner ───────────────────────────────────────────────────────────────────


@dataclass
class Context:
    """Shared state threaded through the phases."""

    client: Client
    console: Console
    args: argparse.Namespace
    results: list[Result] = field(default_factory=list)

    # Discovered environment
    allowed_git_hosts: list[str] = field(default_factory=list)
    default_top_k: int = 10
    infra_ready: bool = False
    allow_credentials_in_git_url: bool = False

    # Resources created by this run
    git_space_id: str | None = None
    upload_space_id: str | None = None
    hyperspace_id: str | None = None
    authz_space_id: str | None = None
    created_git_space: bool = False

    # Facts learned along the way
    git_space_ready: bool = False
    upload_space_ready: bool = False
    component_name: str | None = None
    vectors_available: bool = False
    # None until the authz phase probes the instance; True/False once it knows
    # whether CODEKB_REQUIRE_AUTHORIZATION is on.
    authz_enforced: bool | None = None

    @contextmanager
    def check(self, name: str) -> Iterator[None]:
        """Run one check, recording PASS / FAIL / SKIP. Never propagates.

        Raise ``Skipped`` inside the body to downgrade a check to SKIP (for a
        precondition that legitimately doesn't hold on this instance); raise
        ``CheckError`` — which every ``expect_*`` helper does — to fail it.
        """
        start = time.time()
        try:
            yield
        except Skipped as exc:
            self._record(name, "SKIP", time.time() - start, str(exc))
        except CheckError as exc:
            self._record(name, "FAIL", time.time() - start, str(exc))
        except Exception as exc:  # unexpected harness/server error
            self._record(
                name, "FAIL", time.time() - start, f"unexpected {type(exc).__name__}: {exc}"
            )
        else:
            self._record(name, "PASS", time.time() - start)

    def skip(self, name: str, reason: str) -> None:
        """Record a check as skipped without running it."""
        self._record(name, "SKIP", 0.0, reason)

    def _record(self, name: str, status: str, duration: float, detail: str = "") -> None:
        self.results.append(Result(name, status, duration, detail))
        c = self.console
        tag = {"PASS": c.green("PASS"), "FAIL": c.red("FAIL"), "SKIP": c.yellow("SKIP")}[status]
        timing = c.dim(f"{duration:6.2f}s") if duration >= 0.01 else c.dim("      -")
        print(f"  [{tag}] {timing}  {name}")
        if detail:
            print(f"           {c.dim(detail) if status == 'SKIP' else c.red(detail)}")


# ── Polling helpers ──────────────────────────────────────────────────────────


def expect_vector_hits(hits: list, *, space_ids: set[str] | None = None) -> list[float]:
    """Validate the shape and ranking of semantic-search results.

    Verified against a live S3 Vectors index; both the scoped
    (/spaces/{id}/search) and unscoped (/search) routes return this shape:

        {"distance": 0.509,
         "key": "<space_id>::<component>",
         "metadata": {"space_id", "component_id", "description", "type",
                      "file_path", "line_number", "business_relevance_*"}}

    Asserting on ``metadata.description`` matters: that text is what actually
    got embedded, so an empty one means summarization wrote vectors with no
    content behind them — search would appear to work while returning noise.

    Pass ``space_ids`` to assert the metadata filter held; that is the control
    that stops one space's results leaking into another's search.

    Returns the distances, nearest first.
    """
    expect(hits, "semantic search returned no hits")
    distances: list[float] = []
    for i, hit in enumerate(hits):
        expect(isinstance(hit, dict), f"hit {i} is not an object")
        expect_keys(hit, "distance", "key", "metadata")
        expect(
            isinstance(hit["distance"], (int, float)) and not isinstance(hit["distance"], bool),
            f"hit {i} distance is not numeric: {hit['distance']!r}",
        )
        distances.append(float(hit["distance"]))
        meta = hit["metadata"]
        expect(isinstance(meta, dict), f"hit {i} metadata is not an object")
        expect_keys(meta, "space_id", "component_id", "description")
        expect(
            str(meta["description"]).strip(),
            f"hit {i} ({hit['key']}) has an empty description — the vector index "
            f"holds an embedding with no summary text behind it",
        )
        if space_ids is not None:
            expect(
                meta["space_id"] in space_ids,
                f"hit {i} leaked from space {meta['space_id']!r}; the search was "
                f"scoped to {sorted(space_ids)}",
            )
    # S3 Vectors returns k-NN ordered nearest-first and the client does not
    # re-sort, so a break in monotonicity means ranking got mangled en route.
    expect(
        all(a <= b for a, b in zip(distances, distances[1:])),
        f"hits are not ranked nearest-first by distance: {distances}",
    )
    return distances


def read_space_detail(ctx: Context, space_id: str) -> dict:
    """GET /api/spaces/{id} — the authoritative source for created_by and vector_count.

    Do NOT read those two from GET /{id}/status: that route declares
    response_model=AnalysisStatus, and FastAPI strips any field the model doesn't
    declare. AnalysisStatus omits both, so they are always absent there even when
    status.json holds them. Only SpaceDetail (this route) and SpaceSummary
    (the list route) expose them.
    """
    resp = ctx.client.get(f"/api/spaces/{urllib.parse.quote(space_id)}", timeout=120)
    expect_status(resp, 200)
    return expect_dict(resp)


def poll_space_status(
    ctx: Context,
    space_id: str,
    *,
    terminal: tuple[str, ...] = ("READY", "FAILED"),
    timeout: float,
    interval: float = 5.0,
    label: str = "analysis",
    gone_on_denied: bool = False,
) -> dict:
    """Poll GET /api/spaces/{id}/status until a terminal state or timeout.

    The backend deliberately never flips a long-running job to FAILED (it only
    adds a `warning`), so the timeout here is the only bound on the wait.

    With ``gone_on_denied`` (used after a delete on an authz-enforced
    deployment), a 401 read_denied is treated as the space being gone: once
    status.json is removed the read gate finds no record and denies the read
    instead of reporting NOT_FOUND. It is returned as a synthetic NOT_FOUND.
    """
    deadline = time.time() + timeout
    last_render = ""
    while True:
        resp = ctx.client.get(f"/api/spaces/{urllib.parse.quote(space_id)}/status")
        if gone_on_denied and resp.status == 401 and "read_denied" in resp.text:
            ctx.console.note(f"{label}: gone (read_denied)")
            return {"status": "NOT_FOUND"}
        expect_status(resp, 200)
        status = expect_dict(resp)
        state = status.get("status", "UNKNOWN")

        step = status.get("current_step") or "-"
        done, total = status.get("summarize_done"), status.get("summarize_total")
        progress = f" {done}/{total}" if done is not None and total is not None else ""
        render = f"{state} step={step}{progress}"
        if render != last_render:
            ctx.console.note(f"{label}: {render}")
            last_render = render

        if state in terminal:
            return status
        if state == "NOT_FOUND":
            raise CheckError(f"space '{space_id}' disappeared while polling {label}")
        if time.time() >= deadline:
            raise CheckError(
                f"{label} did not reach {' or '.join(terminal)} within {timeout:.0f}s "
                f"(last state: {render})"
            )
        time.sleep(interval)


def poll_overview_status(ctx: Context, space_id: str, *, timeout: float, interval: float = 10.0) -> dict:
    """Poll until overview_status leaves the in-progress state."""
    deadline = time.time() + timeout
    last_render = ""
    while True:
        resp = ctx.client.get(f"/api/spaces/{urllib.parse.quote(space_id)}/status")
        expect_status(resp, 200)
        status = expect_dict(resp)
        state = (status.get("overview_status") or "").lower()
        done, total = status.get("overview_done"), status.get("overview_total")
        progress = f" {done}/{total}" if done is not None and total is not None else ""
        render = f"{state or 'none'}{progress}"
        if render != last_render:
            ctx.console.note(f"overview: {render}")
            last_render = render

        if state in ("completed", "failed"):
            return status
        if time.time() >= deadline:
            raise CheckError(
                f"overview generation did not finish within {timeout:.0f}s (last: {render})"
            )
        time.sleep(interval)


def poll_hyperspace_overview_status(
    ctx: Context, hyperspace_id: str, *, timeout: float, interval: float = 10.0
) -> dict:
    """Poll until a hyperspace's overview_status leaves the in-progress state.

    Unlike spaces, a hyperspace has no /status subroute: progress lives on the
    hyperspace detail itself (GET /api/hyperspaces/{id}), which carries the same
    overview_status / overview_done / overview_total fields the service writes.
    """
    deadline = time.time() + timeout
    last_render = ""
    while True:
        resp = ctx.client.get(f"/api/hyperspaces/{urllib.parse.quote(hyperspace_id)}")
        expect_status(resp, 200)
        detail = expect_dict(resp)
        state = (detail.get("overview_status") or "").lower()
        done, total = detail.get("overview_done"), detail.get("overview_total")
        progress = f" {done}/{total}" if done is not None and total is not None else ""
        render = f"{state or 'none'}{progress}"
        if render != last_render:
            ctx.console.note(f"hyperspace overview: {render}")
            last_render = render

        if state in ("completed", "failed"):
            return detail
        if time.time() >= deadline:
            raise CheckError(
                f"hyperspace overview generation did not finish within {timeout:.0f}s "
                f"(last: {render})"
            )
        time.sleep(interval)


# ── Sample source archive for the upload flow ───────────────────────────────


def build_sample_zip() -> bytes:
    """Build a small multi-language source archive in memory.

    Deliberately contains functions, classes, methods, and module-level
    variables in more than one language so the parser and component catalog get
    a real workout without needing a checkout on disk.
    """
    files = {
        "sample-app/README.md": (
            "# Sample App\n\n"
            "Synthetic project generated by the CodeKB regression suite.\n"
            "It models a tiny order-processing service.\n"
        ),
        "sample-app/orders/__init__.py": "",
        "sample-app/orders/models.py": (
            '"""Order domain models."""\n\n'
            "DEFAULT_CURRENCY = \"USD\"\n"
            "MAX_ITEMS_PER_ORDER = 50\n\n\n"
            "class Money:\n"
            '    """An amount in a single currency."""\n\n'
            "    def __init__(self, amount, currency=DEFAULT_CURRENCY):\n"
            "        self.amount = amount\n"
            "        self.currency = currency\n\n"
            "    def add(self, other):\n"
            '        """Add two Money values of the same currency."""\n'
            "        if other.currency != self.currency:\n"
            "            raise ValueError(\"currency mismatch\")\n"
            "        return Money(self.amount + other.amount, self.currency)\n\n\n"
            "class Order:\n"
            '    """A customer order made up of line items."""\n\n'
            "    def __init__(self, order_id, customer_id):\n"
            "        self.order_id = order_id\n"
            "        self.customer_id = customer_id\n"
            "        self.items = []\n\n"
            "    def add_item(self, sku, quantity, unit_price):\n"
            '        """Append a line item, enforcing the per-order cap."""\n'
            "        if len(self.items) >= MAX_ITEMS_PER_ORDER:\n"
            "            raise ValueError(\"too many items\")\n"
            "        self.items.append({\"sku\": sku, \"quantity\": quantity, \"unit_price\": unit_price})\n\n"
            "    def total(self):\n"
            '        """Sum every line item into a single Money value."""\n'
            "        running = Money(0)\n"
            "        for item in self.items:\n"
            "            running = running.add(Money(item[\"unit_price\"] * item[\"quantity\"]))\n"
            "        return running\n"
        ),
        "sample-app/orders/service.py": (
            '"""Order processing service."""\n\n'
            "from .models import Order\n\n\n"
            "def validate_order(order):\n"
            '    """Reject orders with no line items."""\n'
            "    return bool(order.items)\n\n\n"
            "def apply_discount(order, percent):\n"
            '    """Reduce an order total by a percentage."""\n'
            "    total = order.total()\n"
            "    return total.amount * (1 - percent / 100.0)\n\n\n"
            "class OrderService:\n"
            '    """Coordinates validation, pricing, and submission."""\n\n'
            "    def __init__(self, repository):\n"
            "        self.repository = repository\n\n"
            "    def place_order(self, customer_id, items):\n"
            '        """Build, validate, and persist an order."""\n'
            "        order = Order(order_id=\"generated\", customer_id=customer_id)\n"
            "        for sku, qty, price in items:\n"
            "            order.add_item(sku, qty, price)\n"
            "        if not validate_order(order):\n"
            "            raise ValueError(\"empty order\")\n"
            "        return self.repository.save(order)\n"
        ),
        "sample-app/web/checkout.js": (
            "// Checkout front-end helpers.\n"
            "const TAX_RATE = 0.08;\n\n"
            "function formatMoney(amount, currency) {\n"
            "  return `${currency} ${amount.toFixed(2)}`;\n"
            "}\n\n"
            "function calculateTax(subtotal) {\n"
            "  return subtotal * TAX_RATE;\n"
            "}\n\n"
            "class CheckoutForm {\n"
            "  constructor(element) {\n"
            "    this.element = element;\n"
            "  }\n\n"
            "  submit(payload) {\n"
            "    return fetch('/api/orders', { method: 'POST', body: JSON.stringify(payload) });\n"
            "  }\n"
            "}\n\n"
            "module.exports = { formatMoney, calculateTax, CheckoutForm };\n"
        ),
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


# ── Phase 0: preflight ───────────────────────────────────────────────────────


def phase_preflight(ctx: Context) -> None:
    """Confirm the service is reachable and usable before anything else.

    Returns normally only if the run can continue; raises SystemExit otherwise,
    because every later phase would just produce noise.
    """
    c, client = ctx.console, ctx.client
    c.heading("Preflight")

    # Reachability first: a wrong --base-url or a container that isn't up should
    # produce one clear sentence, not a wall of identical failures.
    try:
        client.get("/api/health", timeout=15)
    except CheckError as exc:
        c.line()
        c.line(c.red(f"Cannot reach the CodeKB API at {client.base_url}"))
        c.line(f"  {exc}")
        c.line(
            "\nCheck that the container is running and the port is published, e.g.\n"
            "  docker run -p 8000:8000 ... codekb\n"
            "then pass --base-url http://localhost:8000"
        )
        raise SystemExit(2) from None

    with ctx.check("GET /api/health returns ok"):
        resp = expect_status(client.get("/api/health", timeout=15), 200)
        payload = expect_dict(resp)
        expect_keys(payload, "service", "version", "status")
        expect(payload["status"] == "ok", f"status is {payload['status']!r}, expected 'ok'")

    # Probe an authenticated endpoint to classify the auth mode. This drives a
    # clear early exit instead of every later check failing identically.
    probe = client.get("/api/config", timeout=60)
    if probe.status == 503:
        c.line()
        c.line(c.red("Every /api/* request is returning 503."))
        c.line(
            "The backend is running with CODEKB_REQUIRE_AUTH=true but cannot enforce auth\n"
            "(CODEKB_COGNITO_USER_POOL_ID or CODEKB_COGNITO_APP_CLIENT_IDS missing, or JWKS\n"
            "failed to load), so it fails closed. Restart the container with\n"
            "CODEKB_REQUIRE_AUTH=false for local testing, or finish the Cognito config."
        )
        raise SystemExit(2)
    if probe.status == 401:
        c.line()
        c.line(c.red("Authentication rejected (401)."))
        c.line(
            "This deployment enforces Cognito JWT verification, so X-User-Identity is\n"
            "ignored. Re-run with --token <JWT> using a token from the configured pool."
        )
        raise SystemExit(2)

    with ctx.check("GET /api/config returns settings and infrastructure readiness"):
        expect_status(probe, 200)
        payload = expect_dict(probe)
        expect_keys(
            payload,
            "default_top_k",
            "allowed_git_hosts",
            "infrastructure",
            "git_credential_providers",
        )
        expect(
            isinstance(payload["allowed_git_hosts"], list),
            "allowed_git_hosts should be a list",
        )
        expect(
            isinstance(payload["git_credential_providers"], list),
            "git_credential_providers should be a list",
        )
        infra = payload["infrastructure"]
        expect(isinstance(infra, dict), "infrastructure should be an object")
        expect_keys(infra, "ready", "s3_bucket", "vectors_bucket", "vector_index")
        # FIX-25: the payload must not leak bucket names, regions, or model IDs.
        leaked = [k for k in ("bucket_name", "region", "model_id") if k in infra]
        expect(not leaked, f"infrastructure leaked sensitive keys: {leaked}")

        ctx.default_top_k = int(payload["default_top_k"])
        ctx.allowed_git_hosts = list(payload["allowed_git_hosts"])
        ctx.infra_ready = bool(infra["ready"])
        ctx.allow_credentials_in_git_url = bool(
            payload.get("allow_credentials_in_git_url", False)
        )

    # Detect once whether this deployment enforces space authorization, so the
    # negative paths (and the authz / MCP phases) assert the right outcome. The
    # read gate denies a *missing* space with 401 read_denied only when the
    # switch is on; with it off the status route answers 200 NOT_FOUND. This runs
    # after the /api/config check above succeeded, so the caller is authenticated
    # and a 401 here is an authorization denial, not a missing credential.
    probe = client.get(f"/api/spaces/{urllib.parse.quote(MISSING_SPACE_ID)}/status")
    ctx.authz_enforced = probe.status == 401 and "read_denied" in probe.text
    c.note(f"authorization enforced: {'yes' if ctx.authz_enforced else 'no'}")

    # Resolve --reuse-space here rather than inside the git-space phase: it is a
    # precondition for the query/overview/refresh/agent phases, which a user may
    # well select on their own via --phase.
    if ctx.args.reuse_space:
        ctx.git_space_id = ctx.args.reuse_space
        with ctx.check("--reuse-space target is READY"):
            resp = expect_status(
                client.get(f"/api/spaces/{urllib.parse.quote(ctx.git_space_id)}/status"), 200
            )
            status = expect_dict(resp)
            state = status.get("status")
            expect(
                state == "READY",
                f"--reuse-space {ctx.git_space_id} is {state!r}, not READY",
            )
            ctx.git_space_ready = True
            detail = read_space_detail(ctx, ctx.git_space_id)
            ctx.vectors_available = bool(detail.get("vector_count"))
            c.note(
                f"reusing {ctx.git_space_id} "
                f"(components={detail.get('component_count')}, "
                f"vectors={detail.get('vector_count')})"
            )

    if not ctx.infra_ready:
        c.warn(
            "infrastructure.ready is false — S3, the vectors bucket, or the vector index "
            "is missing. Space creation will likely fail."
        )
    if not ctx.allowed_git_hosts:
        c.warn(
            "CODEKB_ALLOWED_GIT_HOSTS is not set, so git-based space creation is disabled "
            "(fail-closed). Git lifecycle checks will be skipped."
        )
    else:
        c.note(f"allowed git hosts: {', '.join(ctx.allowed_git_hosts)}")
    c.note(f"default_top_k: {ctx.default_top_k}")


# ── Phase 1: read-only endpoints ─────────────────────────────────────────────


def phase_readonly(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Read-only endpoints")

    with ctx.check("GET /api/metrics aggregates across spaces"):
        resp = expect_status(client.get("/api/metrics"), 200)
        payload = expect_dict(resp)
        expect_keys(
            payload,
            "total_spaces",
            "ready_spaces",
            "failed_spaces",
            "in_progress_spaces",
            "total_components",
            "total_lines_of_code",
            "total_vectors",
            "languages",
            "language_count",
        )
        expect(isinstance(payload["languages"], list), "languages should be a list")
        expect(
            payload["total_spaces"] >= payload["ready_spaces"],
            "total_spaces should be >= ready_spaces",
        )

    with ctx.check("GET /api/spaces lists spaces"):
        resp = expect_status(client.get("/api/spaces"), 200)
        spaces = expect_list(resp)
        for space in spaces:
            expect_keys(space, "space_id", "status")
        c.note(f"{len(spaces)} space(s) currently on this instance")

    with ctx.check("GET /api/hyperspaces lists hyperspaces"):
        resp = expect_status(client.get("/api/hyperspaces"), 200)
        expect_list(resp)

    with ctx.check("GET /api/search runs an unscoped semantic search"):
        # May legitimately return zero hits when nothing has been summarized;
        # the queries phase is where hits are asserted for real.
        resp = expect_status(
            client.get("/api/search", params={"q": ctx.args.search_query}, timeout=180), 200
        )
        expect_list(resp)

    with ctx.check("GET /favicon.ico returns 204"):
        expect_status(client.get("/favicon.ico"), 204)


def phase_negative(ctx: Context) -> None:
    """Negative paths: missing resources, bad input, and the SSRF allowlist.

    These are the checks most likely to catch a regression that a happy-path
    test would sail straight past.
    """
    c, client = ctx.console, ctx.client
    c.heading("Negative paths and input validation")

    with ctx.check("GET /api/spaces/{unknown} returns 404"):
        expect_unknown_space(ctx, client.get(f"/api/spaces/{MISSING_SPACE_ID}"), 404)

    with ctx.check("GET /api/spaces/{unknown}/status returns 200 with NOT_FOUND"):
        # By design this endpoint never 404s — it reports NOT_FOUND in the body.
        # Under authz the read gate denies an unknown space before the route runs.
        resp = client.get(f"/api/spaces/{MISSING_SPACE_ID}/status")
        if ctx.authz_enforced:
            expect_authz_denied(resp, 401, "read_denied")
        else:
            payload = expect_dict(expect_status(resp, 200))
            expect(
                payload.get("status") == "NOT_FOUND",
                f"expected status NOT_FOUND, got {payload.get('status')!r}",
            )

    with ctx.check("GET /api/spaces/{unknown}/knowledge-base returns 404"):
        expect_unknown_space(ctx, client.get(f"/api/spaces/{MISSING_SPACE_ID}/knowledge-base"), 404)

    with ctx.check("GET /api/spaces/{unknown}/components returns 404"):
        expect_unknown_space(ctx, client.get(f"/api/spaces/{MISSING_SPACE_ID}/components"), 404)

    with ctx.check("DELETE /api/spaces/{unknown} returns 404"):
        expect_unknown_space(ctx, client.delete(f"/api/spaces/{MISSING_SPACE_ID}"), 404)

    with ctx.check("GET /api/hyperspaces/{unknown} returns 404"):
        expect_status(client.get(f"/api/hyperspaces/{MISSING_HYPERSPACE_ID}"), 404)

    with ctx.check("DELETE /api/hyperspaces/{unknown} returns 404"):
        expect_status(client.delete(f"/api/hyperspaces/{MISSING_HYPERSPACE_ID}"), 404)

    with ctx.check("POST /api/spaces without git_url returns 422"):
        expect_status(client.post("/api/spaces", json_body={"space_name": "no url"}), 422)

    with ctx.check("GET /api/spaces/git-branches without git_url returns 422"):
        expect_status(client.get("/api/spaces/git-branches"), 422)

    with ctx.check("GET /api/search without q returns 422"):
        expect_status(client.get("/api/search"), 422)

    with ctx.check("flow depth below range returns 422"):
        # Under authz the read gate denies the unknown space before query-param
        # validation can run, so the 422 becomes a 401 read_denied.
        resp = client.get(
            f"/api/spaces/{MISSING_SPACE_ID}/components/anything/flow", params={"depth": 0}
        )
        expect_unknown_space(ctx, resp, 422)

    with ctx.check("flow depth above range returns 422"):
        resp = client.get(
            f"/api/spaces/{MISSING_SPACE_ID}/components/anything/flow", params={"depth": 51}
        )
        expect_unknown_space(ctx, resp, 422)

    with ctx.check("search limit above range returns 422"):
        resp = client.get("/api/search", params={"q": "x", "limit": 999})
        expect_status(resp, 422)

    with ctx.check("POST /api/agent/chat without message returns 422"):
        # `message` is the only required field. Omitting `space_ids` is valid and
        # means "every space" — that path is asserted in the agent phase, because
        # it reaches Bedrock and this tier must stay free of model calls.
        expect_status(client.post("/api/agent/chat", json_body={}), 422)

    with ctx.check("POST /api/hyperspaces with no spaces returns 400"):
        # One space is a valid hyperspace; zero is not.
        resp = client.post(
            "/api/hyperspaces",
            json_body={"name": "regression-too-small", "space_ids": []},
        )
        expect_status(resp, 400)

    with ctx.check("POST /api/hyperspaces with unknown spaces returns 404"):
        resp = client.post(
            "/api/hyperspaces",
            json_body={
                "name": "regression-unknown-members",
                "space_ids": [MISSING_SPACE_ID, MISSING_SPACE_ID + "-b"],
            },
        )
        expect_status(resp, 404)

    with ctx.check("POST /api/hyperspaces with one unknown space returns 404"):
        # Single-member hyperspaces are allowed, so this must fail on the
        # membership check rather than on a size guard.
        resp = client.post(
            "/api/hyperspaces",
            json_body={"name": "regression-unknown-solo", "space_ids": [MISSING_SPACE_ID]},
        )
        expect_status(resp, 404)

    with ctx.check("POST /api/spaces/upload before the zip exists returns 400"):
        resp = client.post(
            "/api/spaces/upload",
            json_body={"space_id": MISSING_SPACE_ID, "space_name": "regression-missing-upload"},
        )
        expect_status(resp, 400)

    with ctx.check("POST /api/spaces/{unknown}/refresh returns 404"):
        expect_unknown_space(ctx, client.post(f"/api/spaces/{MISSING_SPACE_ID}/refresh"), 404)

    with ctx.check("POST /api/spaces/{unknown}/generate-overview returns 404"):
        expect_unknown_space(
            ctx, client.post(f"/api/spaces/{MISSING_SPACE_ID}/generate-overview"), 404
        )

    with ctx.check("POST /api/hyperspaces/{unknown}/generate-overview returns 404"):
        expect_status(
            client.post(f"/api/hyperspaces/{MISSING_HYPERSPACE_ID}/generate-overview"), 404
        )

    with ctx.check("GET /api/hyperspaces/{unknown}/overview/{doc} returns 404"):
        expect_status(
            client.get(f"/api/hyperspaces/{MISSING_HYPERSPACE_ID}/overview/system_overview"), 404
        )

    # ── SSRF controls on git_url ──
    c.heading("Git URL SSRF controls")

    for label, bad_url in [
        ("file:// scheme", "file:///etc/passwd"),
        ("ext:: RCE transport", "ext::sh -c whoami"),
        ("plaintext git://", "git://github.com/octocat/Hello-World.git"),
        ("no scheme", "github.com/octocat/Hello-World.git"),
        ("empty git_url", ""),
    ]:
        with ctx.check(f"POST /api/spaces rejects {label}"):
            resp = client.post("/api/spaces", json_body={"git_url": bad_url})
            expect_status(resp, 400)

    # Credentials embedded in the git_url (userinfo) are rejected on an
    # allowlisted host, unless CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL is enabled.
    for label, creds_url in [
        ("user:password userinfo", "https://user:tok@github.com/org/repo.git"),
        ("single-token userinfo", "https://x-access-token@github.com/org/repo.git"),
    ]:
        with ctx.check(f"POST /api/spaces rejects {label} in the git_url"):
            if ctx.allow_credentials_in_git_url:
                raise Skipped("CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL is enabled on this instance")
            resp = client.post("/api/spaces", json_body={"git_url": creds_url})
            expect_status(resp, 400)

    with ctx.check("POST /api/spaces rejects a host outside the allowlist"):
        # Skip if someone has genuinely allowlisted this domain.
        blocked_host = "codekb-regression-not-allowed.example.com"
        if any(
            blocked_host == h or blocked_host.endswith("." + h) for h in ctx.allowed_git_hosts
        ):
            raise Skipped(f"{blocked_host} is in the allowlist on this instance")
        resp = client.post(
            "/api/spaces", json_body={"git_url": f"https://{blocked_host}/org/repo.git"}
        )
        expect_status(resp, 400)

    with ctx.check("GET /api/spaces/git-branches rejects a host outside the allowlist"):
        resp = client.get(
            "/api/spaces/git-branches",
            params={"git_url": "https://codekb-regression-not-allowed.example.com/o/r.git"},
        )
        expect_status(resp, 400)


def phase_security_headers(ctx: Context) -> None:
    """Assert the headers the security middleware guarantees.

    Only the two unconditional headers are checked. HSTS is opt-in (empty by
    default) and CSP is deliberately scoped to text/html, so neither belongs in
    an assertion about an API response.
    """
    c, client = ctx.console, ctx.client
    c.heading("Security headers")

    with ctx.check("security headers are present on API responses"):
        resp = expect_status(client.get("/api/health"), 200)
        got_nosniff = resp.headers.get("x-content-type-options", "").lower()
        got_frame = resp.headers.get("x-frame-options", "").upper()
        if not got_nosniff and not got_frame:
            raise Skipped(
                "no security headers at all — security.headers.enabled may be false"
            )
        expect(
            got_nosniff == "nosniff",
            f"X-Content-Type-Options should be 'nosniff', got {got_nosniff!r}",
        )
        expect(got_frame == "DENY", f"X-Frame-Options should be 'DENY', got {got_frame!r}")

    with ctx.check("CSP is applied to the SPA document"):
        # The middleware attaches CSP only to text/html, and "/" is registered
        # only when the built frontend is bundled into the image.
        resp = client.get("/")
        if resp.status == 404:
            raise Skipped("SPA is not bundled in this build (no codekb/static/dist)")
        expect_status(resp, 200)
        expect(
            "text/html" in resp.content_type,
            f"expected the SPA document, got content-type {resp.content_type!r}",
        )
        csp = resp.headers.get("content-security-policy") or resp.headers.get(
            "content-security-policy-report-only"
        )
        expect(csp, "no Content-Security-Policy on the SPA document")
        for directive in ("default-src", "script-src", "frame-ancestors", "object-src"):
            expect(directive in csp, f"CSP is missing the {directive} directive")

    with ctx.check("unknown paths return a real 404, not the SPA shell"):
        # The DAST finding (unauthenticated web endpoint) fired because the SPA
        # catch-all answered arbitrary probe paths with index.html + 200, which
        # looks like an exposed service. Unknown paths must now 404 as JSON,
        # never the SPA HTML.
        if client.get("/").status == 404:
            raise Skipped("SPA is not bundled in this build (no codekb/static/dist)")
        probe = expect_status(client.get("/openapi.json"), 404)
        expect(
            "text/html" not in (probe.content_type or ""),
            f"/openapi.json should 404 as JSON, got content-type {probe.content_type!r}",
        )

    with ctx.check("known client-side routes still serve the SPA shell"):
        # Guards against over-narrowing the allowlist: deep links and refreshes
        # on real UI routes (served only by the catch-all) must still work.
        resp = client.get("/git-creds")
        if resp.status == 404:
            raise Skipped("SPA is not bundled in this build (no codekb/static/dist)")
        expect_status(resp, 200)
        expect(
            "text/html" in (resp.content_type or ""),
            f"expected the SPA document for a known route, got {resp.content_type!r}",
        )

    with ctx.check("CSP is not applied to JSON responses"):
        resp = expect_status(client.get("/api/health"), 200)
        expect(
            "content-security-policy" not in resp.headers,
            "CSP should be scoped to text/html, not JSON responses",
        )


# ── Phase 2: git space lifecycle ─────────────────────────────────────────────


def phase_git_space(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Git space lifecycle")

    if ctx.args.reuse_space:
        # Already validated in preflight; creating another space would defeat
        # the purpose of the flag.
        ctx.skip(
            "POST /api/spaces creates a space from git",
            f"--reuse-space {ctx.args.reuse_space} supplied",
        )
        return

    git_url = ctx.args.git_url
    host = (urllib.parse.urlparse(git_url).hostname or "").lower()
    if not ctx.allowed_git_hosts or not any(
        host == h or host.endswith("." + h) for h in ctx.allowed_git_hosts
    ):
        ctx.skip(
            "POST /api/spaces creates a space from git",
            f"host '{host}' is not in the allowlist ({ctx.allowed_git_hosts or 'empty'}); "
            f"pass --git-url for an allowed host, or --reuse-space",
        )
        return

    with ctx.check("GET /api/spaces/git-branches lists remote refs"):
        resp = expect_status(
            client.get("/api/spaces/git-branches", params={"git_url": git_url}, timeout=120), 200
        )
        payload = expect_dict(resp)
        expect_keys(payload, "git_url", "branches", "tags")
        expect(isinstance(payload["branches"], list), "branches should be a list")
        expect(payload["branches"], "expected at least one branch on the remote")
        c.note(f"{len(payload['branches'])} branch(es), {len(payload['tags'])} tag(s)")

    with ctx.check("POST /api/spaces creates a space from git"):
        resp = client.post(
            "/api/spaces",
            json_body={
                "git_url": git_url,
                "space_name": "codekb-regression-git",
                # Summarize so vectors exist and semantic search is asserted for
                # real; this is part of the single default run now.
                "summarize": True,
            },
            timeout=120,
        )
        expect_status(resp, 201)
        payload = expect_dict(resp)
        expect_keys(payload, "space_id", "git_url", "status")
        # Record the space before further assertions so that even a partial
        # failure still leaves it tracked for teardown and later phases.
        ctx.git_space_id = payload["space_id"]
        ctx.created_git_space = True
        c.note(f"space_id: {ctx.git_space_id}")
        expect(
            payload["status"] == "SUBMITTED",
            f"expected SUBMITTED, got {payload['status']!r}",
        )

    if not ctx.git_space_id:
        ctx.skip("git space analysis reaches READY", "space was never created")
        return

    space_path = f"/api/spaces/{urllib.parse.quote(ctx.git_space_id)}"

    # While the pipeline is mid-flight, mutating operations must be refused.
    # This is the only reliable window to exercise the 409 busy paths.
    with ctx.check("POST /{id}/refresh while analyzing returns 409"):
        resp = client.post(f"{space_path}/refresh")
        if resp.status == 202:
            raise Skipped("analysis already finished before the busy check could run")
        expect_status(resp, 409)

    with ctx.check("POST /{id}/generate-overview while analyzing returns 409"):
        resp = client.post(f"{space_path}/generate-overview")
        if resp.status == 202:
            raise Skipped("analysis already finished before the busy check could run")
        expect_status(resp, 409)

    with ctx.check("git space analysis reaches READY"):
        status = poll_space_status(
            ctx, ctx.git_space_id, timeout=ctx.args.analysis_timeout, label="git analysis"
        )
        state = status.get("status")
        if state == "FAILED":
            raise CheckError(f"analysis FAILED: {status.get('error') or 'no error reported'}")
        expect(state == "READY", f"expected READY, got {state!r}")
        ctx.git_space_ready = True
        # vector_count is only exposed by the detail route, not /status.
        detail = read_space_detail(ctx, ctx.git_space_id)
        ctx.vectors_available = bool(detail.get("vector_count"))
        c.note(
            f"components={detail.get('component_count')} "
            f"vectors={detail.get('vector_count')}"
        )

    if not ctx.git_space_ready:
        return

    with ctx.check("status reports completed pipeline steps"):
        resp = expect_status(client.get(f"{space_path}/status"), 200)
        status = expect_dict(resp)
        steps = status.get("pipeline_steps") or []
        expect(steps, "pipeline_steps is empty")
        by_step = {s.get("step"): s.get("status") for s in steps}
        for required in ("clone", "analyze", "save"):
            expect(required in by_step, f"pipeline step '{required}' is missing")
            expect(
                by_step[required] == "completed",
                f"pipeline step '{required}' is {by_step[required]!r}, expected completed",
            )
        expect("summarize" in by_step, "summarize step missing despite summarize=true")

    with ctx.check("status includes git provenance"):
        resp = expect_status(client.get(f"{space_path}/status"), 200)
        status = expect_dict(resp)
        expect(status.get("git_url"), "git_url missing from status")

    with ctx.check("space detail records the creating identity"):
        # created_by lives on SpaceDetail / SpaceSummary, never on /status.
        detail = read_space_detail(ctx, ctx.git_space_id)
        expect(detail.get("created_by"), "created_by was not captured on the space")
        expected = ctx.args.user_identity if not ctx.args.token else None
        if expected:
            expect(
                detail["created_by"] == expected,
                f"created_by is {detail['created_by']!r}, expected the calling "
                f"identity {expected!r}",
            )

    with ctx.check("space list also reports the creating identity"):
        resp = expect_status(client.get("/api/spaces", timeout=120), 200)
        rows = expect_list(resp)
        row = next((r for r in rows if r.get("space_id") == ctx.git_space_id), None)
        expect(row is not None, f"{ctx.git_space_id} is missing from GET /api/spaces")
        expect(row.get("created_by"), "created_by missing from the space list row")


# ── Phase 2b: git credentials and private-repo clone ────────────────────────


def phase_git_credentials(ctx: Context) -> None:
    """Store a PAT via the git-credentials API, then clone a private repo with it.

    Skips entirely without --github-pat-token.
    """
    c, client = ctx.console, ctx.client
    c.heading("Git credentials and private-repo clone")

    if not ctx.args.github_pat_token:
        for name in (
            "PUT /gitcreds/github rejects an invalid token",
            "PUT /gitcreds/github stores a valid token",
            "GET /gitcreds lists the saved github credential",
            "POST /gitcreds/github/test re-validates the stored token",
            "private git space analysis reaches READY",
            "public git space analysis reaches READY",
        ):
            ctx.skip(name, "--github-pat-token not supplied")
        return

    base = "/api/user-profile/gitcreds"
    created_spaces: list[str] = []
    saved_credential = False
    try:
        with ctx.check("PUT /gitcreds/github rejects an invalid token"):
            # Save validates against GitHub first; a bad token is rejected 400.
            resp = client.put(
                f"{base}/github",
                json_body={
                    "username": ctx.args.github_username,
                    "token": "ghp_codekbRegressionObviouslyInvalidToken0000000000",
                },
                timeout=60,
            )
            expect_status(resp, 400)

        with ctx.check("PUT /gitcreds/github stores a valid token"):
            resp = client.put(
                f"{base}/github",
                json_body={
                    "username": ctx.args.github_username,
                    "token": ctx.args.github_pat_token,
                },
                timeout=60,
            )
            expect_status(resp, 200)
            payload = expect_dict(resp)
            expect(
                payload.get("provider") == "github",
                f"expected provider 'github', got {payload.get('provider')!r}",
            )
            saved_credential = True

        with ctx.check("GET /gitcreds lists the saved github credential"):
            if not saved_credential:
                raise Skipped("token was never stored")
            resp = expect_status(client.get(base, timeout=60), 200)
            rows = expect_list(resp)
            expect(
                any(r.get("provider") == "github" for r in rows if isinstance(r, dict)),
                "github is missing from the saved-credentials list",
            )

        with ctx.check("POST /gitcreds/github/test re-validates the stored token"):
            if not saved_credential:
                raise Skipped("token was never stored")
            resp = expect_status(client.post(f"{base}/github/test", timeout=60), 200)
            payload = expect_dict(resp)
            expect(
                payload.get("status") == "ok",
                f"expected status 'ok', got {payload.get('status')!r}",
            )

        with ctx.check("private git space analysis reaches READY"):
            if not ctx.args.github_private_git_url:
                raise Skipped("--github-private-git-url not supplied")
            resp = client.post(
                "/api/spaces",
                json_body={
                    "git_url": ctx.args.github_private_git_url,
                    "space_name": "codekb-regression-private",
                    "summarize": False,
                },
                timeout=120,
            )
            expect_status(resp, 201)
            payload = expect_dict(resp)
            sid = payload.get("space_id")
            expect(sid, "no space_id returned for the private repo")
            created_spaces.append(sid)
            status = poll_space_status(
                ctx, sid, timeout=ctx.args.analysis_timeout, label="private clone"
            )
            state = status.get("status")
            if state == "FAILED":
                raise CheckError(
                    f"private clone FAILED: {status.get('error') or 'no error reported'} — "
                    f"the stored token may lack access to this repo"
                )
            expect(state == "READY", f"expected READY, got {state!r}")

        with ctx.check("public git space analysis reaches READY"):
            # A credentialed run must not regress ordinary public analysis.
            public_url = ctx.args.git_url or DEFAULT_GIT_URL
            resp = client.post(
                "/api/spaces",
                json_body={
                    "git_url": public_url,
                    "space_name": "codekb-regression-gitcreds-public",
                    "summarize": False,
                },
                timeout=120,
            )
            expect_status(resp, 201)
            payload = expect_dict(resp)
            sid = payload.get("space_id")
            expect(sid, "no space_id returned for the public repo")
            created_spaces.append(sid)
            status = poll_space_status(
                ctx, sid, timeout=ctx.args.analysis_timeout, label="public clone"
            )
            state = status.get("status")
            if state == "FAILED":
                raise CheckError(
                    f"public clone FAILED: {status.get('error') or 'no error reported'}"
                )
            expect(state == "READY", f"expected READY, got {state!r}")
    finally:
        # Honor --keep; treat a 404 on cleanup as already-gone.
        if ctx.args.keep:
            c.note("--keep set; leaving git-credentials spaces and the stored token in place")
            for sid in created_spaces:
                c.note(f"git-credentials space: {sid}")
        else:
            for sid in created_spaces:
                with ctx.check(f"DELETE /api/spaces/{{id}} (git-credentials {sid})"):
                    # Async delete: 202 Accepted (or 404 if already gone).
                    resp = client.delete(
                        f"/api/spaces/{urllib.parse.quote(sid)}", timeout=900.0
                    )
                    expect_status(resp, 202, 404)
            if saved_credential:
                with ctx.check("DELETE /gitcreds/github removes the stored token"):
                    expect_status(client.delete(f"{base}/github", timeout=60), 200, 404)


# ── Phase 3: space reads and query endpoints ────────────────────────────────


def phase_space_queries(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Space detail and query endpoints")

    if not (ctx.git_space_id and ctx.git_space_ready):
        for name in (
            "GET /{id} returns space detail",
            "GET /{id}/knowledge-base returns the raw KB",
            "GET /{id}/components lists components",
            "GET /{id}/components?type= filters by type",
            "GET /{id}/components?pattern= wildcard search",
            "GET /{id}/components/{name} returns one component",
            "GET /{id}/components/{name}/flow traces execution",
            "GET /{id}/components/{name}/deps returns dependencies",
            "GET /{id}/components/{unknown} returns 404",
            "GET /{id}/search returns ranked vector hits",
            "GET /{id}/search honors the limit parameter",
            "GET /{id}/search descriptions come from summarization",
            "GET /api/search scoped to one space filters by space_id",
            "GET /api/search unscoped searches the whole index",
        ):
            ctx.skip(name, "no READY git space available")
        return

    space_path = f"/api/spaces/{urllib.parse.quote(ctx.git_space_id)}"

    with ctx.check("GET /{id} returns space detail"):
        resp = expect_status(client.get(space_path), 200)
        payload = expect_dict(resp)
        expect_keys(
            payload,
            "space_id",
            "source_directory",
            "component_count",
            "version",
            "statistics",
            "vector_count",
            "git_url",
        )
        expect(
            payload["component_count"] > 0,
            f"component_count is {payload['component_count']}, expected > 0 — "
            f"the parser extracted nothing",
        )
        # source_directory has to survive the pipeline that produced it.
        expect_no_working_tree_path(
            f"{payload['source_directory']}/", "space detail source_directory"
        )

    with ctx.check("GET /{id}/knowledge-base returns the raw KB"):
        resp = expect_status(client.get(f"{space_path}/knowledge-base", timeout=180), 200)
        payload = expect_dict(resp)
        expect(payload, "knowledge base document is empty")

    with ctx.check("GET /{id}/components lists components"):
        resp = expect_status(client.get(f"{space_path}/components", timeout=180), 200)
        components = expect_list(resp)
        expect(components, "component list is empty for a READY space")
        first = components[0]
        expect(isinstance(first, dict), "components should be objects")
        expect_keys(first, "name")

        # Prefer a function/method: those have call graphs, so flow/deps return data.
        preferred = next(
            (comp for comp in components if comp.get("type") in ("function", "method")),
            first,
        )
        ctx.component_name = preferred.get("name")
        c.note(f"{len(components)} component(s); probing '{ctx.component_name}'")

    # A separate check, deliberately: a wrong path here must not prevent the
    # component discovery above from setting ctx.component_name, or every
    # downstream flow/deps/by-name check would skip on a single failure.
    with ctx.check("GET /{id}/components file paths name the durable cache location"):
        resp = expect_status(client.get(f"{space_path}/components", timeout=180), 200)
        components = expect_list(resp)
        # A path into a pipeline working tree is a dead pointer the moment
        # analysis finishes, and it leaks an internal scratch directory to every
        # API client.
        for comp in components:
            path = str(comp.get("file_path", ""))
            if path:
                expect_no_working_tree_path(
                    f"{path}/", f"component {comp.get('name', '?')!r} file_path"
                )

    with ctx.check("GET /{id}/components?type= filters by type"):
        resp = expect_status(
            client.get(f"{space_path}/components", params={"type": "class"}, timeout=180), 200
        )
        classes = expect_list(resp)
        for comp in classes:
            expect(
                comp.get("type") == "class",
                f"type filter leaked a {comp.get('type')!r} component",
            )

    with ctx.check("GET /{id}/components?pattern= wildcard search"):
        if not ctx.component_name:
            raise Skipped("no component name discovered")
        stem = ctx.component_name.split(".")[-1][:3] or ctx.component_name[:3]
        resp = expect_status(
            client.get(f"{space_path}/components", params={"pattern": f"{stem}*"}, timeout=180),
            200,
        )
        expect_list(resp)

    with ctx.check("GET /{id}/components/{name} returns one component"):
        if not ctx.component_name:
            raise Skipped("no component name discovered")
        name = urllib.parse.quote(ctx.component_name, safe="")
        resp = expect_status(client.get(f"{space_path}/components/{name}", timeout=180), 200)
        payload = expect_dict(resp)
        expect_keys(payload, "id", "name")
        expect(
            payload["name"] == ctx.component_name,
            f"asked for {ctx.component_name!r}, got {payload['name']!r}",
        )

    with ctx.check("GET /{id}/components/{name}/flow traces execution"):
        if not ctx.component_name:
            raise Skipped("no component name discovered")
        name = urllib.parse.quote(ctx.component_name, safe="")
        resp = client.get(f"{space_path}/components/{name}/flow", params={"depth": 5}, timeout=180)
        if resp.status == 404:
            raise Skipped(f"'{ctx.component_name}' has no call flow")
        expect_status(resp, 200)
        expect_dict(resp)

    with ctx.check("GET /{id}/components/{name}/deps returns dependencies"):
        if not ctx.component_name:
            raise Skipped("no component name discovered")
        name = urllib.parse.quote(ctx.component_name, safe="")
        resp = client.get(f"{space_path}/components/{name}/deps", timeout=180)
        if resp.status == 404:
            raise Skipped(f"'{ctx.component_name}' has no dependency record")
        expect_status(resp, 200)
        expect_dict(resp)

    with ctx.check("GET /{id}/components/{unknown} returns 404"):
        expect_status(
            client.get(f"{space_path}/components/CodeKbRegressionNoSuchComponent", timeout=180),
            404,
        )

    query = ctx.args.search_query

    with ctx.check("GET /{id}/search returns ranked vector hits"):
        resp = expect_status(
            client.get(f"{space_path}/search", params={"q": query}, timeout=180), 200
        )
        hits = expect_list(resp)
        if not ctx.vectors_available:
            if hits:
                raise CheckError("got vector hits for a space with no vectors")
            raise Skipped(
                "space has no vectors — summarization produced none, or this is a "
                "--reuse-space of an un-summarized space"
            )
        distances = expect_vector_hits(hits, space_ids={ctx.git_space_id})
        c.note(
            f"{len(hits)} hit(s) for {query!r}; nearest: {hits[0]['key']} "
            f"(distance {distances[0]:.4f})"
        )

    with ctx.check("GET /{id}/search honors the limit parameter"):
        if not ctx.vectors_available:
            raise Skipped("space has no vectors")
        resp = expect_status(
            client.get(f"{space_path}/search", params={"q": query, "limit": 3}, timeout=180), 200
        )
        hits = expect_list(resp)
        expect(hits, "limited search returned no hits")
        expect(len(hits) <= 3, f"limit=3 was ignored, got {len(hits)} hits")

    with ctx.check("GET /{id}/search descriptions come from summarization"):
        if not ctx.vectors_available:
            raise Skipped("space has no vectors")
        resp = expect_status(
            client.get(f"{space_path}/search", params={"q": query, "limit": 5}, timeout=180), 200
        )
        hits = expect_list(resp)
        expect(hits, "search returned no hits")
        # A real AI summary is prose, not a name echo or a stub.
        longest = max((str(h["metadata"].get("description", "")) for h in hits), key=len)
        expect(
            len(longest) > 80,
            f"longest hit description is only {len(longest)} chars — summarization "
            f"likely wrote placeholder text rather than a real summary",
        )
        c.note(f"longest description: {len(longest)} chars")

    with ctx.check("GET /api/search scoped to one space filters by space_id"):
        if not ctx.vectors_available:
            raise Skipped("space has no vectors")
        resp = expect_status(
            client.get(
                "/api/search",
                params={"q": query, "spaces": ctx.git_space_id, "limit": 10},
                timeout=180,
            ),
            200,
        )
        hits = expect_list(resp)
        expect(len(hits) <= 10, f"limit=10 was ignored, got {len(hits)} hits")
        expect_vector_hits(hits, space_ids={ctx.git_space_id})

    with ctx.check("GET /api/search unscoped searches the whole index"):
        resp = expect_status(
            client.get("/api/search", params={"q": query, "limit": 5}, timeout=180), 200
        )
        hits = expect_list(resp)
        expect(len(hits) <= 5, f"limit=5 was ignored, got {len(hits)} hits")
        if not hits:
            raise Skipped("no vectors anywhere on this instance")
        # Unscoped: any space may legitimately win, so don't constrain space_id.
        expect_vector_hits(hits)


# ── Phase 3b: authorization ──────────────────────────────────────────────────


def _authz_other_client(ctx: Context) -> Client | None:
    """A client authenticated as a *different* principal than ctx.client.

    X-User-Identity mode (local dev): synthesize a second identity, exactly as
    the non-owner upload check does. Cognito mode: a JWT is one fixed principal,
    so a second identity must be supplied explicitly via --second-token; without
    it, cross-identity checks cannot run.
    """
    if ctx.args.second_token:
        return Client(
            ctx.client.base_url, token=ctx.args.second_token, timeout=ctx.client.timeout
        )
    if ctx.args.token:
        return None
    return Client(
        ctx.client.base_url,
        user_identity=f"codekb-regression-other-{ctx.args.user_identity}",
        timeout=ctx.client.timeout,
    )


def phase_authz(ctx: Context) -> None:
    """Git-permission space authorization (CODEKB_REQUIRE_AUTHORIZATION).

    The suite's other phases assume authorization is OFF — their negative paths
    (e.g. "GET /{unknown}/status returns 200 NOT_FOUND") only hold when every
    caller is allowed. So this phase first *detects* whether the instance
    enforces authorization, then asserts the deny/allow contract only when it
    does; otherwise every enforcement check is skipped with a clear reason rather
    than silently passing or spuriously failing. Running the full suite against
    an authz-ON instance is therefore expected to fail the auth-off-assuming
    negative checks in other phases — this phase is the one that validates
    authz-ON behavior.

    Contract asserted when enforcement is on (backend/src/authz):
      - a missing space reads as 401 read_denied (never confirms existence)
      - a zip space is owner-only: a non-owner read (status + detail) is denied
        401, a non-owner delete is denied and the space survives
      - GET /api/spaces hides spaces the caller cannot read
      - a public git space is readable by any signed-in user, but a non-creator
        write (refresh) is denied 403 write_denied
    """
    c, owner = ctx.console, ctx.client
    c.heading("Authorization (git-permission space access)")

    # ── Detect enforcement with a single-identity probe ──────────────────────
    # gate_read denies a *missing* space (so it never reveals whether a space
    # exists); with the switch off the route runs and reports NOT_FOUND. That
    # difference is the cheapest on/off signal and needs no resources or second
    # identity.
    with ctx.check("authorization mode detected (missing-space probe)"):
        resp = owner.get(f"/api/spaces/{urllib.parse.quote(MISSING_SPACE_ID)}/status")
        if resp.status == 200:
            expect(
                expect_dict(resp).get("status") == "NOT_FOUND",
                f"expected 200 NOT_FOUND with authz off, got {resp.preview()}",
            )
            ctx.authz_enforced = False
            c.note("CODEKB_REQUIRE_AUTHORIZATION is OFF — enforcement checks will be skipped")
        elif resp.status == 401:
            expect_authz_denied(resp, 401, "read_denied")
            ctx.authz_enforced = True
            c.note("CODEKB_REQUIRE_AUTHORIZATION is ON")
        else:
            raise CheckError(
                f"missing-space probe returned an unexpected {resp.status}: {resp.preview()}"
            )

    enforcement_checks = [
        "owner can read its own zip space",
        "non-owner read of a zip space status is denied",
        "non-owner read of zip space detail is denied",
        "non-owner delete of a zip space is denied and the space survives",
        "space list hides spaces the caller cannot read",
        "non-owner read of a public git space is allowed",
        "non-creator write to a public git space is denied",
    ]

    if not ctx.authz_enforced:
        for name in enforcement_checks:
            ctx.skip(name, "authorization is disabled on this instance")
        return

    other = _authz_other_client(ctx)
    if other is None:
        for name in enforcement_checks:
            ctx.skip(name, "needs a second identity; pass --second-token on a JWT deployment")
        return

    # ── Owner-only zip space: mint a cheap one (no upload, no analysis) ───────
    zip_id: str | None = None
    with ctx.check("owner can read its own zip space"):
        resp = owner.post(
            "/api/spaces/upload-url",
            json_body={"space_name": "codekb-regression-authz", "summarize": False},
        )
        if resp.status == 403:
            raise Skipped("zip upload is disabled (CODEKB_ZIP_UPLOAD_ENABLED=false)")
        expect_status(resp, 200)
        zip_id = expect_dict(resp).get("space_id")
        expect(zip_id, "upload-url returned no space_id")
        ctx.authz_space_id = zip_id
        status = expect_dict(
            expect_status(owner.get(f"/api/spaces/{urllib.parse.quote(zip_id)}/status"), 200)
        )
        expect(
            status.get("status") == "PENDING_UPLOAD",
            f"owner read its own new zip space as {status.get('status')!r}, not PENDING_UPLOAD",
        )

    if not zip_id:
        for name in enforcement_checks[1:5]:
            ctx.skip(name, "no zip space to probe")
    else:
        zq = urllib.parse.quote(zip_id)

        with ctx.check("non-owner read of a zip space status is denied"):
            expect_authz_denied(other.get(f"/api/spaces/{zq}/status"), 401, "read_denied")

        with ctx.check("non-owner read of zip space detail is denied"):
            # The read gate runs before the handler, so a non-owner is denied
            # (401) rather than reaching the 404 the owner would get on an
            # un-analyzed space.
            expect_authz_denied(other.get(f"/api/spaces/{zq}"), 401, "read_denied")

        with ctx.check("non-owner delete of a zip space is denied and the space survives"):
            # Zip writes check read first, so a non-owner delete surfaces the
            # read denial (401), not write_denied.
            expect_authz_denied(
                other.delete(f"/api/spaces/{zq}", timeout=120), 401, "read_denied"
            )
            status = expect_dict(expect_status(owner.get(f"/api/spaces/{zq}/status"), 200))
            expect(
                status.get("status") == "PENDING_UPLOAD",
                f"a denied delete still changed the space to {status.get('status')!r}",
            )

        with ctx.check("space list hides spaces the caller cannot read"):
            owner_rows = expect_list(expect_status(owner.get("/api/spaces", timeout=120), 200))
            expect(
                any(r.get("space_id") == zip_id for r in owner_rows),
                "the owner's own zip space is missing from its space list",
            )
            other_rows = expect_list(expect_status(other.get("/api/spaces", timeout=120), 200))
            expect(
                all(r.get("space_id") != zip_id for r in other_rows),
                "a non-owner's space list leaked a zip space it cannot read",
            )

    # ── Public git space: readable by any signed-in user, writes still gated ──
    if not (ctx.git_space_id and ctx.git_space_ready):
        for name in enforcement_checks[5:]:
            ctx.skip(name, "no READY git space (run the git-space phase, or --reuse-space)")
        return

    gq = urllib.parse.quote(ctx.git_space_id)

    public_space = False
    with ctx.check("non-owner read of a public git space is allowed"):
        detail = expect_dict(expect_status(other.get(f"/api/spaces/{gq}"), 200))
        visibility = detail.get("visibility")
        if visibility != "public":
            raise Skipped(
                f"git space visibility is {visibility!r}, not public — a non-owner "
                f"read is expected to be denied here, not allowed"
            )
        public_space = True

    with ctx.check("non-creator write to a public git space is denied"):
        if not public_space:
            raise Skipped("git space is not public")
        resp = other.post(f"/api/spaces/{gq}/refresh", timeout=120)
        # The git-permission path may be unable to reach the provider; that is a
        # 503 authorization_unavailable, not a verdict, so don't fail on it.
        if resp.status == 503 and expect_dict(resp).get("error") == "authorization_unavailable":
            raise Skipped("git provider unreachable (authorization_unavailable)")
        expect_authz_denied(resp, 403, "write_denied")


# ── Phase 4: upload flow ─────────────────────────────────────────────────────


def phase_upload_space(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Upload space lifecycle")

    upload_url = None
    with ctx.check("POST /api/spaces/upload-url mints a presigned PUT"):
        resp = expect_status(
            client.post(
                "/api/spaces/upload-url",
                json_body={"space_name": "codekb-regression-upload", "summarize": False},
            ),
            200,
        )
        payload = expect_dict(resp)
        expect_keys(payload, "space_id", "upload_url", "s3_key", "expires_in")
        # Capture the handles before asserting anything else: a failure on a
        # detail must not cascade into skipping the whole upload phase.
        ctx.upload_space_id = payload["space_id"]
        upload_url = payload["upload_url"]
        c.note(f"space_id: {ctx.upload_space_id}")
        expect(payload["expires_in"] > 0, "expires_in should be positive")
        expect(payload["s3_key"], "s3_key should not be empty")

    with ctx.check("presigned upload URL is https"):
        if not upload_url:
            raise Skipped("no upload_url returned")
        expect(
            upload_url.startswith("https://"),
            f"presigned upload URL should be https, got {upload_url.split('://')[0]}://…",
        )

    with ctx.check("presigned upload URL allows a browser CORS preflight"):
        if not upload_url:
            raise Skipped("no upload_url returned")
        # The SPA uploads the zip with a cross-origin PUT, so the browser first
        # sends a CORS preflight (OPTIONS + Origin + Access-Control-Request-*).
        # The raw_put() below skips the preflight, so a missing bucket CORS rule
        # is invisible to it — this check reproduces what the browser requires.
        # Content-Type: application/zip is not CORS-safelisted, so the real
        # preflight lists content-type; assert the same is allowed.
        resp = raw_preflight(
            upload_url,
            origin="https://codekb-regression.example.com",
            request_method="PUT",
            request_headers="content-type",
        )
        expect_status(resp, 200)
        allow_origin = resp.headers.get("access-control-allow-origin")
        expect(
            allow_origin is not None,
            "S3 preflight returned no Access-Control-Allow-Origin: the KB bucket has no "
            "matching CORS rule, so a browser blocks the zip upload with 'Failed to fetch' "
            "(add a cors rule to the bucket — see StorageStack / setup_aws.sh)",
        )
        allow_methods = (resp.headers.get("access-control-allow-methods") or "").upper()
        expect(
            "PUT" in allow_methods,
            f"CORS preflight does not allow PUT (Access-Control-Allow-Methods: "
            f"{allow_methods or 'absent'})",
        )
        c.note(f"preflight allow-origin={allow_origin} allow-methods={allow_methods or 'n/a'}")

    if not (ctx.upload_space_id and upload_url):
        for name in (
            "upload-url space starts in PENDING_UPLOAD",
            "POST /api/spaces/upload by a non-owner is rejected",
            "POST /api/spaces/upload before PUT returns 400",
            "PUT the source zip to the presigned URL",
            "POST /api/spaces/upload starts analysis",
            "upload space analysis reaches READY",
        ):
            ctx.skip(name, "no presigned upload URL")
        return

    space_path = f"/api/spaces/{urllib.parse.quote(ctx.upload_space_id)}"

    with ctx.check("upload-url space starts in PENDING_UPLOAD"):
        resp = expect_status(client.get(f"{space_path}/status"), 200)
        status = expect_dict(resp)
        expect(
            status.get("status") == "PENDING_UPLOAD",
            f"expected PENDING_UPLOAD, got {status.get('status')!r}",
        )

    with ctx.check("POST /api/spaces/upload by a non-owner is rejected"):
        attacker = _authz_other_client(ctx)
        if attacker is None:
            raise Skipped("needs a second identity: pass --second-token with --token")
        rows = expect_list(expect_status(client.get("/api/spaces", timeout=120), 200))
        row = next((r for r in rows if r.get("space_id") == ctx.upload_space_id), None)
        expect(row is not None, f"{ctx.upload_space_id} missing from GET /api/spaces")
        owner = row.get("created_by")
        resp = attacker.post(
            "/api/spaces/upload",
            json_body={
                "space_id": ctx.upload_space_id,
                "space_name": "codekb-regression-upload",
            },
        )
        expect_status(resp, 404)
        # The rejected attempt must leave the owner key and status untouched.
        status = expect_dict(expect_status(client.get(f"{space_path}/status"), 200))
        expect(
            status.get("status") == "PENDING_UPLOAD",
            f"non-owner upload clobbered status to {status.get('status')!r}",
        )
        # created_by lives on the list row (SpaceSummary); it is absent from
        # /status (AnalysisStatus strips it) and the detail route 404s until the
        # space is READY, so read it from GET /api/spaces.
        rows = expect_list(expect_status(client.get("/api/spaces", timeout=120), 200))
        row = next((r for r in rows if r.get("space_id") == ctx.upload_space_id), None)
        expect(row is not None, f"{ctx.upload_space_id} missing from GET /api/spaces")
        expect(
            row.get("created_by") == owner,
            f"non-owner upload reassigned owner to {row.get('created_by')!r}",
        )

    with ctx.check("POST /api/spaces/upload before PUT returns 400"):
        resp = client.post(
            "/api/spaces/upload",
            json_body={
                "space_id": ctx.upload_space_id,
                "space_name": "codekb-regression-upload",
            },
        )
        expect_status(resp, 400)

    zip_bytes = build_sample_zip()

    with ctx.check("PUT the source zip to the presigned URL"):
        resp = raw_put(upload_url, zip_bytes, "application/zip")
        expect_status(resp, 200)
        c.note(f"uploaded {len(zip_bytes):,} bytes")

    with ctx.check("POST /api/spaces/upload starts analysis"):
        resp = client.post(
            "/api/spaces/upload",
            json_body={
                "space_id": ctx.upload_space_id,
                "space_name": "codekb-regression-upload",
                "summarize": False,
            },
            timeout=120,
        )
        expect_status(resp, 201)
        payload = expect_dict(resp)
        expect(
            payload.get("status") == "SUBMITTED",
            f"expected SUBMITTED, got {payload.get('status')!r}",
        )
        expect(
            payload.get("source_type") == "upload",
            f"expected source_type 'upload', got {payload.get('source_type')!r}",
        )

    with ctx.check("upload space analysis reaches READY"):
        status = poll_space_status(
            ctx, ctx.upload_space_id, timeout=ctx.args.analysis_timeout, label="upload analysis"
        )
        state = status.get("status")
        if state == "FAILED":
            raise CheckError(f"analysis FAILED: {status.get('error') or 'no error reported'}")
        expect(state == "READY", f"expected READY, got {state!r}")
        ctx.upload_space_ready = True
        c.note(f"components={status.get('component_count')}")

    if not ctx.upload_space_ready:
        return

    with ctx.check("uploaded project yields parsed components"):
        resp = expect_status(client.get(f"{space_path}/components", timeout=180), 200)
        components = expect_list(resp)
        expect(components, "no components parsed from the uploaded project")
        names = {c.get("name") for c in components}
        # These exist in build_sample_zip(); if the parser regressed they vanish.
        for expected_name in ("Order", "OrderService", "validate_order"):
            expect(
                any(expected_name == n or str(n).endswith("." + expected_name) for n in names),
                f"expected component '{expected_name}' from the sample project",
            )
        c.note(f"{len(components)} component(s) parsed")

    with ctx.check("POST /{id}/refresh on an upload-sourced space returns 400"):
        # Space refresh is git-only; an upload space must be rejected, not crash.
        expect_status(client.post(f"{space_path}/refresh"), 400)

    # Lives here rather than in the queries phase because this is the first
    # point at which two spaces exist to scope a search across.
    with ctx.check("GET /api/search across two spaces stays within scope"):
        if not (ctx.git_space_id and ctx.git_space_ready):
            raise Skipped("need both a git and an upload space")
        if not ctx.vectors_available:
            raise Skipped("no vectors in either space (summarize=false)")
        scope = {ctx.git_space_id, ctx.upload_space_id}
        resp = expect_status(
            client.get(
                "/api/search",
                params={
                    "q": ctx.args.search_query,
                    "spaces": ",".join(sorted(scope)),
                    "limit": 10,
                },
                timeout=180,
            ),
            200,
        )
        hits = expect_list(resp)
        expect_vector_hits(hits, space_ids=scope)
        c.note(f"{len(hits)} hit(s) across 2 spaces")


# ── Phase 5: hyperspaces ─────────────────────────────────────────────────────


def phase_hyperspaces(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Hyperspaces")

    members = [
        sid
        for sid, ready in (
            (ctx.git_space_id, ctx.git_space_ready),
            (ctx.upload_space_id, ctx.upload_space_ready),
        )
        if sid and ready
    ]

    # A hyperspace needs two READY spaces. If this run only produced one, fall
    # back to borrowing an existing READY space from the instance.
    if len(members) < 2:
        resp = client.get("/api/spaces")
        if resp.status == 200:
            for space in resp.json():
                sid = space.get("space_id")
                if space.get("status") == "READY" and sid not in members:
                    members.append(sid)
                    if len(members) >= 2:
                        break

    if len(members) < 2:
        for name in (
            "POST /api/hyperspaces creates a hyperspace",
            "GET /api/hyperspaces includes the new hyperspace",
            "GET /api/hyperspaces/{id} enriches member spaces",
            "PUT /api/hyperspaces/{id} accepts a single space",
            "PUT /api/hyperspaces/{id} rejects an empty space list",
        ):
            ctx.skip(name, "fewer than 2 READY spaces available")
        return

    members = members[:2]
    c.note(f"members: {', '.join(members)}")

    with ctx.check("POST /api/hyperspaces creates a hyperspace"):
        resp = client.post(
            "/api/hyperspaces",
            json_body={"name": "codekb-regression-hyperspace", "space_ids": members},
            timeout=120,
        )
        expect_status(resp, 201)
        payload = expect_dict(resp)
        expect_keys(payload, "name", "space_ids")
        hid = payload.get("hyperspace_id") or payload.get("id")
        expect(hid, f"no hyperspace id in the response: {payload.keys()}")
        ctx.hyperspace_id = hid
        expect(
            sorted(payload["space_ids"]) == sorted(members),
            f"space_ids came back as {payload['space_ids']}",
        )
        c.note(f"hyperspace_id: {hid}")

    if not ctx.hyperspace_id:
        return

    hpath = f"/api/hyperspaces/{urllib.parse.quote(ctx.hyperspace_id)}"

    with ctx.check("GET /api/hyperspaces includes the new hyperspace"):
        resp = expect_status(client.get("/api/hyperspaces", timeout=120), 200)
        listed = expect_list(resp)
        ids = {h.get("hyperspace_id") or h.get("id") for h in listed}
        expect(ctx.hyperspace_id in ids, "newly created hyperspace is missing from the list")

    with ctx.check("GET /api/hyperspaces/{id} enriches member spaces"):
        resp = expect_status(client.get(hpath, timeout=120), 200)
        payload = expect_dict(resp)
        expect_keys(payload, "space_ids", "spaces", "invalid", "missing_space_ids")
        expect(
            len(payload["spaces"]) == len(members),
            f"expected {len(members)} enriched spaces, got {len(payload['spaces'])}",
        )
        for space in payload["spaces"]:
            expect_keys(space, "space_id", "status")
        expect(not payload["invalid"], f"hyperspace flagged invalid: {payload}")
        expect(not payload["missing_space_ids"], f"missing members: {payload['missing_space_ids']}")

    with ctx.check("PUT /api/hyperspaces/{id} accepts a single space"):
        resp = expect_status(
            client.put(hpath, json_body={"space_ids": [members[0]]}, timeout=120), 200
        )
        payload = expect_dict(resp)
        expect(
            payload.get("space_ids") == [members[0]],
            f"shrink to one member did not stick: {payload.get('space_ids')}",
        )

    with ctx.check("PUT /api/hyperspaces/{id} rejects an empty space list"):
        resp = client.put(hpath, json_body={"space_ids": []}, timeout=120)
        expect_status(resp, 400)


# ── Phase 6: Space Overview generation (full tier) ──────────────────────────


def phase_overview(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Space Overview generation")

    names = ["POST /{id}/generate-overview starts generation"] + [
        f"GET /{{id}}/overview/{key}" for key in OVERVIEW_DOC_KEYS
    ]
    if not (ctx.git_space_id and ctx.git_space_ready):
        for name in names:
            ctx.skip(name, "no READY git space available")
        return

    space_path = f"/api/spaces/{urllib.parse.quote(ctx.git_space_id)}"

    with ctx.check("GET /{id}/overview/{unknown} returns 404"):
        expect_status(client.get(f"{space_path}/overview/not_a_real_doc"), 404)

    generated = False
    with ctx.check("POST /{id}/generate-overview starts generation"):
        resp = client.post(f"{space_path}/generate-overview", timeout=120)
        expect_status(resp, 202)
        payload = expect_dict(resp)
        expect(
            payload.get("status") == "SUBMITTED",
            f"expected SUBMITTED, got {payload.get('status')!r}",
        )
        generated = True

    if not generated:
        for key in OVERVIEW_DOC_KEYS:
            ctx.skip(f"GET /{{id}}/overview/{key}", "generation never started")
        return

    completed = False
    with ctx.check("overview generation completes"):
        status = poll_overview_status(ctx, ctx.git_space_id, timeout=ctx.args.overview_timeout)
        state = (status.get("overview_status") or "").lower()
        if state == "failed":
            raise CheckError("overview generation reported failed")
        expect(state == "completed", f"expected completed, got {state!r}")
        docs = status.get("space_overview_docs") or []
        expect(docs, "space_overview_docs is empty after a completed run")
        for doc in docs:
            expect_keys(doc, "key", "title", "s3_path")
        completed = True
        c.note(f"{len(docs)} document(s) in the catalog")

    for key in OVERVIEW_DOC_KEYS:
        with ctx.check(f"GET /{{id}}/overview/{key}"):
            if not completed:
                raise Skipped("generation did not complete")
            resp = client.get(f"{space_path}/overview/{key}", timeout=120)
            expect_status(resp, 200)
            expect(
                "markdown" in resp.content_type,
                f"expected text/markdown, got {resp.content_type!r}",
            )
            expect(len(resp.text.strip()) > 50, f"{key} document looks empty")


# ── Phase 6b: Hyperspace Overview generation (full tier) ────────────────────


def phase_hyperspace_overview(ctx: Context) -> None:
    """Exercise the hyperspace overview (summary-of-synthesis-from-member-spaces).

    Depends on a hyperspace existing, which phase_hyperspaces creates. Generation
    first ensures each READY member has an up-to-date Space Overview (phase 1,
    auto-generated if missing/stale) and then synthesizes the system-level docs
    (phase 2), so this is heavier and slower than a single Space Overview and
    reuses the same --overview-timeout budget.
    """
    c, client = ctx.console, ctx.client
    c.heading("Hyperspace Overview generation")

    names = ["POST /hyperspaces/{id}/generate-overview starts generation"] + [
        f"GET /hyperspaces/{{id}}/overview/{key}" for key in HYPERSPACE_OVERVIEW_DOC_KEYS
    ]
    if not ctx.hyperspace_id:
        for name in names:
            ctx.skip(name, "no hyperspace created this run")
        return

    hpath = f"/api/hyperspaces/{urllib.parse.quote(ctx.hyperspace_id)}"

    with ctx.check("GET /hyperspaces/{id}/overview/{unknown} returns 404"):
        expect_status(client.get(f"{hpath}/overview/not_a_real_doc"), 404)

    generated = False
    with ctx.check("POST /hyperspaces/{id}/generate-overview starts generation"):
        resp = client.post(f"{hpath}/generate-overview", timeout=120)
        expect_status(resp, 202)
        payload = expect_dict(resp)
        expect(
            payload.get("status") == "SUBMITTED",
            f"expected SUBMITTED, got {payload.get('status')!r}",
        )
        generated = True

    if not generated:
        for key in HYPERSPACE_OVERVIEW_DOC_KEYS:
            ctx.skip(f"GET /hyperspaces/{{id}}/overview/{key}", "generation never started")
        return

    completed = False
    with ctx.check("hyperspace overview generation completes"):
        detail = poll_hyperspace_overview_status(
            ctx, ctx.hyperspace_id, timeout=ctx.args.overview_timeout
        )
        state = (detail.get("overview_status") or "").lower()
        if state == "failed":
            raise CheckError(
                f"hyperspace overview reported failed: {detail.get('overview_error')}"
            )
        expect(state == "completed", f"expected completed, got {state!r}")
        docs = detail.get("hyperspace_overview_docs") or []
        expect(docs, "hyperspace_overview_docs is empty after a completed run")
        for doc in docs:
            expect_keys(doc, "key", "title", "s3_path")
        keys = {d.get("key") for d in docs}
        expect(
            keys == set(HYPERSPACE_OVERVIEW_DOC_KEYS),
            f"catalog keys {sorted(keys)} != expected {sorted(HYPERSPACE_OVERVIEW_DOC_KEYS)}",
        )
        completed = True
        c.note(f"{len(docs)} document(s) in the catalog")

    for key in HYPERSPACE_OVERVIEW_DOC_KEYS:
        with ctx.check(f"GET /hyperspaces/{{id}}/overview/{key}"):
            if not completed:
                raise Skipped("generation did not complete")
            resp = client.get(f"{hpath}/overview/{key}", timeout=120)
            expect_status(resp, 200)
            expect(
                "markdown" in resp.content_type,
                f"expected text/markdown, got {resp.content_type!r}",
            )
            expect(len(resp.text.strip()) > 50, f"{key} document looks empty")


# ── Phase 7: space refresh (full tier) ──────────────────────────────────────


def phase_refresh(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Space refresh")

    if not (ctx.git_space_id and ctx.git_space_ready):
        ctx.skip("POST /{id}/refresh runs a space refresh", "no READY git space available")
        return

    space_path = f"/api/spaces/{urllib.parse.quote(ctx.git_space_id)}"

    resp = client.get(f"{space_path}/status")
    runs_before = len((resp.json().get("refresh_pipeline_runs") or [])) if resp.status == 200 else 0

    started = False
    with ctx.check("POST /{id}/refresh runs a space refresh"):
        resp = client.post(f"{space_path}/refresh", timeout=120)
        expect_status(resp, 202)
        payload = expect_dict(resp)
        expect(
            payload.get("status") == "SUBMITTED",
            f"expected SUBMITTED, got {payload.get('status')!r}",
        )
        started = True

    with ctx.check("space refresh returns the space to READY"):
        if not started:
            raise Skipped("refresh never started")
        status = poll_space_status(
            ctx, ctx.git_space_id, timeout=ctx.args.analysis_timeout, label="refresh"
        )
        state = status.get("status")
        if state == "FAILED":
            raise CheckError(f"refresh FAILED: {status.get('error') or 'no error reported'}")
        expect(state == "READY", f"expected READY, got {state!r}")
        if status.get("refresh_warning"):
            c.warn(f"refresh_warning: {status['refresh_warning']}")

    with ctx.check("refresh records a run and a result summary"):
        if not started:
            raise Skipped("refresh never started")
        resp = expect_status(client.get(f"{space_path}/status"), 200)
        status = expect_dict(resp)
        runs = status.get("refresh_pipeline_runs") or []
        expect(
            len(runs) > runs_before,
            f"refresh_pipeline_runs did not grow ({runs_before} → {len(runs)})",
        )

        # TODO "refresh_result" is missing in the system. Will be added when the space refresh
        # feature is redesigned.
        #
        # result = status.get("refresh_result")
        # expect(isinstance(result, dict), "refresh_result is missing")
        # expect_keys(
        #     result,
        #     "files_added",
        #     "files_modified",
        #     "files_removed",
        #     "files_unchanged",
        #     "components_added",
        #     "components_removed",
        #     "vectors_upserted",
        #     "vectors_deleted",
        #     "duration_s",
        # )
        # c.note(
        #     f"unchanged={result['files_unchanged']} added={result['files_added']} "
        #     f"modified={result['files_modified']} removed={result['files_removed']}"
        # )


# ── Phase 8: agent (full tier) ──────────────────────────────────────────────


def phase_agent(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Agent")

    with ctx.check("POST /api/agent/chat with an unknown space in scope"):
        # With authz off an unknown space id is not a hard error: the agent
        # scopes to it, finds nothing, and still answers 200. With authz on the
        # scope gate refuses a scope that names a space the caller can't read.
        resp = client.post(
            "/api/agent/chat",
            json_body={"message": "hello", "space_ids": [MISSING_SPACE_ID]},
            timeout=180,
        )
        if ctx.authz_enforced:
            expect_authz_denied(resp, 401, "read_denied")
        else:
            expect_status(resp, 200)
            payload = expect_dict(resp)
            expect_keys(payload, "space_ids", "message", "response", "history")
            expect(
                payload["space_ids"] == [MISSING_SPACE_ID],
                f"space_ids should echo the requested scope, got {payload['space_ids']!r}",
            )
            expect(payload["response"].strip(), "unknown-space query returned an empty response")

    if not (ctx.git_space_id and ctx.git_space_ready):
        for name in (
            "POST /api/agent/chat without space_ids searches every space",
            "POST /api/agent/chat answers a question",
            "POST /api/agent/chat continues a conversation",
            "POST /api/agent/chat supports impact_analysis mode",
            "POST /api/agent/chat/stream streams SSE events",
        ):
            ctx.skip(name, "no READY git space available")
        return

    with ctx.check("POST /api/agent/chat without space_ids searches every space"):
        # Omitting space_ids scopes the agent to all spaces. The scope stays None
        # rather than being expanded to every id, so semantic search runs
        # unfiltered instead of building an oversized metadata filter.
        resp = client.post(
            "/api/agent/chat",
            json_body={"message": "Which repositories can you see? Answer in one sentence."},
            timeout=ctx.args.agent_timeout,
        )
        expect_status(resp, 200)
        payload = expect_dict(resp)
        expect_keys(payload, "space_ids", "message", "response", "history")
        if ctx.authz_enforced:
            # Authz resolves "all spaces" to the caller's readable set, so the
            # scope comes back as an explicit list rather than staying null.
            expect(
                isinstance(payload["space_ids"], list) and payload["space_ids"],
                f"authz should resolve the scope to the caller's readable spaces, "
                f"got {payload['space_ids']!r}",
            )
        else:
            expect(
                payload["space_ids"] is None,
                f"space_ids should stay null for an all-spaces query, got {payload['space_ids']!r}",
            )
        expect(payload["response"].strip(), "all-spaces query returned an empty response")

    history: list[Any] = []
    with ctx.check("POST /api/agent/chat answers a question"):
        resp = client.post(
            "/api/agent/chat",
            json_body={
                "message": "What does this codebase do? Answer in two sentences.",
                "space_ids": [ctx.git_space_id],
            },
            timeout=ctx.args.agent_timeout,
        )
        expect_status(resp, 200)
        payload = expect_dict(resp)
        expect_keys(payload, "space_ids", "message", "response", "history")
        history = payload["history"]  # captured first so turn 2 can still run
        c.note(f"{len(payload['response'])} chars, history={len(history)} message(s)")
        expect(payload["response"].strip(), "agent returned an empty response")
        expect(history, "agent returned empty history")

    with ctx.check("POST /api/agent/chat continues a conversation"):
        if not history:
            raise Skipped("no history from the first turn")
        resp = client.post(
            "/api/agent/chat",
            json_body={
                "message": "Name one component you mentioned.",
                "space_ids": [ctx.git_space_id],
                "history": history,
            },
            timeout=ctx.args.agent_timeout,
        )
        expect_status(resp, 200)
        payload = expect_dict(resp)
        expect(payload["response"].strip(), "agent returned an empty response on turn 2")
        expect(
            len(payload["history"]) > len(history),
            "history did not grow across turns — the conversation is not threading",
        )

    with ctx.check("POST /api/agent/chat supports impact_analysis mode"):
        resp = client.post(
            "/api/agent/chat",
            json_body={
                "message": "What breaks if the main entry point changes?",
                "space_ids": [ctx.git_space_id],
                "mode": "impact_analysis",
            },
            timeout=ctx.args.agent_timeout,
        )
        expect_status(resp, 200)
        payload = expect_dict(resp)
        expect(payload["response"].strip(), "impact_analysis returned an empty response")

    with ctx.check("POST /api/agent/chat/stream streams SSE events"):
        stream = client.open_stream(
            "POST",
            "/api/agent/chat/stream",
            json_body={
                "message": "List two components in one short sentence.",
                "space_ids": [ctx.git_space_id],
            },
            timeout=ctx.args.agent_timeout,
        )
        events: list[str] = []
        saw_done = False
        try:
            expect(
                "text/event-stream" in stream.headers.get("Content-Type", ""),
                f"expected text/event-stream, got {stream.headers.get('Content-Type')!r}",
            )
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("event:"):
                    name = line.split(":", 1)[1].strip()
                    events.append(name)
                    if name == "done":
                        saw_done = True
                        break
        finally:
            stream.close()
        expect(events, "no SSE events received")
        expect(saw_done, f"stream ended without a 'done' event; saw: {sorted(set(events))}")
        c.note(f"event types: {', '.join(sorted(set(events)))}")


# ── Phase 9: teardown ────────────────────────────────────────────────────────


# ── Phase: MCP ──────────────────────────────────────────────────────────────
# MCP is how agents consume CodeKB, and it was entirely unexercised here. Most
# tools have an HTTP twin whose logic is covered elsewhere, so what this phase
# adds is the transport itself plus the one capability with no route at all:
# get_call_graph_with_source, which is also the only reader that opens source
# files directly.


def _function_component_candidates(ctx: Context, limit: int = 12) -> list[str]:
    """Return function/method component names to try, most-connected first.

    Returns a list rather than a single name because not every function has a
    call graph — a leaf with no recorded edges makes get_call_graph_with_source
    return nothing, and picking only the first candidate turned the one check
    that matters into a SKIP. The caller tries these in order until one yields a
    graph. Ordering by descending call-graph fan-out (when the listing reports
    it) puts the components most likely to have a graph first, so the common case
    costs one call.

    Fetches the listing directly, so ``--phase mcp --reuse-space`` works alone.
    """
    if not ctx.git_space_id:
        return []
    resp = ctx.client.get(
        f"/api/spaces/{urllib.parse.quote(ctx.git_space_id)}/components", timeout=180
    )
    if resp.status != 200:
        return []
    try:
        components = resp.json()
    except Exception:
        return []
    if not isinstance(components, list):
        return []

    functions = [
        comp
        for comp in components
        if isinstance(comp, dict)
        and comp.get("type") in ("function", "method")
        and comp.get("name")
    ]

    def fan_out(comp: dict) -> int:
        # The listing may or may not carry an outgoing-call count; when it does,
        # a higher one is likelier to produce a multi-node graph. Absent it,
        # order is preserved (sort is stable).
        for key in ("call_count", "calls", "out_degree", "fan_out"):
            value = comp.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, list):
                return len(value)
        return 0

    functions.sort(key=fan_out, reverse=True)
    return [comp["name"] for comp in functions[:limit]]


def phase_mcp(ctx: Context) -> None:
    c = ctx.console
    c.heading("MCP tools (streamable HTTP)")
    mcp = McpClient(ctx.client)

    initialized = False
    with ctx.check("POST /mcp/mcp initialize completes the handshake"):
        result = mcp.initialize()
        expect(isinstance(result, dict), "initialize returned no result object")
        expect(
            result.get("protocolVersion"),
            f"initialize reported no protocolVersion: {str(result)[:200]}",
        )
        server = result.get("serverInfo") or {}
        c.note(f"server {server.get('name', '?')}, protocol {result.get('protocolVersion')}")
        initialized = True

    tool_names: set[str] = set()
    with ctx.check("tools/list advertises the expected tools"):
        if not initialized:
            raise Skipped("MCP handshake failed")
        tools = mcp.list_tools()
        tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
        # A representative set rather than all of them: this should not break
        # every time a tool is legitimately added or renamed, but these are the
        # ones the product is built around.
        essential = {
            "list_spaces",
            "find_component",
            "trace_flow",
            "show_dependencies",
            "get_call_graph_with_source",
            "get_component_from_description",
        }
        missing = sorted(essential - tool_names)
        expect(not missing, f"tools/list is missing {', '.join(missing)}")
        for tool in tools:
            expect(
                isinstance(tool, dict) and tool.get("inputSchema"),
                f"tool {tool.get('name', '?')!r} advertises no inputSchema",
            )
        c.note(f"{len(tool_names)} tool(s) advertised")

    with ctx.check("tools/call list_spaces returns the space under test"):
        if not initialized:
            raise Skipped("MCP handshake failed")
        if not ctx.git_space_id:
            raise Skipped("no git space available")
        text = mcp.call_tool("list_spaces", {})
        expect(
            ctx.git_space_id in text,
            f"list_spaces did not mention {ctx.git_space_id}",
        )

    with ctx.check("tools/call get_stats accepts a bare space id"):
        # A space-qualified tool, to exercise the other MCP argument shape
        # (plain space_id) distinct from the component "space::name" form below.
        if not initialized:
            raise Skipped("MCP handshake failed")
        if not ctx.git_space_id:
            raise Skipped("no git space available")
        text = mcp.call_tool("get_stats", {"space_id": ctx.git_space_id})
        expect(text.strip(), "get_stats returned an empty body")

    with ctx.check("tools/call get_call_graph_with_source returns real source"):
        if not initialized:
            raise Skipped("MCP handshake failed")
        if not (ctx.git_space_id and ctx.git_space_ready):
            raise Skipped("no READY git space available")
        candidates = _function_component_candidates(ctx)
        if not candidates:
            raise Skipped("no function/method components discovered")

        # Try candidates until one has a call graph. A leaf with no recorded
        # edges yields nothing, and skipping on the first such component is what
        # let this check pass vacuously before.
        graph_text: str | None = None
        used: str | None = None
        tried = 0
        for name in candidates:
            tried += 1
            # The listing already reports each component's fully qualified id
            # ("space::file.Class.method"), which is exactly what the tool wants.
            # Prefixing it again produces "space::space::..." and every lookup
            # misses — the bug that made this check skip on every candidate.
            qualified = name if "::" in name else f"{ctx.git_space_id}::{name}"
            text = mcp.call_tool(
                "get_call_graph_with_source", {"component_id": qualified}, timeout=300
            )
            if "not found or is not a function/method" in text:
                continue
            graph_text, used = text, qualified
            break

        if graph_text is None:
            # Not a pass: every candidate lacking a graph in a healthy space
            # points at an ID-convention mismatch, not just leaf selection.
            raise Skipped(f"no call graph among {tried} function/method candidate(s)")

        # The check the suite was missing. The reader swallows a failed open into
        # its return value, so a broken source read comes back as a perfectly
        # successful response whose body is the error message.
        expect_real_source(graph_text, "get_call_graph_with_source")
        # And the paths it reports must name the durable cache location, not a
        # working tree the pipeline has already deleted.
        expect_no_working_tree_path(graph_text, "get_call_graph_with_source output")
        expect(
            "Source:" in graph_text,
            "get_call_graph_with_source returned no Source: section — "
            "the source assertion would be vacuous",
        )
        c.note(f"traced {used} ({tried} candidate(s) tried)")

    # ── Authorization gates on the MCP layer ──────────────────────────────────
    # MCP is a SEPARATE enforcement point from REST: its own read gates live in
    # mcp/tools/common.py, and a denial comes back as ordinary tool-result text
    # (message-first, no isError), not an HTTP 401/403 — so these assert on the
    # returned text, not a status code. They reuse what the authz phase learned
    # (ctx.authz_enforced) and created (ctx.authz_space_id, an owner-only zip
    # space); when the authz phase didn't run this round, enforcement is probed
    # here and the probe space is minted.
    mcp_authz_names = (
        "MCP get_stats denies a non-owner on an owner-only space",
        "MCP get_stats admits the owner on the same space",
        "MCP list_spaces hides a space the caller cannot read",
    )

    enforced = ctx.authz_enforced
    if enforced is None and initialized:
        # authz phase not in this run (e.g. --phase mcp): reuse the signal that
        # phase uses — gate_read denies a *missing* space (401) only when the
        # switch is on; with it off the route answers 200 NOT_FOUND.
        probe = ctx.client.get(f"/api/spaces/{urllib.parse.quote(MISSING_SPACE_ID)}/status")
        enforced = probe.status == 401
        ctx.authz_enforced = enforced

    other = _authz_other_client(ctx) if (initialized and enforced) else None

    def _mcp_authz_precondition() -> None:
        if not initialized:
            raise Skipped("MCP handshake failed")
        if not enforced:
            raise Skipped("authorization is disabled on this instance")
        if other is None:
            raise Skipped("needs a second identity; pass --second-token on a JWT deployment")

    # Owner-admit runs first because it also acquires the owner-only space.
    with ctx.check(mcp_authz_names[1]):
        _mcp_authz_precondition()
        zip_id = ctx.authz_space_id
        if not zip_id:
            resp = ctx.client.post(
                "/api/spaces/upload-url",
                json_body={"space_name": "codekb-regression-authz-mcp", "summarize": False},
            )
            if resp.status == 403:
                raise Skipped("zip upload is disabled (CODEKB_ZIP_UPLOAD_ENABLED=false)")
            expect_status(resp, 200)
            zip_id = expect_dict(resp).get("space_id")
            expect(zip_id, "upload-url returned no space_id")
            ctx.authz_space_id = zip_id
        # The owner passes the gate; the body may still fail on an un-analyzed
        # space, but it must not be the authorization denial.
        text = McpClient(ctx.client).call_tool("get_stats", {"space_id": zip_id})
        expect(
            "do not have access to this space" not in text.lower(),
            f"an MCP tool denied the owner access to its own space: {text[:200]}",
        )

    with ctx.check(mcp_authz_names[0]):
        _mcp_authz_precondition()
        if not ctx.authz_space_id:
            raise Skipped("no owner-only space available to probe")
        # A denial is returned as plain tool text (no isError), so call_tool does
        # not raise — the gate not firing would instead return real stats.
        text = McpClient(other).call_tool("get_stats", {"space_id": ctx.authz_space_id})
        expect(
            "do not have access to this space" in text.lower(),
            f"a non-owner MCP get_stats was not denied — the read gate is not wired "
            f"on the MCP tool. Got: {text[:200]}",
        )

    with ctx.check("MCP trace_flow denies a non-owner (gate wired across tool modules)"):
        # get_stats lives in query_tools; trace_flow lives in flow_tools and has
        # its own gate_space_read call. Spot-check it so a gate missing from one
        # module isn't masked by another. The component need not exist: the gate
        # runs before the body, so a non-owner is denied on the space_id prefix.
        _mcp_authz_precondition()
        if not ctx.authz_space_id:
            raise Skipped("no owner-only space available to probe")
        text = McpClient(other).call_tool(
            "trace_flow", {"component": f"{ctx.authz_space_id}::anything"}
        )
        expect(
            "do not have access to this space" in text.lower(),
            f"a non-owner MCP trace_flow was not denied — the read gate is missing "
            f"from flow_tools. Got: {text[:200]}",
        )

    with ctx.check(mcp_authz_names[2]):
        _mcp_authz_precondition()
        if not ctx.authz_space_id:
            raise Skipped("no owner-only space available to probe")
        owner_text = McpClient(ctx.client).call_tool("list_spaces", {})
        expect(
            ctx.authz_space_id in owner_text,
            "the owner's MCP list_spaces omitted its own space — the hiding "
            "assertion below would be vacuous",
        )
        other_text = McpClient(other).call_tool("list_spaces", {})
        expect(
            ctx.authz_space_id not in other_text,
            "a non-owner's MCP list_spaces leaked a space it cannot read — "
            "filter_readable_spaces is not applied on the MCP layer",
        )

    with ctx.check("tools/call rejects an unknown tool cleanly"):
        if not initialized:
            raise Skipped("MCP handshake failed")
        try:
            mcp.call_tool("codekb_regression_no_such_tool", {})
        except CheckError:
            return  # a JSON-RPC error or isError result is the expected outcome
        raise CheckError("an unknown tool name was accepted")


def phase_teardown(ctx: Context) -> None:
    c, client = ctx.console, ctx.client
    c.heading("Teardown")

    if ctx.args.keep:
        c.note("--keep set; leaving created resources in place")
        for label, sid in (
            ("git space", ctx.git_space_id if ctx.created_git_space else None),
            ("upload space", ctx.upload_space_id),
            ("authz probe space", ctx.authz_space_id),
            ("hyperspace", ctx.hyperspace_id),
        ):
            if sid:
                c.note(f"{label}: {sid}")
        return

    # Delete the hyperspace first: it references the spaces.
    if ctx.hyperspace_id:
        hid = urllib.parse.quote(ctx.hyperspace_id)
        with ctx.check("DELETE /api/hyperspaces/{id}"):
            expect_status(client.delete(f"/api/hyperspaces/{hid}", timeout=120), 200)
        with ctx.check("deleted hyperspace is gone"):
            expect_status(client.get(f"/api/hyperspaces/{hid}", timeout=120), 404)

    # Deletion runs in the background: DELETE returns 202 immediately and the
    # space flips to NOT_FOUND once the async task finishes. Vector cleanup can
    # still take many minutes on a large S3 Vectors index, so this generous
    # budget bounds how long we poll status to completion. Pass --keep to skip
    # teardown entirely.
    _DELETE_TIMEOUT = 900.0
    # Only delete the git space if this run created it — never a --reuse-space.
    to_delete = [
        ("git space", ctx.git_space_id if ctx.created_git_space else None),
        ("upload space", ctx.upload_space_id),
        ("authz probe space", ctx.authz_space_id),
    ]
    for label, sid in to_delete:
        if not sid:
            continue
        quoted = urllib.parse.quote(sid)
        with ctx.check(f"DELETE /api/spaces/{{id}} ({label})"):
            # Async delete: 202 Accepted, deletion proceeds in the background.
            expect_status(client.delete(f"/api/spaces/{quoted}", timeout=120), 202)
        with ctx.check(f"deleted {label} reports NOT_FOUND"):
            status = poll_space_status(
                ctx,
                sid,
                terminal=("NOT_FOUND", "FAILED"),
                timeout=_DELETE_TIMEOUT,
                label=f"delete {label}",
                gone_on_denied=bool(ctx.authz_enforced),
            )
            expect(
                status.get("status") == "NOT_FOUND",
                f"expected NOT_FOUND after delete, got {status.get('status')!r}",
            )

    if ctx.args.reuse_space:
        c.note(f"left --reuse-space {ctx.args.reuse_space} untouched")


# ── Report ───────────────────────────────────────────────────────────────────


def print_report(ctx: Context, elapsed: float) -> int:
    c = ctx.console
    passed = [r for r in ctx.results if r.status == "PASS"]
    failed = [r for r in ctx.results if r.status == "FAIL"]
    skipped = [r for r in ctx.results if r.status == "SKIP"]

    c.heading("Summary")
    c.line(
        f"  {c.green(f'{len(passed)} passed')}   "
        f"{c.red(f'{len(failed)} failed') if failed else '0 failed'}   "
        f"{c.yellow(f'{len(skipped)} skipped') if skipped else '0 skipped'}"
        f"   {c.dim(f'in {elapsed:.1f}s' if elapsed >= 10 else f'in {elapsed:.2f}s')}"
    )

    if skipped:
        c.line()
        c.line(c.yellow("  Skipped:"))
        for r in skipped:
            c.line(f"    - {r.name}")
            if r.detail:
                c.line(f"      {c.dim(r.detail)}")

    if failed:
        c.line()
        c.line(c.red("  Failed:"))
        for r in failed:
            c.line(f"    - {r.name}")
            c.line(f"      {c.red(r.detail)}")
        c.line()
        c.line(c.red(f"  REGRESSION: {len(failed)} check(s) failed."))
        return 1

    if skipped:
        c.line()
        c.line(
            c.yellow(
                "  All executed checks passed, but some were skipped — review the list above\n"
                "  before treating this as a clean run."
            )
        )
    else:
        c.line()
        c.line(c.green("  All checks passed."))
    return 0


# ── Entry point ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Phase:
    name: str
    run: Any
    description: str
    # Opt-in phases run only with --with-overviews (or when named in --phase).
    # These are the slow, heavy-Bedrock Overview generations; everything else
    # runs in the single default pass.
    optional: bool = False


# Ordered: later phases depend on state established by earlier ones.
# `preflight` always runs; `teardown` always runs last unless --keep.
PHASES: list[Phase] = [
    Phase("readonly", lambda ctx: phase_readonly(ctx), "GET endpoints and listings"),
    Phase("negative", lambda ctx: phase_negative(ctx), "404/422/400 paths and SSRF controls"),
    Phase("headers", lambda ctx: phase_security_headers(ctx), "security response headers"),
    Phase("git-space", lambda ctx: phase_git_space(ctx), "create a space from git, poll to READY"),
    Phase("git-credentials", lambda ctx: phase_git_credentials(ctx), "git-credentials API + private/public clone (needs --github-pat-token)"),
    Phase("queries", lambda ctx: phase_space_queries(ctx), "space detail, components, flow, deps, search"),
    Phase("authz", lambda ctx: phase_authz(ctx), "git-permission space authorization deny/allow contract"),
    Phase("upload", lambda ctx: phase_upload_space(ctx), "presigned upload flow end to end"),
    Phase("hyperspaces", lambda ctx: phase_hyperspaces(ctx), "hyperspace CRUD"),
    Phase("mcp", lambda ctx: phase_mcp(ctx), "MCP transport and tools, incl. source reads"),
    Phase("refresh", lambda ctx: phase_refresh(ctx), "space refresh"),
    Phase("agent", lambda ctx: phase_agent(ctx), "agent chat, multi-turn, SSE (Bedrock)"),
    Phase("overview", lambda ctx: phase_overview(ctx), "Space Overview generation (Bedrock, slow)", optional=True),
    Phase("hyperspace-overview", lambda ctx: phase_hyperspace_overview(ctx), "Hyperspace Overview synthesis (Bedrock, slow)", optional=True),
]

PHASE_NAMES = [p.name for p in PHASES]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Black-box regression suite for the CodeKB backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "phases (--phase):\n"
            + "\n".join(
                f"  {p.name:<20} {p.description}" + ("  [--with-overviews]" if p.optional else "")
                for p in PHASES
            )
            + "\n\nexamples:\n"
            "  # the whole suite in one pass (everything except the slow overviews)\n"
            "  python3 codekb_regression.py --base-url http://localhost:8000\n\n"
            "  # also generate Space + Hyperspace Overviews (heavy Bedrock, tens of minutes)\n"
            "  python3 codekb_regression.py --base-url http://localhost:8000 --with-overviews\n\n"
            "  # fast read-only sanity check, makes no writes\n"
            "  python3 codekb_regression.py --phase readonly,negative,headers\n\n"
            "  # a different repository needs a matching search query\n"
            "  python3 codekb_regression.py --git-url https://github.com/org/repo.git \\\n"
            "      --search-query 'parse an incoming webhook payload'\n\n"
            "  # reuse an already-analyzed space to iterate quickly on one phase\n"
            "  python3 codekb_regression.py --phase agent --reuse-space my-repo-a1b2c3d4\n"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CODEKB_BASE_URL", "http://localhost:8000"),
        help="Base URL of the running CodeKB container (default: %(default)s)",
    )
    parser.add_argument(
        "--with-overviews",
        action="store_true",
        help=(
            "Also run Space and Hyperspace Overview generation. These make heavy "
            "Bedrock calls and add tens of minutes, so they are off by default; "
            "every other feature runs in the single default pass."
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CODEKB_TOKEN"),
        help="Cognito JWT for a deployment with auth enforced",
    )
    parser.add_argument(
        "--second-token",
        default=os.environ.get("CODEKB_SECOND_TOKEN"),
        help=(
            "A second Cognito JWT for a *different* principal. The authz phase "
            "uses it to check cross-identity denials on a token-enforced "
            "deployment. In X-User-Identity (local) mode a second identity is "
            "synthesized automatically, so this is not needed there."
        ),
    )
    parser.add_argument(
        "--user-identity",
        default=os.environ.get("CODEKB_USER_IDENTITY", "regression@codekb.test"),
        help="X-User-Identity value for local-dev mode (default: %(default)s)",
    )
    parser.add_argument(
        "--git-url",
        default=os.environ.get("CODEKB_TEST_GIT_URL", DEFAULT_GIT_URL),
        help="Repository to analyze; must be on an allowlisted host (default: %(default)s)",
    )
    parser.add_argument(
        "--search-query",
        default=os.environ.get("CODEKB_TEST_SEARCH_QUERY", DEFAULT_SEARCH_QUERY),
        help=(
            "Natural-language query for the semantic-search checks. The default "
            "is phrased for the default repository; change it alongside --git-url "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--github-pat-token",
        default=os.environ.get("CODEKB_GITHUB_PAT_TOKEN"),
        help=(
            "GitHub personal access token. When set, the 'git-credentials' phase "
            "runs: it stores the token via the git-credentials API and (if "
            "--github-private-git-url is also given) clones a private repo with "
            "it. When unset, that phase is skipped."
        ),
    )
    parser.add_argument(
        "--github-private-git-url",
        default=os.environ.get("CODEKB_GITHUB_PRIVATE_GIT_URL"),
        help=(
            "A private GitHub repo URL (plain clone URL, no embedded credentials) "
            "the --github-pat-token can access; required for the private-repo "
            "clone check in the 'git-credentials' phase"
        ),
    )
    parser.add_argument(
        "--github-username",
        default=os.environ.get("CODEKB_GITHUB_USERNAME", "codekb-regression"),
        help=(
            "Username stored alongside --github-pat-token; GitHub accepts any "
            "non-empty username with a PAT (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--reuse-space",
        help="Run against an existing READY space instead of creating one",
    )
    parser.add_argument("--keep", action="store_true", help="Skip teardown")
    parser.add_argument(
        "--phase",
        help=(
            "Comma-separated phases to run instead of the default set "
            f"(choices: {', '.join(PHASE_NAMES)}). Preflight always runs. Naming "
            "an overview phase here runs it even without --with-overviews."
        ),
    )
    parser.add_argument(
        "--analysis-timeout",
        type=float,
        default=1800.0,
        help="Seconds to wait for analysis or refresh to finish (default: %(default)s)",
    )
    parser.add_argument(
        "--overview-timeout",
        type=float,
        default=1800.0,
        help=(
            "Seconds to wait for Space or Hyperspace Overview generation "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--agent-timeout",
        type=float,
        default=600.0,
        help="Seconds to wait for a single agent response (default: %(default)s)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    return parser.parse_args(argv)


def select_phases(args: argparse.Namespace, console: Console) -> list[Phase]:
    """Resolve which phases to run from --phase / --with-overviews."""
    if args.phase:
        requested = [p.strip() for p in args.phase.split(",") if p.strip()]
        unknown = [p for p in requested if p not in PHASE_NAMES]
        if unknown:
            console.line(
                console.red(
                    f"unknown phase(s): {', '.join(unknown)}. "
                    f"Choose from: {', '.join(PHASE_NAMES)}"
                )
            )
            raise SystemExit(2)
        # Preserve registry order so dependencies still hold.
        return [p for p in PHASES if p.name in requested]
    # Default: every phase except the opt-in (slow overview) ones, unless asked.
    return [p for p in PHASES if not p.optional or args.with_overviews]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console(color=False if args.no_color else None)
    client = Client(
        args.base_url,
        token=args.token,
        user_identity=None if args.token else args.user_identity,
    )
    ctx = Context(client=client, console=console, args=args)
    phases = select_phases(args, console)

    console.line(console.bold("CodeKB backend regression suite"))
    console.note(f"target    : {client.base_url}")
    console.note(f"overviews : {'on (--with-overviews)' if args.with_overviews else 'off'}")
    console.note(f"auth      : {'Bearer token' if args.token else f'X-User-Identity {args.user_identity}'}")
    console.note(f"phases    : {', '.join(p.name for p in phases) or '(none)'}")

    start = time.time()
    try:
        phase_preflight(ctx)
        for phase in phases:
            phase.run(ctx)
        # Always clean up what this run created, even if only some phases ran.
        if ctx.git_space_id or ctx.upload_space_id or ctx.authz_space_id or ctx.hyperspace_id:
            phase_teardown(ctx)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        console.line()
        console.warn("interrupted")
        leftovers = [
            sid
            for sid in (
                ctx.git_space_id if ctx.created_git_space else None,
                ctx.upload_space_id,
                ctx.authz_space_id,
                ctx.hyperspace_id,
            )
            if sid
        ]
        if leftovers:
            console.warn("resources left behind: " + ", ".join(leftovers))
        return 130

    return print_report(ctx, time.time() - start)


if __name__ == "__main__":
    sys.exit(main())
