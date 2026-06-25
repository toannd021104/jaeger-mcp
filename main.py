"""
Jaeger MCP Server
=================

A small, self-contained MCP server (FastAPI) that exposes Jaeger tracing data
as MCP tools. Designed to run on the private MCP EC2 instance alongside the
Jaeger all-in-one container (which listens on :16686).

Endpoints
---------
- GET  /health  -> health check used by the ALB target group
- POST /mcp      -> Streamable-HTTP MCP endpoint (JSON-RPC 2.0)

The /mcp endpoint implements the minimal MCP method set Amazon Quick needs:
- initialize
- tools/list
- tools/call

Tool input schemas use JSON Schema Draft 7 (required is a root-level array),
which is what Amazon Quick requires at connector publish time.
"""

import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

JAEGER_URL = os.environ.get("JAEGER_URL", "http://localhost:16686")
MCP_PORT = int(os.environ.get("PORT", "8000"))
SERVER_NAME = "jaeger-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-03-26"

app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)


# --------------------------------------------------------------------------- #
# Tool definitions (JSON Schema Draft 7)
# --------------------------------------------------------------------------- #
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_services",
        "description": "List all services that have reported traces to Jaeger.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_operations",
        "description": "List operations (span names) for a given service.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name to list operations for.",
                }
            },
            "required": ["service"],
        },
    },
    {
        "name": "find_traces",
        "description": "Find recent traces for a service, optionally filtered by operation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name to search traces for.",
                },
                "operation": {
                    "type": "string",
                    "description": "Optional operation name to filter by.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of traces to return (default 20).",
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "get_trace",
        "description": "Fetch a single trace by its trace ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {
                    "type": "string",
                    "description": "The Jaeger trace ID (hex string).",
                }
            },
            "required": ["trace_id"],
        },
    },
]


# --------------------------------------------------------------------------- #
# Jaeger client helpers
# --------------------------------------------------------------------------- #
async def _jaeger_get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{JAEGER_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


async def tool_get_services() -> dict[str, Any]:
    data = await _jaeger_get("/api/services")
    return {"services": data.get("data") or []}


async def tool_get_operations(service: str) -> dict[str, Any]:
    data = await _jaeger_get(f"/api/operations", {"service": service})
    return {"service": service, "operations": data.get("data") or []}


async def tool_find_traces(
    service: str, operation: str | None = None, limit: int = 20
) -> dict[str, Any]:
    params: dict[str, Any] = {"service": service, "limit": limit}
    if operation:
        params["operation"] = operation
    data = await _jaeger_get("/api/traces", params)
    traces = data.get("data") or []
    summary = [
        {
            "traceID": t.get("traceID"),
            "spans": len(t.get("spans") or []),
        }
        for t in traces
    ]
    return {"service": service, "count": len(summary), "traces": summary}


async def tool_get_trace(trace_id: str) -> dict[str, Any]:
    data = await _jaeger_get(f"/api/traces/{trace_id}")
    return {"trace_id": trace_id, "data": data.get("data") or []}


TOOL_DISPATCH = {
    "get_services": lambda args: tool_get_services(),
    "get_operations": lambda args: tool_get_operations(args["service"]),
    "find_traces": lambda args: tool_find_traces(
        args["service"], args.get("operation"), args.get("limit", 20)
    ),
    "get_trace": lambda args: tool_get_trace(args["trace_id"]),
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
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


@app.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
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
                {
                    "content": [{"type": "text", "text": f"Jaeger error: {exc}"}],
                    "isError": True,
                },
            )
        import json

        return _rpc_result(
            req_id,
            {"content": [{"type": "text", "text": json.dumps(output, default=str)}]},
        )

    return _rpc_error(req_id, -32601, f"Unknown method: {method}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
