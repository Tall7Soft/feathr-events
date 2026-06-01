# Distributed Event Processing Platform

A production-shaped backend for ingesting, processing, querying, searching, and
caching high-volume web events. Built for Feathr's Platform Engineering team
ahead of the per-tenant analytics dashboard launch.

**Stack:** Python 3.11 · FastAPI · MongoDB (Motor) · Elasticsearch · Redis · in-process SQS-style queue.

---

## Documentation map

### Primary deliverables — read these first

| Document | What it covers | When to reach for it |
| -------- | -------------- | -------------------- |
| [`README.md`](./README.md) (this file) | How to run the service, the endpoint surface, the configuration knobs, the testing approach, the AI-in-workflow notes. | Always first — the operating manual. |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | System diagram, component responsibilities, storage rationale, indexing strategy, caching strategy, queue + SQS comparison, failure modes (incl. the phantom-write scenario), scaling analysis, SLOs and operational targets, security and trust boundaries, what we would do differently with more time, plus an appendix of findings from manual QA. | Second — the load-bearing artifact for design review (ARB) and any future maintainer. As substantive as the code itself. |
| [`postman_collection.json`](./postman_collection.json) | An importable Postman v2.1 collection: 35 requests across `Health`, `Ingestion`, `Queries`, and `Workflow examples`, with response assertions and a 6-step end-to-end demo. | After the stack is running — interactive verification of every endpoint without writing curl. |

If you only have 20 minutes, read the top of `README.md` (Quick start + Endpoints) and then `ARCHITECTURE.md` cover-to-cover. That is the canonical hand-off.

### Operational and reference artifacts

| File | What it is |
| ---- | ---------- |
| [`docker-compose.yml`](./docker-compose.yml) | The four-service stack: Mongo, Elasticsearch, Redis, app. Includes healthchecks and the dependency ordering at startup. |
| [`Dockerfile`](./Dockerfile) | The app image — Python 3.11 slim base, non-root user, `curl`-based healthcheck. |
| [`.env.example`](./.env.example) | Every environment variable the service reads, with its default. |
| [`requirements.txt`](./requirements.txt) | Production dependencies (pinned). |
| [`requirements-dev.txt`](./requirements-dev.txt) | Adds pytest, httpx, fakeredis, ruff. |
| [`pyproject.toml`](./pyproject.toml) | pytest configuration (asyncio auto-mode, markers, pythonpath). |
| [`.gitignore`](./.gitignore) / [`.dockerignore`](./.dockerignore) | What stays out of source control and the docker build context. |

### Development journal — optional context

The [`docs/`](./docs/) directory is a phased account of how the design
came together. Reading it is **not required** to understand the
service; the primary deliverables above already document the final
state. The journal exists for readers who want to see how the
thinking evolved rather than just the result.

Start at [`docs/README.md`](./docs/README.md) for the index and
suggested reading paths.

Each phase has two parallel documents:

- `*_technical.md` — for engineers, ARB reviewers, and future
  maintainers. Code references, file paths, mock patterns,
  tradeoff math.
- `*_stakeholder.md` — for product owners and other non-technical
  partners. Same decisions translated into business vocabulary
  (the "library / mailroom inbox / lobby scoreboard" analogies
  thread through every phase).

| Phase | What this phase contributed |
| ----- | --------------------------- |
| [phase_1](./docs/phase_1_planning_technical.md) — Planning | Decisions on paper before any code: framework choice, source-of-truth picks, the tradeoffs that would have to be defended in ARB. |
| [phase_2](./docs/phase_2_scaffolding_technical.md) — Scaffolding | Folder layout, dependency pins, 12-factor configuration, the `Protocol` + in-memory-fakes seam that makes integration tests hermetic. |
| [phase_3](./docs/phase_3_implementation_technical.md) — Implementation | Building the queue, worker, storage layers, cache, API, middleware, lifecycle — one section per module. |
| [phase_4](./docs/phase_4_testing_technical.md) — Testing | Test pyramid, the "drain the queue" pattern, philosophy of "test the seams, not the libraries", and what we explicitly chose not to cover. |
| [phase_5](./docs/phase_5_manual_qa_technical.md) — Manual QA | Standing up the real stack and finding **six bugs** the fakes could not catch, including the silent data-loss bug from `NetworkTimeout`. |
| [phase_6](./docs/phase_6_fixes_and_hardening_technical.md) — Fixes & hardening | Closing each finding with both a fix and a regression test (30 → 48), plus the post-release coverage sprint that took the suite to 108 tests / 94%. |
| [phase_7](./docs/phase_7_final_documentation_technical.md) — Documentation pass | Auditing every section of `README.md` and `ARCHITECTURE.md` for clarity and reviewer-friendliness. |
| [phase_8](./docs/phase_8_submission_technical.md) — Release & hand-off | Pre-merge checklist, ARB sign-off, ops readiness review, Postman as a reproducible verification layer, post-release bake-in week. |

