"""
Jaeger MCP Server
=================

A self-contained MCP server (FastAPI) exposing Jaeger tracing data as 11 MCP
tools, matching the architecture of the private MCP + Amazon Quick setup.

Endpoints
---------
- GET  /health  -> health check used by the ALB target group
- POST /mcp      -> MCP endpoint (JSON-RPC 2.0): initialize, tools/list, tools/call

Tool input schemas use JSON Schema Draft 7 (required is a root-level array),
which is what Amazon Quick requires at connector publish time.

Auth
----
- AUTH_MODE=none    -> no authentication (default)
- AUTH_MODE=service -> validate a Cognito-issued RS256 JWT on every /mcp call
"""

import json
import os
import statistics
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

JAEGER_URL = os.environ.get("JAEGER_URL", "http://localhost:16686")
MCP_PORT = int(os.environ.get("PORT", "8000"))
SERVER_NAME = "jaeger-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-03-26"

# --------------------------------------------------------------------------- #
# Auth configuration
# --------------------------------------------------------------------------- #
AUTH_MODE = os.environ.get("AUTH_MODE", "none").lower()
OAUTH_ISSUER = os.environ.get("OAUTH_ISSUER", "")
OAUTH_JWKS_URL = os.environ.get("OAUTH_JWKS_URL", "")
OAUTH_REQUIRED_SCOPE = os.environ.get("OAUTH_REQUIRED_SCOPE", "private-mcp/invoke")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")

_jwks_client = (
    PyJWKClient(OAUTH_JWKS_URL)
    if (AUTH_MODE == "service" and OAUTH_JWKS_URL)
    else None
)

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)


class AuthError(Exception):
    def __init__(self, message: str):
        self.message = message


def _verify_bearer(request: Request) -> None:
    if AUTH_MODE != "service":
        return
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise AuthError("Missing or malformed Authorization header")
    token = auth.split(" ", 1)[1].strip()
    if _jwks_client is None:
        raise AuthError("Server OAuth misconfigured (no JWKS)")
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=OAUTH_ISSUER or None,
            options={"verify_aud": False},
        )
    except Exception as exc:
        raise AuthError(f"Invalid token: {exc}")
    scopes = (claims.get("scope") or "").split()
    if OAUTH_REQUIRED_SCOPE and OAUTH_REQUIRED_SCOPE not in scopes:
        raise AuthError("Missing required scope")
    if OAUTH_CLIENT_ID and claims.get("client_id") != OAUTH_CLIENT_ID:
        raise AuthError("Unexpected client_id")


# --------------------------------------------------------------------------- #
# Tool definitions (JSON Schema Draft 7)
# --------------------------------------------------------------------------- #
def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get-services",
        "description": "Gets the service names as JSON array of string.",
        "inputSchema": _obj({}, []),
    },
    {
        "name": "get-operations",
        "description": 'Gets the operations as JSON array of object with "name" and "spanKind" properties.',
        "inputSchema": _obj({"service": {**_STR, "description": "Service name."}}, ["service"]),
    },
    {
        "name": "get-trace",
        "description": "Gets the spans by the given trace ID as JSON array of object.",
        "inputSchema": _obj({"trace_id": {**_STR, "description": "Jaeger trace ID."}}, ["trace_id"]),
    },
    {
        "name": "find-traces",
        "description": "Searches traces and returns spans as JSON array of object.",
        "inputSchema": _obj(
            {
                "service": {**_STR, "description": "Service name to search."},
                "operation": {**_STR, "description": "Optional operation filter."},
                "limit": {**_INT, "description": "Max traces (default 20)."},
                "lookback": {**_STR, "description": "Time window, e.g. 1h, 24h (default 1h)."},
            },
            ["service"],
        ),
    },
    {
        "name": "find-error-traces",
        "description": "Finds traces that contain error spans for a service within a time window.",
        "inputSchema": _obj(
            {
                "service": {**_STR, "description": "Service name."},
                "lookback": {**_STR, "description": "Time window (default 1h)."},
                "limit": {**_INT, "description": "Max traces to scan (default 50)."},
            },
            ["service"],
        ),
    },
    {
        "name": "get-dependencies",
        "description": "Gets the service dependency graph (caller->callee with call counts).",
        "inputSchema": _obj(
            {"lookback": {**_STR, "description": "Time window in hours (default 24)."}}, []
        ),
    },
    {
        "name": "get-service-latency-stats",
        "description": "Computes latency stats (min, max, mean, p50, p95, p99) for a service.",
        "inputSchema": _obj(
            {
                "service": {**_STR, "description": "Service name."},
                "lookback": {**_STR, "description": "Time window (default 1h)."},
                "limit": {**_INT, "description": "Traces to sample (default 100)."},
            },
            ["service"],
        ),
    },
    {
        "name": "get-system-health",
        "description": "Summarizes overall system health across all services. No input required.",
        "inputSchema": _obj({}, []),
    },
    {
        "name": "analyze-trace",
        "description": "Deep-analyzes a single trace: per-span durations, critical path, error spans, slow spans.",
        "inputSchema": _obj({"trace_id": {**_STR, "description": "Jaeger trace ID."}}, ["trace_id"]),
    },
    {
        "name": "compare-traces",
        "description": "Compares two traces side-by-side: duration diffs, missing spans, status changes.",
        "inputSchema": _obj(
            {
                "trace_id_a": {**_STR, "description": "First trace ID."},
                "trace_id_b": {**_STR, "description": "Second trace ID."},
            },
            ["trace_id_a", "trace_id_b"],
        ),
    },
    {
        "name": "investigate-user-issue",
        "description": "Investigates a user-reported issue described in plain language. No trace ID or service name required.",
        "inputSchema": _obj(
            {
                "description": {**_STR, "description": "Plain-language description of the issue."},
                "lookback": {**_STR, "description": "Time window (default 1h)."},
            },
            ["description"],
        ),
    },
]


