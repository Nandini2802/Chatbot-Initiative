# Danube AI Chatbot — Code Flow & Architecture

## What This Is

A **standalone web-based chatbot** for Danube Properties brokers and RMs. Brokers open it in their browser, type questions about projects, and get back marketing materials (brochures, floor plans, gallery, videos) streamed as rich cards with AI commentary.

Auth is currently open (`AUTH_ENABLED=false`). Salesforce login will be wired in later without changing anything else.

---

## Top-Level Layout

```
danube-ai/
├── core/          ← shared infrastructure (all 3 phases use this)
├── agent/         ← LangGraph brain (routing, nodes, tools)
├── modules/       ← phase-specific business logic
│   ├── phase1a/   ← ACTIVE — marketing materials
│   ├── phase2/    ← placeholder (inventory / Genie)
│   └── phase3/    ← placeholder (analytics / Freshdesk)
├── api/           ← FastAPI — HTTP endpoints + middleware
├── templates/     ← Jinja2 HTML card templates (UI team owns)
├── evals/         ← LangSmith eval runners + golden test dataset
├── platform/      ← broker feedback → LangSmith annotation loop
├── infra/         ← docker-compose (local) + Azure Bicep (prod)
├── tests/         ← unit / integration / e2e
└── scripts/       ← one-time setup (Qdrant collection creation)
```

---

## Layer 1 — `core/` (Shared Foundation)

Nothing phase-specific ever lives here. Every module in the codebase imports from `core/`.

```
core/
├── config/
│   ├── settings.py   ← Single source of truth for ALL config.
│   │                   Pydantic Settings reads from .env.local.
│   │                   Every URL, model name, TTL, threshold lives here.
│   │                   Never hardcode values anywhere else.
│   │
│   └── prompts.py    ← Prompt registry. In prod: fetches from LangSmith Hub.
│                       In dev: local fallback dict. Always call get_prompt("key"),
│                       never write prompt strings inline in nodes or tools.
│
├── auth/
│   ├── salesforce.py ← Produces UserContext (broker_id, tenant_id, role…).
│   │                   JWT validation is a placeholder — deferred.
│   │                   Currently middleware bypasses this when AUTH_ENABLED=false.
│   └── rbac.py       ← Role definitions: broker / rm / admin.
│                       Permission checks used by ingestion + SQL queries.
│
├── db/
│   ├── postgres.py   ← Async SQLAlchemy engine + session factory + Base class.
│   ├── redis.py      ← aioredis connection pool.
│   └── migrations/   ← Alembic configuration. ALL schema changes go here.
│                       Never run raw ALTER TABLE or CREATE TABLE.
│
├── llm/
│   ├── client.py     ← THE ONLY FILE that imports AzureChatOpenAI/Embeddings.
│   │                   Exports: chat_model, embeddings_model.
│   │                   Everything else imports from here — never from langchain_openai.
│   └── streaming.py  ← Helper to extract text chunks from astream() generators.
│
├── storage/
│   ├── blob.py       ← StorageBackend ABC with two implementations:
│   │                     LocalStorageBackend  → files on disk (dev)
│   │                     AzureBlobBackend     → Azure Blob Storage (prod)
│   │                   Switched by STORAGE_BACKEND=local|azure env var.
│   │                   Business logic only calls storage.upload() / .get_signed_url()
│   │                   — never imports Azure SDK directly.
│   └── signed_urls.py← All download URLs go through here.
│                       Raw blob paths NEVER appear in API responses.
│
├── streaming/
│   └── sse.py        ← All SSE event builders live here.
│                       html_block(), token(), followups(), done(), error()
│                       Never construct raw "data: {...}\n\n" strings elsewhere.
│
├── observability/
│   ├── langsmith.py  ← Called ONCE at app startup before any LangChain imports.
│   │                   Sets LANGCHAIN_TRACING_V2 env vars.
│   │                   LangSmith then auto-traces every LLM call and LangGraph node.
│   ├── tracing.py    ← OpenTelemetry setup → exports to Datadog.
│   │                   `tracer` is used for non-LLM spans (DB, storage, auth).
│   │                   Never add OTel spans around LLM calls — LangSmith handles those.
│   └── metrics.py    ← Custom business metrics: cards served, clarification rate,
│                       request latency. Emitted as OTel metrics to Datadog.
│
└── errors.py         ← Exception hierarchy:
                        DanubeError
                          ├── AuthenticationError
                          ├── AuthorizationError
                          ├── NotFoundError
                          ├── ValidationError
                          ├── StorageError
                          ├── DatabaseError
                          ├── LLMError
                          ├── RateLimitError
                          └── AgentError
```

---

## Layer 2 — `agent/` (LangGraph Brain)

All agent behaviour is expressed as LangGraph nodes and edges. No `while True` loops, no raw LangChain LCEL chains as agent loops.