### Visual reference

[`docs/images/swagger_docs.png`](./docs/images/swagger_docs.png) is a
capture of the auto-generated Swagger UI at `/docs`. Useful as a
quick visual confirmation of the endpoint surface without standing
up the stack.

---

## Quick start

End-to-end, from a machine with nothing installed to a running stack
in roughly 5 minutes (most of which is downloads).

### 0. Prerequisites

| Tool | Version | How to install | Verify |
| ---- | ------- | -------------- | ------ |
| **git** | any recent | macOS: `brew install git` · Ubuntu/Debian: `sudo apt install git` · Windows: [git-scm.com/download/win](https://git-scm.com/download/win) | `git --version` |
| **Docker Desktop** | 4.x+ (Compose v2 bundled) | macOS / Windows: [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop) · Linux: distro packages for `docker-ce` + `docker-compose-plugin` | `docker --version && docker compose version` |
| **Python** (only for Option B) | 3.11 or newer | macOS: `brew install python@3.11` · Ubuntu: `sudo apt install python3.11 python3.11-venv` · Windows: [python.org/downloads](https://www.python.org/downloads/) | `python --version` |

**Windows users:** Docker Desktop needs **hardware virtualization
enabled in BIOS/UEFI**. If `docker info` fails with a virtualization
error after install, reboot into firmware, enable Intel VT-x / AMD-V,
reboot back. (We hit this during phase 5 — see `docs/phase_5_manual_qa_technical.md` §5.2.)

### 1. Get the code

```bash
git clone <REPO_URL> feathr-events
cd feathr-events
```

### 2. Option A — Docker Compose (recommended)

One command builds the app image, pulls Mongo / Elasticsearch / Redis,
and starts everything with healthchecks + the correct startup
ordering:

```bash
docker compose up -d --build
```

Cold start (no images cached): about **60–90 seconds**, dominated by
the Elasticsearch pull (~700 MB) and its first-boot index warmup.
Subsequent restarts: a few seconds.

Verify all four services are healthy:

```bash
docker compose ps
# Expect: 4 containers, all "Up (healthy)"

curl http://localhost:8000/health
# Expect: {"status":"ok"}
```

If `/health` returns 200 you are ready to drive the API. See
[Drive the API](#drive-the-api-end-to-end-postman-or-swagger) below.

### 3. Drive the API end-to-end (Postman or Swagger)

Two ways to explore the API once the stack is up:

- **Interactive Swagger UI** — open `http://localhost:8000/docs` in any
  browser. FastAPI generates this automatically from the route
  signatures; it covers all seven endpoints with try-it-out forms. A
  reference screenshot lives at [`docs/images/swagger_docs.png`](docs/images/swagger_docs.png).
- **Postman collection** — import [`postman_collection.json`](postman_collection.json)
  into Postman (`File → Import` and drag the file). The collection
  ships with 35 requests across `Health`, `Ingestion`, `Queries`, and
  `Workflow examples` folders, with test scripts on key responses
  (202 ingest, cache miss → hit, validation 422s) and a six-step
  end-to-end workflow that posts one event and reads it back four
  ways. Variables (`baseUrl`, `userId`, `eventType`, window bounds)
  let the same collection target local, staging, or production by
  editing a single field.

### 4. Run the test suite

The unit + integration suite uses in-memory fakes for Mongo and
Elasticsearch, so it does **not** require the docker-compose stack to
be running. You only need Python and the dev dependencies:

```bash
# Linux / macOS
python -m venv .venv
source .venv/bin/activate

# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# All platforms
pip install -r requirements-dev.txt
pytest                              # 108 tests, ~2.5 s
pytest --cov=app --cov-report=term  # with coverage (94%)
```

### 5. Option B — Local Python without Docker Compose

If you prefer to run the FastAPI app on the host while keeping the
data stores in containers:

```bash
# (After the venv + pip install steps above)

# Start just the data stores
docker run -d --name feathr-mongo -p 27017:27017 mongo:7
docker run -d --name feathr-redis -p 6379:6379 redis:7-alpine
docker run -d --name feathr-es -p 9200:9200 \
  -e discovery.type=single-node -e xpack.security.enabled=false \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.1

# Configure connection URLs (defaults work for local Docker as above)
cp .env.example .env

# Run the app with auto-reload
uvicorn app.main:app --reload
```

### 6. Stopping and resetting

```bash
docker compose down              # stop the stack, keep the volumes
docker compose down --volumes    # also wipe Mongo + Elasticsearch data
docker compose restart app       # restart just the app (config / code change)
```

### 7. Troubleshooting

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `port is already allocated` on `8000`, `27017`, `9200`, or `6379` | Another process is using the port | `docker compose down` other Feathr stacks, or change the `ports:` mapping in `docker-compose.yml` |
| `feathr-app` stays in `health: starting` for > 90 s | Elasticsearch did not finish booting yet (first start can take ~1 min on slow disks) | Check `docker logs feathr-es` — wait for `cluster_health` log; the app auto-recovers once ES is healthy |
| `docker info` errors on Windows after install | Hardware virtualization disabled | Enable Intel VT-x / AMD-V in BIOS/UEFI, reboot |
| `pytest` fails on Windows with `ImportError` | venv not activated | `.\.venv\Scripts\Activate.ps1` then re-run |
| `curl: command not found` on Windows | PowerShell's `curl` is `Invoke-WebRequest`, not the real curl | Either install curl (`winget install curl.curl`) or use the Swagger UI / Postman instead |

---

## Endpoints

| Method | Path                       | Purpose                                                                 |
| ------ | -------------------------- | ----------------------------------------------------------------------- |
| POST   | `/events`                  | Validate + enqueue an event (returns `202 Accepted` with `event_id`).   |
| GET    | `/events`                  | List/filter events by type, user, source URL, and date window.          |
| GET    | `/events/stats`            | Mongo aggregation grouped by event type and time bucket (hour/day/week).|
| GET    | `/events/search`           | Full-text search over metadata + URL via Elasticsearch.                 |
| GET    | `/events/stats/realtime`   | Recent-window totals served from Redis with a configurable TTL.         |
| GET    | `/health`                  | Liveness probe.                                                         |
| GET    | `/health/queue`            | Queue depth, in-flight count, DLQ depth, worker counters.               |

### Event payload

```jsonc
{
  "event_type": "click",          // free string; normalized to lowercase
  "user_id": "user-42",
  "source_url": "https://feathr.co/pricing",
  "timestamp": "2026-05-28T12:34:56Z",  // optional, defaults to now()
  "metadata": {                          // flexible JSON; ≤ 64 top-level keys
    "browser": "chrome",
    "campaign": "spring-2026"
  }
}
```

### Sample calls

```bash
# Ingest
curl -X POST http://localhost:8000/events \
  -H 'content-type: application/json' \
  -d '{"event_type":"click","user_id":"u1","source_url":"https://feathr.co","metadata":{"campaign":"spring"}}'

# List
curl 'http://localhost:8000/events?user_id=u1&limit=10'

# Stats (Mongo aggregation)
curl 'http://localhost:8000/events/stats?start=2026-05-01T00:00:00Z&end=2026-06-01T00:00:00Z&bucket=day'

# Search (Elasticsearch)
curl 'http://localhost:8000/events/search?q=spring'

# Realtime stats (Redis-cached)
curl 'http://localhost:8000/events/stats/realtime?window_seconds=300'
```

---

## Project layout

```
app/
├── api/            # FastAPI routers + DI providers
├── cache/          # Redis-backed realtime stats cache
├── middleware/     # Token-bucket rate limiter
├── models/         # Pydantic input + persisted event schemas
├── queue/          # In-process, SQS-shaped queue
├── storage/        # Mongo + Elasticsearch repos + in-memory fakes for tests
├── workers/        # Background batching worker
├── config.py       # Settings (env-driven)
├── lifecycle.py    # App startup/shutdown wiring
├── logging_config.py
└── main.py
tests/
├── unit/           # Queue, worker, validation, cache, aggregation
└── integration/    # Full HTTP→queue→store lifecycle
docs/
├── README.md       # Phased development journal — index + reading paths
├── images/         # Reference screenshots (Swagger UI capture)
└── phase_*.md      # Per-phase technical + stakeholder docs
postman_collection.json   # Importable Postman v2.1 collection (35 requests)
ARCHITECTURE.md     # Design rationale, failure modes, SLOs, security
README.md           # This file
docker-compose.yml  # Mongo + Elasticsearch + Redis + app
Dockerfile          # App image
```

Module boundaries map 1:1 to the layers we want to scale or replace
independently: HTTP, async transport, persistence, search, cache. See
`ARCHITECTURE.md` for why those are the cut lines.

---

## Testing

```bash
pytest                              # full suite — 108 tests, ~2.5 s
pytest tests/unit -q                # fast feedback (~0.5 s)
pytest -m integration -q            # full lifecycle tests
pytest --cov=app --cov-report=term  # coverage report
```

**108 tests, 94% statement coverage.** Integration tests run through the
real FastAPI app, real queue, real worker, and real (`fakeredis`) Redis —
but Mongo and Elasticsearch are substituted with in-memory fakes that
implement the same `EventStore`/`EventSearchStore` protocols. The result:
every test exercises the ingestion → worker → persistence → query control
flow end-to-end without standing up containers.

### Coverage breakdown

23 of 25 source files are at 100% statement coverage. The remaining gap
is concentrated in code that requires real infrastructure to exercise
(documented in the testing philosophy below):

| File | Coverage | Comment |
| ---- | -------: | ------- |
| `app/main.py` | 100% | App factory + global exception handler |
| `app/api/*.py` | 98–100% | All endpoints + helpers |
| `app/cache/realtime.py` | 100% | Single-flight, TTL, JSON encoding |
| `app/middleware/rate_limit.py` | 100% | Token bucket |
| `app/models/event.py` | 100% | Validation + enrichment |
| `app/queue/memory_queue.py` | 100% | SQS-shaped queue semantics |
| `app/storage/mongo.py` | 100% | All query paths via mock-cursor tests |
| `app/storage/elasticsearch_repo.py` | 100% | All bulk + lifecycle paths |
| `app/storage/fakes.py` | 100% | All filter predicates |
| `app/storage/protocols.py` | 100% | Protocol declarations |
| `app/workers/event_worker.py` | 98% | 3 lines: timing edges in `_collect_batch` |
| `app/config.py` | 100% | Settings + lru_cache factory |
| `app/lifecycle.py` | 45% | `lifespan()` requires real Mongo/ES/Redis connections |
| `app/logging_config.py` | 31% | Called only from `lifespan` |

**Testing philosophy.** The point of these tests is to catch behavior
changes in the seams between layers, not to re-test third-party code. We
test:

- Contract correctness of the queue's retry / visibility semantics.
- Worker behavior on success, Mongo failure (retry + DLQ), ES failure
  (ack anyway, Mongo is source of truth), invalid payloads (DLQ
  immediately), and mixed-batch poison-pill resilience.
- Input validation that rejects bad URLs and oversized metadata.
- The cache's single-flight guarantee under concurrency + corrupt-JSON
  degradation.
- The Mongo storage layer's idempotency under `BulkWriteError`, naive-
  datetime tz coercion, half-open windows, and explicit `startOfWeek`
  pinning — all via mocked Motor cursors.
- The Elasticsearch repo's `lenient: true` flag, `text` subfield default,
  index creation lifecycle, and best-effort bulk indexing.
- The aggregation math against deterministic time buckets.
- The app factory + global 500 exception handler.
- Full HTTP lifecycle tests for happy paths and 422 validation.

The gaps that remain — `lifecycle.py:31-89` and `logging_config.py:8-23`
— are the production startup path that opens real Motor / AsyncElasticsearch
/ Redis connections. Covering them with mocks would test the mocks more
than the code; they are validated instead through phase 5 manual QA and
the docker-compose healthchecks.

**What we'd add with more time:** property-based tests for the worker's
retry state machine, contract tests that pin the Mongo aggregation
pipeline against a real Mongo container, a load test that records p50/p99
of `POST /events` under steady load to validate the 202 latency claim,
and chaos tests where Mongo / ES / Redis flap during a sustained load.

---

## Configuration

All runtime configuration is read from environment variables (see
`.env.example`). Highlights:

| Variable                           | Default              | What it controls                              |
| ---------------------------------- | -------------------- | --------------------------------------------- |
| `WORKER_CONCURRENCY`               | `4`                  | Number of parallel batch consumers.           |
| `WORKER_BATCH_SIZE`                | `50`                 | Max events flushed per bulk insert.           |
| `WORKER_BATCH_TIMEOUT_SECONDS`     | `1.0`                | Max wait before flushing a partial batch.     |
| `QUEUE_MAX_RECEIVE_COUNT`          | `3`                  | Retries before a message is dead-lettered.    |
| `QUEUE_BASE_RETRY_DELAY_SECONDS`   | `1.0`                | Base of the exponential retry backoff.        |
| `REALTIME_CACHE_TTL_SECONDS`       | `10`                 | TTL for cached `/events/stats/realtime`.      |
| `REALTIME_WINDOW_SECONDS`          | `300`                | Default lookback when none is requested.      |
| `RATE_LIMIT_PER_MINUTE`            | `600`                | Per-IP request budget (token bucket).         |

---

## AI in My Workflow

> The platform team agreed to document AI use across the team for
> shared learning. Here's the honest version for this project.

**Tools used.** Claude (Sonnet/Opus) inside an agentic IDE was the primary
driver. I used it as a design partner first and a code generator second.

**Where AI sped things up.** Ordered chronologically — early planning
work first, hand-off artifacts last:

*Up-front design (before any code):*

- **Pros-and-cons sweeps for tooling decisions.** Instead of reading
  three blog posts per choice, I'd ask the model for a comparison
  matrix and then validate the load-bearing claims against the
  library's own docs. Concrete examples: `motor` (async) vs `pymongo`
  + `run_in_executor` — picked motor to avoid head-of-line stalls;
  `structlog` vs `python-json-logger` — picked the latter for lower
  setup cost at this scope; `fakeredis` vs `miniredis` vs a real
  Redis testcontainer — fakeredis for fast unit tests, real Redis for
  phase 5 manual QA; `BaseHTTPMiddleware` vs raw ASGI for the rate
  limiter — raw is faster, `BaseHTTPMiddleware` is sufficient at v0.1
  throughput. Each of these is a one-paragraph note in
  `ARCHITECTURE.md` or `docs/`; the model accelerated the decision,
  the docs hold the receipts.
- **Logging strategy in the early phases.** Before writing any worker
  code, I asked the model to enumerate "what would on-call need to see
  at 3 a.m. when this paged?". That conversation produced (a) the
  JSON-structured format over plain text, (b) the per-stage worker
  counters that ended up exposed at `/health/queue` (`processed`,
  `failed`, `dead_lettered`, `duplicates_suppressed`), and (c) the
  discipline of logging `message_id` on every retry / DLQ path so a
  later investigator can grep one ID and reconstruct the full
  message lifecycle. None of those were spelled out in the brief; all
  came out of the up-front "imagine the on-call" exercise.
- **Protocol-shaped repos.** Asking the model to enumerate the operations the
  worker and API needed before writing concrete repos surfaced the
  `EventStore` / `EventSearchStore` Protocol cut, which then let the tests
  swap in in-memory fakes for free.

*Writing the code:*

- **Scaffolding.** Generating the skeleton (`app/__init__.py`, routers,
  Settings class, Dockerfile, docker-compose) from a one-paragraph spec —
  the kind of code that follows from prior decisions and isn't worth typing.
- **Aggregation pipeline.** I described the desired output shape in English
  and used the model to draft the `$dateTrunc`-based group stage, then
  cross-checked it against MongoDB docs before committing.

*Testing strategy:*

- **Test enumeration.** Generating a punch-list of "what's worth testing
  here?" for each layer caught two cases I'd have missed (worker behavior
  when ES is down but Mongo succeeded; queue join timeout semantics).
- **Edge-case enumeration for manual QA.** Before bringing up the docker
  stack in phase 5, I asked the model to enumerate "what failure modes
  would only show up against real Mongo / ES / Redis that the fakes
  cannot exercise?". The answer surfaced the categories I then went
  hunting for: BSON datetime tz handling, ES dynamic-mapping conflicts
  when partner metadata mixes string and numeric fields, server-selection
  vs socket timeouts, week-bucket `startOfWeek` defaults, and the
  at-least-once "phantom write" race. Five of the six bugs phase 5
  caught fall into that list — the model didn't predict the specific
  bugs, it predicted the categories where bugs would hide.

*Debugging and hardening:*

- **Debugging the harder bugs.** Walking through the silent data-loss bug
  from phase 5 (the `except Exception` clause in `MongoEventStore.insert_many`
  that interpreted "no writeErrors" as "all duplicates absorbed" and
  silently swallowed `NetworkTimeout`) was faster as a Socratic
  conversation than as a solo bisect. I described the symptom —
  "counter says `processed += 1`, `db.events.countDocuments` shows zero" —
  and the model walked the hypothesis tree until we landed on the
  `0 == 0` branch within two iterations. Same pattern applied to the
  Elasticsearch `simple_query_string` failure on numeric metadata: the
  model surfaced the `lenient: true` flag faster than I would have
  grepped the ES docs.
- **Pushing test coverage from 82% to 94%.** Once the suite was green
  but coverage was still in the low 80s, I fed the model the
  `pytest --cov-report=term-missing` output and asked it to bucket the
  uncovered lines into "needs real infrastructure to exercise" vs
  "could be mocked cleanly." That split honest gaps
  (`lifecycle.py` lifespan and `logging_config.py`, both intentional)
  from real misses (`storage/mongo.py` at 65%, `storage/elasticsearch_repo.py`
  at 73%). For each fixable bucket the model proposed the test pattern
  — `AsyncMock` for the motor calls, `patch` for `async_bulk`, a
  cursor mock that supports the `.sort().skip().limit()` chain. I
  wrote the actual tests; the model gave me the seam. Total: 23 new
  tests across `test_mongo_store.py`, `test_es_repo.py`,
  `test_misc_coverage.py`, `test_app_factory.py`, `test_fakes.py`.

*Documentation and hand-off artifacts:*

- **Going from Swagger to an importable Postman collection.** FastAPI
  already serves an auto-generated Swagger UI at `/docs`, which is
  fine for interactive exploration but not portable to ops or
  customer engineers. I asked the model to design the right shape for
  a v2.1 Postman collection covering the same surface — folders,
  variables, response assertions, a sequenced workflow demo — and to
  draft the JSON. Cross-checked the assertions against the actual
  responses with curl, captured the `event_id` round-trip via Postman
  test scripts, and dropped a screenshot of the Swagger UI into
  `docs/images/swagger_docs.png` for readers who don't want to stand
  up the stack. Result: 35 importable requests with assertions, sized
  for a five-minute smoke run.
- **README/ARCHITECTURE prose.** Drafting human-readable explanations
  from bullet-point design notes — same way I'd use a junior writer.
- **Docstring sweep across the codebase.** After the suite was green,
  I went through `app/` file by file with the model and we agreed on
  the docstring shape together: one-line summary first, then a
  paragraph explaining the WHY when non-obvious (clamping rationale,
  idempotency invariants, the half-open window contract), with cross
  references to `ARCHITECTURE.md §` where the underlying design
  decision lives. Trivial DI providers and empty `__init__.py` files
  stayed without docstrings — type hints + names already say
  everything; adding docstring noise hurts more than it helps. Net:
  ~65 public methods documented in one pass with a consistent voice.
- **Documentation format and conventions.** Before the first phase
  doc was written, I worked out the shape with the model: title +
  audience blockquote + cross-link to the counterpart track + cross-
  link to the previous phase + numbered subsections + closing pointer
  to the next phase. That template was decided once and applied 17
  times. Same pattern in `ARCHITECTURE.md`: comparison sections use
  side-by-side tables, "what I'd do differently" items are verb-first
  single-line bullets. None of this is automatic — it's the kind of
  cross-document consistency that drifts when no one is enforcing
  it; the model is what made enforcing it cheap.
- **Versioned per-phase documents written separately.** Unusually for
  a project this size, each of the 8 phases got its own document —
  written *when that phase was actually happening*, not retrofitted
  at the end. The model was the partner for every one: I'd narrate
  what had just shifted, the model would draft the doc in the
  established voice, I'd cross-check against reality and push back on
  anything that sounded better than the truth. This is why the
  journal reads as a process rather than a marketing piece — each
  phase's doc landed before I knew how the next phase would shake
  out, so I cannot retroactively make myself look smarter.

*Day-to-day operational ergonomics:*

- **Docker workflow commands.** For convenience during everyday work
  — `docker compose up -d --build`, `docker compose down`, restarting
  individual services, the BIOS-virtualization detour from phase 5,
  the `docker pause feathr-mongo` / `docker unpause` trick used to
  stress-test the retry-and-DLQ path, the right incantation to reset
  Mongo + ES + Redis volumes between QA runs — I'd describe the
  outcome I wanted ("freeze the Mongo process without breaking its
  TCP connection") and let the model produce the exact flag
  combination. Especially valuable for scenarios where I knew the
  goal but not the syntax; reading the docker docs for each one
  would have been slower than asking.

**Where I pushed back on AI output.**
- The first worker draft used a single `asyncio.Queue` and tried to encode
  the visibility timeout via per-message `create_task(sleep)`. That worked
  but blew up the test reasoning — I switched to a priority queue keyed on
  `visible_at` so retries and delayed sends use the same code path.
- An early sketch wanted to invalidate the realtime cache on every successful
  worker batch. I rejected it: the endpoint's contract is _eventual_
  consistency on the order of the TTL, and write-amplifying the cache on
  every ingest defeats the point. The cache is TTL-only; the doc explains why.
- The model suggested indexing every `metadata.*` key in Mongo for "future
  proofing." I dropped that — see ARCHITECTURE.md for the rationale.
- The model offered a synchronous Redis client because async fixtures are
  "annoying." Rejected: mixing sync and async I/O in a FastAPI app is the
  single fastest way to a head-of-line stall.

**How AI shaped the approach.** The most valuable use was running
architectural tradeoffs through the model as a sounding board before
writing code: "if I make ES failures non-retrying, what's the worst case?"
got a clearer articulation of recovery — Mongo is authoritative; ES is
rebuildable — which then became the operating contract for the worker.
The same dialogue happened around cache invalidation, the queue's
visibility model, and where to draw the protocol boundary. At a principal
level, that's where AI earns its keep: not in code volume, but in
shortening the time between "I have a design instinct" and "I can defend
it under scrutiny."

---

## Tech stack — what the brief asked for and where it lives

Cross-reference of every technology named in the project brief against
where it shows up in the deliverable.

### Mandatory stack

| Technology | Brief's role | How it is used here | Where to find it |
| ---------- | ------------ | ------------------- | ---------------- |
| **Python 3.11** | Language | All application code, async/await throughout, type hints + `Protocol`-based seams between layers. | [`pyproject.toml`](./pyproject.toml), [`Dockerfile`](./Dockerfile), [`app/`](./app/) |
| **FastAPI** (chosen over Flask) | Web framework | HTTP layer with Pydantic-native request validation, dependency injection for store/cache/queue, auto-generated OpenAPI / Swagger UI at `/docs`. | [`app/main.py`](./app/main.py), [`app/api/ingestion.py`](./app/api/ingestion.py), [`app/api/queries.py`](./app/api/queries.py), [`app/api/health.py`](./app/api/health.py), [`app/api/deps.py`](./app/api/deps.py) |
| **MongoDB** (via `motor` async driver) | Primary event store + aggregations | Source of truth for events. Four indexes on `(event_type, timestamp desc)`, `(user_id, timestamp desc)`, `(timestamp desc)`, and a unique index on `event_id` for idempotency under at-least-once delivery. `$dateTrunc`-based aggregation pipeline backs `/events/stats`, pinned to `startOfWeek: "monday"`. | [`app/storage/mongo.py`](./app/storage/mongo.py); the [`docker-compose.yml`](./docker-compose.yml) `mongo:7` service. Rationale in [`ARCHITECTURE.md` §4–§5](./ARCHITECTURE.md). |
| **Elasticsearch 8.15** (via the async client + `elasticsearch.helpers.async_bulk`) | Search / analytics layer | Full-text search over `metadata.*` and `source_url.text`. Mapping uses a dynamic template that maps any partner-supplied metadata string as `text` with a `.keyword` subfield. `simple_query_string` queries set `lenient: true` so a numeric `metadata.amount` does not break unrelated text searches. | [`app/storage/elasticsearch_repo.py`](./app/storage/elasticsearch_repo.py); the [`docker-compose.yml`](./docker-compose.yml) `elasticsearch` service. Mapping rationale in [`ARCHITECTURE.md` §5](./ARCHITECTURE.md). |
| **Redis 7** (via `redis-py` async) | Caching layer | Per-window cache of the realtime stats summary, with configurable TTL (`REALTIME_CACHE_TTL_SECONDS`, default 10 s). An asyncio `Lock` per window key single-flights the recompute on cache miss to prevent dogpile. | [`app/cache/realtime.py`](./app/cache/realtime.py); the [`docker-compose.yml`](./docker-compose.yml) `redis` service. Caching strategy in [`ARCHITECTURE.md` §6](./ARCHITECTURE.md). |
| **In-process queue** (modeled after SQS) | Async processing | `asyncio.PriorityQueue` keyed on `(visible_at, seq)` with `send` / `receive` / `ack` / `nack` semantics, per-message delayed redelivery, exponential-backoff retry, and an observable in-flight count via `join()`. A separate `InMemoryQueue("events-dlq")` is the dead-letter queue. SQS comparison table + drop-in migration notes live in `ARCHITECTURE.md`. | [`app/queue/memory_queue.py`](./app/queue/memory_queue.py), consumed by [`app/workers/event_worker.py`](./app/workers/event_worker.py). Queue / SQS comparison in [`ARCHITECTURE.md` §7](./ARCHITECTURE.md). |
| **pytest** (preferred for testing) | Testing | 108 tests (unit + integration) across [`tests/unit/`](./tests/unit/) and [`tests/integration/`](./tests/integration/). `asyncio_mode = "auto"` removes per-test decorators; `fakeredis` substitutes for Redis; in-memory fakes substitute for Mongo and Elasticsearch so the suite runs in ~2.5 s without containers. Coverage at 94% (23 of 25 source files at 100%). | [`tests/`](./tests/), [`pyproject.toml`](./pyproject.toml), [`requirements-dev.txt`](./requirements-dev.txt) |

### Bonus items the brief mentioned (all implemented)

| Item | How it is delivered |
| ---- | ------------------- |
| **Dockerfile + docker-compose** for all four services | [`Dockerfile`](./Dockerfile) (Python 3.11 slim, non-root user, `curl`-based healthcheck) + [`docker-compose.yml`](./docker-compose.yml) (Mongo, Elasticsearch, Redis, app — each with healthchecks and explicit dependency ordering on the app's startup) |
| **Rate limiting / abuse prevention middleware** | Token-bucket per client IP, `RATE_LIMIT_PER_MINUTE` configurable, `/health*` exempt. Returns 429 with `Retry-After`. — [`app/middleware/rate_limit.py`](./app/middleware/rate_limit.py) |
| **Event deduplication in the worker** | Two-layer dedupe: a per-batch `set` for in-flush duplicates, plus a bounded LRU `_BoundedSet` for cross-batch redeliveries. Only marked **after** a confirmed Mongo commit so retries-after-failure are not falsely suppressed. — [`app/workers/event_worker.py`](./app/workers/event_worker.py) |
| **Dead letter queue** | Separate `InMemoryQueue("events-dlq")`. Events that exhaust `QUEUE_MAX_RECEIVE_COUNT` retries land here with a `reason` field; depth observable via `/health/queue`. — [`app/lifecycle.py`](./app/lifecycle.py), [`app/workers/event_worker.py`](./app/workers/event_worker.py), [`app/api/health.py`](./app/api/health.py) |
| **AWS SQS drop-in design notes** | Side-by-side comparison table + five-item migration list covering visibility-timeout, idempotency, batching shifts, DLQ infrastructure, and producer-side backpressure. — [`ARCHITECTURE.md` §7](./ARCHITECTURE.md) |

### Supporting libraries (chosen by us, not mandated)

| Library | Used for | Why this one |
| ------- | -------- | ------------ |
| `pydantic` v2 + `pydantic-settings` | Request validation, 12-factor config | Native FastAPI integration, fast Rust core, strict-by-default validation. |
| `python-json-logger` | Structured JSON logs | Plays well with platform log pipelines; the brief asked for "internal logging" without specifying a format. |
| `fakeredis` | In-memory Redis for tests | Lets cache tests run without a container; same interface as `redis-py`. |
| `httpx` + `ASGITransport` | Driving the FastAPI app in tests | The FastAPI-canonical async test client. |
| `ruff` | Linting | Replaces flake8 + isort + (optionally) black in a single fast tool. |