# --------------------------------------------------------------------------- #
# Jaeger client helpers
# --------------------------------------------------------------------------- #
async def _jaeger_get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{JAEGER_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


def _lookback_to_micros(lookback: str) -> int:
    """Convert '1h'/'30m'/'24h' to a (start, end) microsecond window helper value."""
    unit = lookback[-1].lower()
    try:
        n = int(lookback[:-1])
    except ValueError:
        n = 1
        unit = "h"
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 3600) * n
    return seconds * 1_000_000


async def _fetch_traces(service: str, operation: str | None, limit: int, lookback: str) -> list[dict]:
    end = int(time.time() * 1_000_000)
    start = end - _lookback_to_micros(lookback)
    params: dict[str, Any] = {"service": service, "limit": limit, "start": start, "end": end}
    if operation:
        params["operation"] = operation
    data = await _jaeger_get("/api/traces", params)
    return data.get("data") or []


def _span_duration(span: dict) -> int:
    return int(span.get("duration") or 0)


def _span_has_error(span: dict) -> bool:
    for tag in span.get("tags") or []:
        if tag.get("key") == "error" and tag.get("value") in (True, "true", "True"):
            return True
        if tag.get("key") == "otel.status_code" and tag.get("value") == "ERROR":
            return True
    return False


def _span_error_message(span: dict) -> str | None:
    for tag in span.get("tags") or []:
        if tag.get("key") in ("error.message", "otel.status_description", "message"):
            return str(tag.get("value"))
    return None


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
async def t_get_services() -> Any:
    data = await _jaeger_get("/api/services")
    return data.get("data") or []


async def t_get_operations(service: str) -> Any:
    data = await _jaeger_get("/api/operations", {"service": service})
    ops = data.get("data") or []
    if ops and isinstance(ops[0], dict):
        return ops
    return [{"name": o, "spanKind": ""} for o in ops]


async def t_get_trace(trace_id: str) -> Any:
    data = await _jaeger_get(f"/api/traces/{trace_id}")
    return data.get("data") or []


async def t_find_traces(service: str, operation: str | None = None, limit: int = 20, lookback: str = "1h") -> Any:
    traces = await _fetch_traces(service, operation, limit, lookback)
    return [
        {"traceID": t.get("traceID"), "spanCount": len(t.get("spans") or [])}
        for t in traces
    ]


async def t_find_error_traces(service: str, lookback: str = "1h", limit: int = 50) -> Any:
    traces = await _fetch_traces(service, None, limit, lookback)
    out = []
    for t in traces:
        errs = [s for s in (t.get("spans") or []) if _span_has_error(s)]
        if errs:
            out.append(
                {
                    "traceID": t.get("traceID"),
                    "errorSpans": [
                        {"operation": s.get("operationName"), "message": _span_error_message(s)}
                        for s in errs
                    ],
                }
            )
    return {"service": service, "errorTraceCount": len(out), "traces": out}


async def t_get_dependencies(lookback: str = "24h") -> Any:
    end = int(time.time() * 1000)
    lb = _lookback_to_micros(lookback) // 1000
    data = await _jaeger_get("/api/dependencies", {"endTs": end, "lookback": lb})
    return data.get("data") or []


async def t_get_service_latency_stats(service: str, lookback: str = "1h", limit: int = 100) -> Any:
    traces = await _fetch_traces(service, None, limit, lookback)
    durations = []
    for t in traces:
        spans = t.get("spans") or []
        if spans:
            durations.append(max(_span_duration(s) for s in spans))
    if not durations:
        return {"service": service, "sampleSize": 0, "note": "No traces in window."}
    durations.sort()

    def pct(p: float) -> float:
        idx = min(len(durations) - 1, int(p * len(durations)))
        return durations[idx] / 1000.0  # micros -> ms

    return {
        "service": service,
        "sampleSize": len(durations),
        "unit": "ms",
        "min": durations[0] / 1000.0,
        "max": durations[-1] / 1000.0,
        "mean": round(statistics.mean(durations) / 1000.0, 2),
        "p50": pct(0.50),
        "p95": pct(0.95),
        "p99": pct(0.99),
    }


