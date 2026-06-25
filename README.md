# Jaeger MCP Server

MCP server gọn nhẹ (FastAPI) expose dữ liệu tracing của Jaeger thành MCP tools,
khớp với kiến trúc private MCP + Amazon Quick trong dự án này.

## Endpoints
- `GET /health` — health check cho ALB target group (trả 200 + JSON)
- `POST /mcp` — endpoint MCP (JSON-RPC 2.0): `initialize`, `tools/list`, `tools/call`

## Tools
| Tool | Mô tả |
|------|-------|
| `get_services` | Liệt kê service có trace trong Jaeger |
| `get_operations` | Liệt kê operation của 1 service |
| `find_traces` | Tìm trace gần đây theo service (+ operation tuỳ chọn) |
| `get_trace` | Lấy 1 trace theo trace ID |

inputSchema dùng JSON Schema **Draft 7** (`required` là mảng ở cấp root) — đúng
yêu cầu của Amazon Quick lúc publish connector.

## Chạy local (test)
```bash
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
JAEGER_URL=http://localhost:16686 ./venv/bin/python main.py
# health
curl http://localhost:8000/health
# list tools
curl -s http://localhost:8000/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Biến môi trường
- `JAEGER_URL` — mặc định `http://localhost:16686`
- `PORT` — mặc định `8000`

## Deploy
Repo này được clone bởi user-data của EC2 (biến Terraform `mcp_repo_url`).
user-data tạo venv, cài requirements, và chạy `main.py` như systemd service
`jaeger-mcp` trên `:8000`.