```
agent/
├── state.py    ← AgentState TypedDict — the single state object that flows
│                 through every node. Never pass extra data via function args.
│
│               AgentState fields:
│                 query, user, session_id, tenant_id
│                 messages, session_history
│                 intent, entities, confidence, path
│                 clarification    ← ClarificationState (tracks missing params)
│                 card_html, card_type, commentary, chips
│                 genie_conv_id, filter_state  ← Phase 2 placeholders (None now)
│
├── graph.py    ← Builds and compiles the master LangGraph graph.
│                 Uses AsyncPostgresSaver to persist AgentState between HTTP
│                 requests (thread_id = session_id = same broker session).
│                 get_graph() is called by the API on first request.
│
├── router.py   ← Two routing functions:
│                 route_after_intent  → looks at intent + confidence + entities
│                 route_after_clarify → checks if clarification is complete
│
└── nodes/                           └── tools/
    ├── intent.py                        ├── retrieve_asset.py
    ├── clarify.py                       ├── check_version.py
    └── followups.py                     ├── retrieve_knowledge.py
                                         ├── clarify_tool.py
                                         └── base.py
```

### Graph Flow — Phase 1A

```
START
  └─► intent node
        │  (LLM classifies intent + extracts entities)
        │
        ├─► missing params? ──► clarify node
        │                           │  emits clarification card via SSE
        │                           └─► END   ← graph stops this turn
        │                               (next broker message resumes from
        │                                checkpointed ClarificationState)
        │
        ├─► get_brochure / get_video / etc. ──► retrieve_asset ──► followups ──► END
        ├─► check_version / list_languages  ──► check_version  ──► followups ──► END
        └─► knowledge_query                 ──► retrieve_knowledge ► followups ──► END
```

### Node Contract — Always the Same Pattern

```python
async def some_node(state: AgentState) -> dict:
    # read from state
    x = state["entities"]
    # return only the keys that changed
    return {"card_html": html, "card_type": "brochure"}
    # LangGraph merges this into state — other keys are untouched
```

Nodes **never** mutate state in place, never yield, never return the full state object.

---

## Layer 3 — `modules/phase1a/` (Business Logic)

```
modules/phase1a/
│
├── models.py         ← SQLAlchemy ORM models.
│                       Tables: projects, assets, asset_versions,
│                               share_links, knowledge_chunks
│                       Every table has: id (UUID), tenant_id (indexed),
│                       created_at, updated_at, deleted_at (soft delete).
│
├── asset_registry.py ← All SQL queries. RBAC baked into every WHERE clause:
│                         .where(Asset.tenant_id == user.tenant_id)
│                         .where(user.role == any_(Asset.permissions))
│                       Never fetch all rows and filter in Python.
│
├── card_builder.py   ← Deterministic card assembly. ZERO LLM calls.
│                       fetch asset → get signed URL → build Pydantic model
│                       → render Jinja2 template → return HTML string.
│                       The LLM only adds commentary AFTER the card is built.
│
├── cards/            ← One Pydantic model per card type.
│   ├── brochure.py         BrochureCard
│   ├── floor_plan.py       FloorPlanCard
│   ├── gallery.py          GalleryCard + GalleryImage
│   ├── video.py            VideoCard
│   ├── version_check.py    VersionCheckCard + VersionEntry
│   ├── language_list.py    LanguageListCard + LanguageEntry
│   └── project_overview.py ProjectOverviewCard
│
├── rag.py            ← Hybrid retrieval for knowledge questions.
│                       1. Dense search via Qdrant (text-embedding-3-large)
│                       2. Sparse fallback via PostgreSQL knowledge_chunks
│                       Results merged and deduplicated.
│
└── ingestion.py      ← Admin pipeline: file upload → DB record → Qdrant index.
                        Strips control characters from PDF text before indexing
                        (prevents prompt injection via document content).
```

---

## Layer 4 — `api/` (HTTP)

```
api/
├── main.py           ← FastAPI app.
│                       Startup order (order matters):
│                         1. configure_langsmith()  ← must be first
│                         2. configure_tracing()
│                         3. App + middleware + routers
│
├── middleware/       ← Applied to every request, outermost first:
│   ├── request_id.py   → attaches X-Request-ID header
│   ├── rate_limit.py   → Redis sliding window, 60 req/min per broker
│   └── auth.py         → AUTH_ENABLED=false: pass through with dev user
│                          AUTH_ENABLED=true:  validate Salesforce JWT
│
└── v1/
    ├── chat.py       ← POST /api/v1/chat/query
    │                   Runs graph.astream_events(), extracts typed events,
    │                   yields SSE frames. Returns StreamingResponse.
    │
    ├── assets.py     ← POST /api/v1/assets/share  (create share link)
    │                   GET  /api/v1/assets/share/{slug} (resolve at access time)
    │
    ├── share.py      ← POST /api/v1/feedback
    │                   Annotates LangSmith trace with broker rating.
    │                   Thumbs-down + corrected output → adds golden pair to eval dataset.
    │
    ├── admin.py      ← POST /api/v1/admin/ingest
    │                   Asset file upload (admin/rm only).
    │
    └── voice.py      ← Placeholder stub (Phase 3).
```