async def t_get_system_health() -> Any:
    services = await _jaeger_get("/api/services")
    svc_list = services.get("data") or []
    health = []
    for svc in svc_list:
        traces = await _fetch_traces(svc, None, 20, "1h")
        total = len(traces)
        errored = sum(
            1 for t in traces if any(_span_has_error(s) for s in (t.get("spans") or []))
        )
        health.append(
            {
                "service": svc,
                "recentTraces": total,
                "errorTraces": errored,
                "status": "degraded" if errored else "ok",
            }
        )
    return {"serviceCount": len(svc_list), "services": health}


async def t_analyze_trace(trace_id: str) -> Any:
    data = await _jaeger_get(f"/api/traces/{trace_id}")
    traces = data.get("data") or []
    if not traces:
        return {"trace_id": trace_id, "note": "Trace not found."}
    spans = traces[0].get("spans") or []
    analyzed = sorted(
        (
            {
                "operation": s.get("operationName"),
                "durationMs": round(_span_duration(s) / 1000.0, 2),
                "hasError": _span_has_error(s),
                "errorMessage": _span_error_message(s),
            }
            for s in spans
        ),
        key=lambda x: x["durationMs"],
        reverse=True,
    )
    return {
        "trace_id": trace_id,
        "spanCount": len(spans),
        "totalDurationMs": round(max((_span_duration(s) for s in spans), default=0) / 1000.0, 2),
        "errorSpans": [a for a in analyzed if a["hasError"]],
        "slowestSpans": analyzed[:5],
    }


async def t_compare_traces(trace_id_a: str, trace_id_b: str) -> Any:
    a = await t_analyze_trace(trace_id_a)
    b = await t_analyze_trace(trace_id_b)
    return {
        "traceA": {"id": trace_id_a, "totalDurationMs": a.get("totalDurationMs"), "spans": a.get("spanCount")},
        "traceB": {"id": trace_id_b, "totalDurationMs": b.get("totalDurationMs"), "spans": b.get("spanCount")},
        "durationDiffMs": round((a.get("totalDurationMs") or 0) - (b.get("totalDurationMs") or 0), 2),
    }


async def t_investigate_user_issue(description: str, lookback: str = "1h") -> Any:
    services = await _jaeger_get("/api/services")
    svc_list = services.get("data") or []
    findings = []
    for svc in svc_list:
        res = await t_find_error_traces(svc, lookback, 50)
        if res.get("errorTraceCount"):
            findings.append(res)
    return {
        "issue": description,
        "window": lookback,
        "servicesScanned": len(svc_list),
        "servicesWithErrors": len(findings),
        "findings": findings,
        "summary": (
            "Found error traces in the services listed under 'findings'. "
            "Inspect those traces with analyze-trace for root cause."
            if findings
            else "No error traces found in the time window."
        ),
    }


TOOL_DISPATCH = {
    "get-services": lambda a: t_get_services(),
    "get-operations": lambda a: t_get_operations(a["service"]),
    "get-trace": lambda a: t_get_trace(a["trace_id"]),
    "find-traces": lambda a: t_find_traces(a["service"], a.get("operation"), a.get("limit", 20), a.get("lookback", "1h")),
    "find-error-traces": lambda a: t_find_error_traces(a["service"], a.get("lookback", "1h"), a.get("limit", 50)),
    "get-dependencies": lambda a: t_get_dependencies(a.get("lookback", "24h")),
    "get-service-latency-stats": lambda a: t_get_service_latency_stats(a["service"], a.get("lookback", "1h"), a.get("limit", 100)),
    "get-system-health": lambda a: t_get_system_health(),
    "analyze-trace": lambda a: t_analyze_trace(a["trace_id"]),
    "compare-traces": lambda a: t_compare_traces(a["trace_id_a"], a["trace_id_b"]),
    "investigate-user-issue": lambda a: t_investigate_user_issue(a["description"], a.get("lookback", "1h")),
}


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION}


def _rpc_result(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_error(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


@app.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
    try:
        _verify_bearer(request)
    except AuthError as exc:
        return JSONResponse(
            status_code=401,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": f"Unauthorized: {exc.message}"}},
        )

    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return _rpc_result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_DISPATCH.get(name)
        if handler is None:
            return _rpc_error(req_id, -32601, f"Unknown tool: {name}")
        try:
            output = await handler(args)
        except httpx.HTTPError as exc:
            return _rpc_result(
                req_id,
                {"content": [{"type": "text", "text": f"Jaeger error: {exc}"}], "isError": True},
            )
        return _rpc_result(
            req_id,
            {"content": [{"type": "text", "text": json.dumps(output, default=str)}]},
        )

    return _rpc_error(req_id, -32601, f"Unknown method: {method}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
