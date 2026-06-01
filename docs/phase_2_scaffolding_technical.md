# Phase 2 — Scaffolding and repo architecture (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, and
> future maintainers of this service.
> **Stakeholder counterpart:** [`phase_2_scaffolding_stakeholder.md`](phase_2_scaffolding_stakeholder.md).
> **Previous phase:** [`phase_1_planning_technical.md`](phase_1_planning_technical.md).
>
> Phase 1 closed with decisions on paper. Before writing any meaningful
> logic, this phase commits those decisions to **structure** — folder
> layout, dependency pins, configuration surface, and the seams
> between modules. Nothing in this phase is business logic; it's the
> scaffolding that determines what's easy and hard to change later.

---

## 2.1 What changed since phase 1

Phase 1 produced architectural intent (which stack, which boundaries,
which tradeoffs). Phase 2 turns intent into **a repo a stranger could
read**:

- A folder layout that maps 1:1 to the layers in phase 1's diagram.
- A pinned dependency set we can defend at ARB and that ops can
  audit at hand-off.
- A configuration surface that's 12-factor by default.
- A storage seam (`Protocol` + in-memory fakes) that lets the
  integration tests run without standing up Mongo / ES / Redis.

By the end of phase 2 the repo has zero behavior and several hundred
lines of structural code. That's intentional — boundaries are cheaper
to move now than after they have logic riding on them.

## 2.2 Folder layout

```
feathr-events/
├── app/
│   ├── api/            # FastAPI routers + DI providers
│   ├── cache/          # Redis-backed realtime stats cache
│   ├── middleware/     # Token-bucket rate limiter
│   ├── models/         # Pydantic input + persisted event schemas
│   ├── queue/          # In-process, SQS-shaped queue
│   ├── storage/        # Mongo + Elasticsearch repos + in-memory fakes
│   ├── workers/        # Background batching worker
│   ├── config.py       # Settings (env-driven)
│   ├── lifecycle.py    # App startup/shutdown wiring
│   ├── logging_config.py
│   └── main.py
├── tests/
│   ├── unit/
│   └── integration/
├── docs/               # This journal
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── docker-compose.yml
└── Dockerfile
```

### Why this cut and not another

The phase 1 diagram has five domains: HTTP, async transport, persistence,
search, cache. Each of those gets its own package under `app/`. Three
operational concerns — config, lifecycle, logging — live as single
modules at the top of `app/` because they aren't shaped like "domains"
and don't grow files individually.

Modules I deliberately did **not** create:

- **No `app/services/` "business logic" layer.** The "service" in this
  system is the worker, which already has its own package. A shared
  services layer would just be a place to dump code that doesn't fit
  anywhere else.
- **No `app/utils/`.** Utility modules collect cruft. If a helper is
  needed in two places, it goes in the layer that owns the concept,
  not in a generic dumping ground.
- **No `app/db/` umbrella for Mongo + ES.** Mongo (source of truth) and
  ES (secondary index) have different operational properties; coupling
  them under one package would hide that. They live as siblings inside
  `storage/`.

## 2.3 Dependencies

Final `requirements.txt`:

```
fastapi==0.115.5
uvicorn[standard]==0.32.1
pydantic==2.9.2
pydantic-settings==2.6.1
motor==3.6.0
elasticsearch[async]==8.15.1
redis==5.2.0
python-json-logger==2.0.7
```

| Package | Why this one | What I considered and rejected |
| ------- | ------------ | ------------------------------ |
| `fastapi` | First-class async, Pydantic-native validation, automatic OpenAPI, clean DI | Flask + flask-pydantic — works, but async support is bolted on |
| `uvicorn[standard]` | ASGI server with `httptools` + `uvloop` extras | Hypercorn — fine, but uvicorn is the conventional default for FastAPI |
| `pydantic` v2 | Faster than v1 (Rust core), strict typing, better discriminated unions | Stay on v1 — no reason to take legacy debt on a new project |
| `pydantic-settings` | The supported 12-factor config story for pydantic v2 | Hand-rolled `os.environ` parsing — re-invents the wheel and skips validation |
| `motor` | Async MongoDB driver maintained by MongoDB itself | `umongo` / `beanie` ODMs — add an abstraction we don't need; this isn't a document modeling exercise |
| `elasticsearch[async]` | Official async client | `elasticsearch-py` sync + run_in_executor — works but bolts sync semantics onto async code; head-of-line stalls under load |
| `redis` v5 | Official sync + async client in one package | `aioredis` — merged into `redis-py`; use the merged package |
| `python-json-logger` | Structured logs with no opinions about output destination | `structlog` — more powerful but more setup; over-investing for this scope |

Dev dependencies (`requirements-dev.txt`):

```
-r requirements.txt
pytest==8.3.3
pytest-asyncio==0.24.0
httpx==0.27.2
fakeredis==2.26.1
ruff==0.7.4
```

| Package | Why |
| ------- | --- |
| `pytest` + `pytest-asyncio` | The standard async-Python testing stack; `asyncio_mode = "auto"` removes per-test decorators |
| `httpx` | Async-native HTTP client; the FastAPI-canonical way to drive integration tests via `ASGITransport` |
| `fakeredis` | In-memory Redis with the same interface as `redis-py`; lets cache tests run without a container |
| `ruff` | Single tool that replaces flake8 + isort + (optional) black; fast enough to run on save |

## 2.4 Configuration surface

[`app/config.py`](../app/config.py) defines a single `Settings` class
keyed off environment variables. The full surface lives in
[`.env.example`](../.env.example).

### Why 12-factor from day one

A prototype is tempting to hard-code (one machine, one shape). But
hard-coded settings:

- Hide the surface from operators (they don't see what's tunable).
- Force a rebuild for every operational change.
- Don't carry over to docker-compose / Kubernetes without rework.

`pydantic-settings` makes the cost of "doing it right" negligible:
the schema is validated at startup, types are enforced, defaults are
visible in code.

### What's in the config surface

Groups in [`app/config.py`](../app/config.py):

- **Service URIs** — `MONGO_URL`, `ELASTICSEARCH_URL`, `REDIS_URL`. Each
  store gets its own URI so a deploy can split them across providers.
- **Worker tuning** — `WORKER_CONCURRENCY`, `WORKER_BATCH_SIZE`,
  `WORKER_BATCH_TIMEOUT_SECONDS`. Decoupling these from code means an
  operator can tune throughput/latency without a PR.
- **Retry policy** — `QUEUE_MAX_RECEIVE_COUNT`,
  `QUEUE_BASE_RETRY_DELAY_SECONDS`. Same reasoning.
- **Cache** — `REALTIME_CACHE_TTL_SECONDS`, `REALTIME_WINDOW_SECONDS`.
- **Abuse prevention** — `RATE_LIMIT_PER_MINUTE`.
- **Mongo client timeouts** — `MONGO_SERVER_SELECTION_TIMEOUT_MS`,
  `MONGO_SOCKET_TIMEOUT_MS`. These two were added in phase 6 — the
  phase 2 version of this file didn't have them yet.

Every field has a sensible default in code, every field is overridable
via env var, every override is type-validated at startup. If you
mis-type `WORKER_CONCURRENCY=banana` the app refuses to start with a
clear error.

## 2.5 The storage seam — `Protocol` + in-memory fakes

This is the single most consequential decision of phase 2.

### The decision

[`app/storage/protocols.py`](../app/storage/protocols.py) defines two
`Protocol`s:

```python
class EventStore(Protocol):
    async def insert_many(self, events: list[StoredEvent]) -> int: ...
    async def find(self, q: EventQuery) -> list[dict[str, Any]]: ...
    async def stats(self, *, start, end, bucket) -> list[dict[str, Any]]: ...
    async def realtime_counts(self, *, since, until) -> dict[str, int]: ...
    async def ensure_ready(self) -> None: ...
    async def close(self) -> None: ...

class EventSearchStore(Protocol):
    async def index_many(self, events: list[StoredEvent]) -> int: ...
    async def search(self, q: EventSearchQuery) -> list[dict[str, Any]]: ...
    async def ensure_ready(self) -> None: ...
    async def close(self) -> None: ...
```

Two concrete implementations follow that surface:

- [`app/storage/mongo.py`](../app/storage/mongo.py) — `MongoEventStore`
  with motor + pymongo.
- [`app/storage/elasticsearch_repo.py`](../app/storage/elasticsearch_repo.py)
  — `ESEventSearch` with the async ES client.

And one set of fakes for tests:

- [`app/storage/fakes.py`](../app/storage/fakes.py) — `InMemoryEventStore`
  and `InMemoryEventSearch`, both implementing the protocols with
  plain-Python data structures.

### Why do this now

Three reasons:

1. **Integration tests stay fast and hermetic.** Tests can drive the
   full HTTP → queue → worker → store → query path without standing up
   Docker. The CI box doesn't need 4 containers; an OSS contributor
   without Docker installed can still hack.
2. **The seam matches an axis the architecture cares about.** Phase 1
   committed to "Mongo is source of truth, ES is secondary." The seam
   makes that boundary explicit in code — both stores look the same to
   the worker, but their failure handling diverges intentionally.
3. **Future swap is a transport change, not a redesign.** If we ever
   move Mongo to DynamoDB, or ES to OpenSearch, the worker doesn't
   change. This is also why phase 1 mentioned SQS as a swap candidate
   for the queue — same principle, different layer.

### What the protocols don't cover

The protocols pin **the call surface** the worker and API need. They
don't pin operational behavior — `ensure_ready` is the only place
where a concrete impl can do schema bootstrapping (`create_index`,
ES mapping create). That's deliberate: the operational surface is
implementation detail and shouldn't leak into tests.

## 2.6 What I deliberately deferred to phase 3

Things that the phase 2 scaffold doesn't have, and the rationale for
not building them yet:

- **No queue implementation.** The queue's design is non-trivial
  (priority queue keyed on `visible_at`, ack/nack/DLQ semantics).
  That belongs to phase 3 alongside the worker that uses it.
- **No worker.** Same as the queue — the two co-evolve.
- **No business logic in models.** `EventIn` and `StoredEvent` get
  defined here at the structural level, but the validators
  (event_type lowercasing, metadata cap) are added in phase 3 when
  there are tests to anchor them.
- **No tests.** Phase 4 is dedicated to test strategy. Phase 2 builds
  the foundation that makes phase 4 cheap; it doesn't pre-empt it.

## 2.7 State of the repo at end of phase 2

A tree a new teammate could clone and read end-to-end in 10 minutes. No
endpoints work yet, but:

- `pip install -r requirements-dev.txt` succeeds.
- `python -c "from app.config import get_settings; print(get_settings())"`
  prints valid settings from defaults.
- The `Protocol` types in `app/storage/protocols.py` type-check against
  the in-memory fakes.

That's the smallest viable "ready to start coding logic" state. Phase
3 starts here.

## 2.8 Cross-references

- Final layout, in production: see [`README.md` → Project layout](../README.md#project-layout).
- Why these module boundaries: see [`ARCHITECTURE.md` §2](../ARCHITECTURE.md).
- Settings surface, in operation: see [`.env.example`](../.env.example).

---

_Phase 3 in progress..._