---

## Layer 5 — `templates/cards/`

Seven Jinja2 templates. UI team owns these files. The Python Pydantic model field names are the contract — they must match the template variable names exactly.

```
brochure.html.j2         → {{ project }}, {{ download_url }}, {{ version_label }}
floor_plan.html.j2       → {{ project }}, {{ unit_type }}, {{ download_url }}
gallery.html.j2          → {{ project }}, {{ images }} (list of url+caption)
video.html.j2            → {{ project }}, {{ stream_url }}, {{ thumbnail_url }}
version_check.html.j2    → {{ current_version }}, {{ all_versions }}, {{ is_latest }}
language_list.html.j2    → {{ project }}, {{ languages }} (list of code+name)
project_overview.html.j2 → {{ project }}, {{ available_asset_types }}, {{ available_languages }}
```

---

## A Full Request — End to End

```
Browser
  │  POST /api/v1/chat/query
  │  { "query": "Get me the Olivz brochure", "session_id": "abc123" }
  ▼
RequestIDMiddleware   → X-Request-ID: <uuid>
RateLimitMiddleware   → check Redis counter for this broker
AuthMiddleware        → AUTH_ENABLED=false → attach default UserContext
  │
  ▼
chat.py
  → builds AgentState { query, user, session_id, tenant_id, … }
  → graph.astream_events(state, thread_id="abc123", version="v2")
  │
  ▼  LangGraph (loads prior state from PostgreSQL checkpointer)
  │
intent_node
  → LLM classifies: intent="get_brochure", entities={"project_name":"Olivz"}, confidence=0.97
  → no missing params → returns {intent, entities, confidence}
  │
router → "retrieve_asset"
  │
retrieve_asset_node
  → asset_registry.get_asset("Olivz", "brochure", user)  ← SQL + RBAC
  → storage.get_signed_url(blob_path)                     ← signed, TTL=900s
  → BrochureCard(project="Olivz", download_url=signed_url, …)
  → jinja_env.render("brochure.html.j2", card.model_dump())
  → adispatch_custom_event("html_block", {html, card_type})  ← card to SSE
  → chat_model.astream(commentary_prompt)                    ← LLM tokens
  │
followups_node
  → LLM generates 3 chips
  → adispatch_custom_event("followups", {chips})
  │
END — graph checkpoints final state to PostgreSQL
  │
  ▼ chat.py assembles SSE stream:

data: {"type":"html_block","html":"<div class=card…>","card_type":"brochure"}

data: {"type":"token","text":"Here is the latest"}
data: {"type":"token","text":" Olivz brochure"}
data: {"type":"token","text":" in English."}

data: {"type":"followups","chips":["Get floor plan","Check version","View gallery"]}

data: {"type":"done","run_id":"run-xyz-123"}

  ▼
Browser renders card + streams text + shows chips
Broker clicks thumbs-down → POST /api/v1/feedback {run_id, score:0, comment}
  → LangSmith trace annotated → optionally added to golden eval dataset
```

---

## The Three Rules That Never Break

| Rule | Where it lives |
|---|---|
| **No raw blob paths in responses** — always signed URLs with TTL | `core/storage/signed_urls.py` + `modules/phase1a/card_builder.py` |
| **RBAC in SQL, not Python** — `WHERE role = ANY(permissions)` on every query | `modules/phase1a/asset_registry.py` |
| **tenant_id on everything** — every table, every query, every node | `modules/phase1a/models.py` + `modules/phase1a/asset_registry.py` |

---

## Local Dev — Getting Started

```bash
# 1. Start local services
docker compose -f infra/local/docker-compose.yml up -d

# 2. Apply DB migrations
alembic upgrade head

# 3. Create Qdrant collection (one-time)
python scripts/setup_qdrant.py

# 4. Start the API
uvicorn api.main:app --reload --port 8000

# 5. Run unit tests
pytest tests/unit
```

Copy `.env.example` to `.env.local` and fill in your Azure OpenAI keys before starting.

---

## Adding a New Phase

When Phase 1A UAT is signed off:

1. Add business logic under `modules/phase2/`
2. Add a new node under `agent/nodes/` or `agent/tools/`
3. Wire it into `agent/graph.py` — add node + edge
4. Update `route_after_intent()` in `agent/router.py` to route to it
5. Add new intent key to `core/config/prompts.py`

Nothing in `core/` or `api/` should need to change.
