# Panda Web Interface

Canonical browser workspace for Panda Multi-Agent. The Web Interface is a static client served by FastAPI; all business operations go through the closed **Business Assistant API / Chat** at `/api/v1/business-assistant`.

## Architecture

```
Browser
  ↓  (static HTML/CSS/JS)
Panda Web Interface  —  /static/panda/*
  ↓  (X-API-Key, JSON/multipart)
Business Assistant API / Chat  —  /api/v1/business-assistant/*
  ↓
Business Assistant → Durable Workflow → ToolGateway → Integration Activation
```

The frontend never calls providers, marketplace APIs, databases, or workflow internals directly.

## Technology choice

The repository already used **vanilla HTML/CSS/JS** for legacy chat (`static/chat/`), product account (`static/product/`), and admin surfaces. The Web Interface reuses that pattern under `static/panda/`:

- No npm/React build step
- FastAPI serves static assets at `/static` and the app shell at `/`
- Typed contracts are enforced by the backend; the client uses a single `PandaApi` module

Legacy UI Chat remains at `/legacy-chat`.

## Application shell

| Area | Purpose |
|------|---------|
| Left sidebar | Panda branding, New Chat, conversation history, account/logout |
| Main header | Conversation title, request status pill |
| Timeline | User/assistant messages |
| Progress panel | Backend execution events |
| Plan panel | Collapsible plan summary |
| Approval card | Preview + Approve / Reject / Cancel |
| Result panel | Business result + artifacts |
| Composer | Multiline input, attachments, send |

Responsive: collapsible sidebar on narrow viewports (`sidebar.open` toggle).

## Authentication

- Sign-in gate collects workspace **API key** (the secret portion of `PANDA_API_KEYS` entries).
- Key stored in **`sessionStorage`** only (`panda_api_key`); cleared on logout.
- No API keys in HTML source, logs, or bundled credentials.
- Unauthenticated users cannot call protected BA API routes.

## Conversations

Minimal transport endpoints (tested):

- `GET /conversations` — list for authenticated tenant/owner
- `POST /conversations` — create
- `GET /conversations/{id}/messages` — restore timeline + `request_id` links

New Chat creates a conversation without deleting history. Switching conversations stops pollers and loads messages from the backend.

## Requests, progress, approval

- Submit: `POST /requests` with `conversation_id`, `idempotency_key`, optional `artifact_refs`
- Status: `GET /requests/{id}/status`
- Events: `GET /requests/{id}/events?after=…` (bounded polling, stops on terminal states)
- Preview: `GET /requests/{id}/preview`
- Approve / Reject / Cancel: canonical POST endpoints (HITL binding on backend)

UI maps internal statuses to readable labels. Progress is rendered only from backend events — never fabricated.

## Attachments

- `POST /attachments` (multipart) stores tenant-scoped uploads
- Allowed: xlsx, xls, csv, pdf, docx, png, jpg, jpeg, webp, txt (max 10 MB)
- Returns `artifact_ref` (`artifact://upload/{uuid}/{filename}`) sent with the request

## Polling and reconnect

- Poll interval ~1.5s while active; ~4s when waiting for approval; stops on terminal states
- Page refresh: messages + latest `request_id` restore pending approval / running / completed state
- `sessionStorage` tracks active request per conversation as a fast path

## Security

- All untrusted content rendered via `textContent` / `renderMultiline` (no raw HTML injection)
- `escapeHtml` available for attribute contexts
- Tenant identity from authenticated backend context only — no client `tenant_id` override
- Artifact URLs are backend references, not arbitrary filesystem paths

## Testing

Focused closure suite: `tests/test_web_interface_closure.py`

Covers static assets, auth boundary, conversation/upload transport, idempotency, WEB-E2E flows (analysis, Excel, Ozon read, Bitrix approve, duplicate approval, reject, refresh recovery, XSS contract, conversation isolation), and OpenAPI presence.

Run:

```bash
python -m unittest tests.test_web_interface_closure -v
python -m unittest tests.test_business_assistant_api_closure -v
python -m unittest discover -s tests -p "test_*.py" -q
```

## Deferred

- Telegram / Voice interfaces
- Native mobile apps
- Real vendor credential activation from UI
- Full admin dashboard
- WebSocket/SSE streaming (polling used instead)
- Analytics dashboard
- Production business E2E against live vendors
