# vehicle-reservation-stage3-mcp-server

## Stage 4 LangGraph architecture

The application uses a `ReservationWorkflow` in `app/workflow.py` to orchestrate
the complete pipeline:

1. **User interaction node** runs the RAG chatbot, guardrails, reservation form,
   and dynamic SQLite availability lookup.
2. **Administrator wait node** receives a pending reservation from the chatbot
   and exposes it through the administrator API.
3. **Administrator approval node** records the approve/refuse decision.
4. **MCP recording node** writes approved reservations to the append-only file
   store exactly once.

The chatbot endpoint and administrator endpoints invoke this graph. The graph
keeps chat state in the existing Flask session and reservation state in SQLite.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m flask --app app.main:create_app run
```

Optional environment variables:

- `ADMIN_API_KEY`: protects administrator routes with `X-Admin-Key`.
- `MCP_API_KEY`: protects the Flask and FastAPI MCP write tool with Bearer auth.
- `RESERVATION_FILE`: changes confirmed reservation storage.
- `DATABASE`: Flask configuration database path can also be supplied by tests.

## End-to-end usage

1. Start the Flask application on port 5000.
2. Use `/api/chat` or the browser UI to enter a reservation.
3. Review `POST /api/admin/requests` or the administrator panel.
4. Approve or refuse the request.
5. Approved records are written by the MCP recording node.

## MCP server

```powershell
$env:MCP_API_KEY="development-key"
uvicorn mcp_server:app --host 127.0.0.1 --port 8000
```

The MCP tool remains available at
`POST /mcp/tools/write_confirmed_reservation`.

## Confirmed reservation storage

Confirmed administrator approvals are appended to
`data/confirmed_reservations.txt` in this format:

`Name | Car Number | Reservation Period | Approval Time`

The administrator approval routes write the record through the integrated
reservation store. Each reservation reference is written only once.

For an HTTP MCP server, run:

```powershell
uvicorn mcp_server:app --host 127.0.0.1 --port 8000
```

The MCP tool is available at
`POST /mcp/tools/write_confirmed_reservation`. Set `MCP_API_KEY` and send:

```text
Authorization: Bearer <MCP_API_KEY>
```

For Flask administrator routes, set `ADMIN_API_KEY` and send
`X-Admin-Key: <ADMIN_API_KEY>`. Requests are rejected with HTTP 401 without a
matching key.