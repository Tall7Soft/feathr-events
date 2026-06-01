# Phase 1 — Planning and project understanding (technical)

> **Audience:** Platform Engineering teammates, the Architecture Review
> Board (ARB), and future maintainers of this service.
> **Stakeholder counterpart:** [`phase_1_planning_stakeholder.md`](phase_1_planning_stakeholder.md).
>
> This is what was committed to on paper **before writing any code**.
> It captures the **why** behind each decision — not the what (that
> lives in the code) or the how (that lives in `ARCHITECTURE.md`).

---

## 1.1 Objective

Deliver the new **Distributed Event Processing Platform** for Feathr's
ingest and analytics infrastructure: a backend that takes high-volume
web events from customer sites, persists them durably, makes them
searchable, and exposes both historical and realtime analytics
endpoints.

There are two parallel artifacts on the deliverable, neither one
optional:

1. **The code** — production-quality, idiomatic Python + FastAPI +
   Mongo + ES + Redis. Tested, observable, ready for the platform
   team to take over and run.
2. **`ARCHITECTURE.md`** — the design document the ARB will read
   to sign off on the service, and the reference future maintainers
   will use to extend it. What carries weight at design review is
   not "does it run?" but **"can the tradeoffs survive
   cross-examination?"**.

The platform team's own engineering standards say it directly: we
ship platform work on *how we reason about systems, communicate
architectural decisions, and make principled tradeoffs under
realistic constraints*.

## 1.2 Project context

| Item | Value |
| ---- | ----- |
| Company | Feathr (marketing-tech for non-profits) |
| Team | Platform Engineering |
| Forcing function | Per-tenant analytics dashboard launches next quarter |
| Why now | The current pipeline writes synchronously to a single DB; chokes above ~50 events/sec per tenant; has no search capability. Sales has already quoted prospects with traffic 10× that |
| Timeline | One sprint (one week) for the platform-side milestone — a working service + ARB-ready ARCHITECTURE.md |
| Sign-off | Code review (peers) → ARB approval → ops review (platform/SRE) → merge + release tag |
| Tooling policy | AI is part of the standard toolkit; we document how we use it for calibration and shared learning across the team |
| Scope guard | Strictly backend infrastructure; the dashboard UI is the product team's deliverable on top |
| Hand-off | After release tag, ops takes over production deployment; we stay on point for the first week of bake-in |

## 1.3 Mandatory stack

The stack is pre-decided by platform standards, not by me. That's
not a constraint to push back on — it's alignment with the rest of
our infrastructure (we already run Mongo and Redis in production;
ES is the team's chosen search layer for the next analytics
projects). The real decision space lives *inside* the box, not
around it.

- **Framework:** Python with FastAPI **or** Flask (free choice between the two)
- **Primary store:** MongoDB (events + aggregations)
- **Search / analytics:** Elasticsearch (full-text + analytic queries)
- **Cache:** Redis
- **Async:** in-process queue, SQS-shaped (with a documented swap
  path to real SQS once the platform team finishes provisioning it)

## 1.4 Reading the problem (what we're actually asking ourselves)

The stack is fixed. The real decision space is the set of **design
choices** the platform team's design brief lists as the things we'll
defend at ARB:

- Who is source of truth, and who is a secondary index?
- What delivery guarantees does the queue provide? What changes with
  real SQS?
- How is the cache invalidated? How does it behave under write-heavy
  load?
- What happens when one layer is down? Does the system degrade, or
  cascade-fail?
- If volume 10×s, what breaks first?
- Which indexes do I deliberately **not** add, and why?
- What would I do differently with more time?

The implication for design: tradeoffs must be **explicit in the
document, not implicit in the code**. Any "obvious" decision I don't
justify with a reason is a decision ARB will probe at sign-off, or
a future maintainer will accidentally undo because they couldn't
tell it was load-bearing.

## 1.5 Early decisions (before writing any code)

Choices committed upfront, which structure everything that follows:

| Decision | Choice | Short reason |
| -------- | ------ | ------------ |
| Framework | **FastAPI** | First-class async, natural fit with `motor` / `elasticsearch[async]` / `redis-py` async, clean DI, free Pydantic validation |
| Queue | `asyncio.PriorityQueue` keyed on `(visible_at, seq)` | Supports delivery delays without spawning per-message tasks; same codepath for retry-with-backoff |
| Source of truth | **Mongo** (ES is secondary, rebuildable) | Mongo gives durability + first-class aggregations; ES is great for search but not for "state of the world" |
| Worker on ES failure | Ack the message anyway | Recovering ES is a reindex-from-Mongo (a recovery op), not a per-message retry. Avoids cascading Mongo→Mongo and keeps ingest decoupled from ES health |
| Cache invalidation | **TTL-only** | Eventual consistency is acceptable for "realtime stats"; bust-on-write amplifies writes and kills throughput without real benefit |
| Dedupe | LRU `_BoundedSet` of committed keys + per-flush in-batch set | Suppresses redeliveries but **not** retries-after-failure. Only marked after a confirmed commit |
| Storage boundaries | `Protocol` + in-memory fakes for tests | Lets integration tests run without docker; protects the seam for future swap (e.g. real SQS, S3 archival) |
| Settings | `pydantic-settings` with env vars (12-factor) | No boilerplate, type-validates on startup, plays well with docker-compose and tests |
| Logging | Structured JSON (`python-json-logger`) | The design brief says "internal logging" ambiguously; JSON is the modern observability standard, ships into our existing log pipeline, and adds zero debt |

## 1.6 Proposed solution (high-level)

```
Client
   │
   ▼ POST /events
┌─────────────┐    enqueue     ┌──────────────┐    receive    ┌──────────────┐
│   FastAPI   │ ─────────────► │   InMemory   │ ────────────► │   Worker     │
│  ingestion  │                │  PriorityQ   │               │  pool (N)    │
└──────┬──────┘                └──────┬───────┘               └──┬─────────┬─┘
       │ 202                          │                          │         │
       │                              │ retry/NACK + delay       │ bulk    │ bulk
       │                              │                          ▼         ▼
       │                          ┌───┴──────┐              ┌────────┐ ┌─────┐
       │                          │   DLQ    │              │ Mongo  │ │ ES  │
       │                          └──────────┘              │  SoT   │ │ idx │
       │                                                    └───┬────┘ └──┬──┘
       │ GET /events                                            │         │
       │ GET /events/stats         ─── aggregation ────────────►│         │
       │ GET /events/search        ─────────────────────────────┘─────────┘
       │ GET /events/stats/realtime ── on miss ──► Mongo ──► Redis (TTL)
       ▼
   Client
```

Layers and their single responsibility:

- **API** — validation, rate limiting, enqueue, query orchestration
- **Queue** — buffer between HTTP and persistence, retries with
  backoff, DLQ
- **Worker** — batching, payload validation, fan-out to stores, dedupe
- **Mongo** — source of truth, indexes for query patterns +
  aggregations
- **Elasticsearch** — full-text, dynamic mapping for open-ended
  metadata
- **Redis** — realtime cache with single-flight to prevent dogpile

## 1.7 Tradeoffs I already know I'll have to defend

These need to be **written down** in `ARCHITECTURE.md`, because they're
the ones ARB will probe at sign-off:

1. **Why Mongo and not ES as source of truth.** ES can be durable, but
   its model is optimized for search, not for "system of record". If
   ES loses an event, I can regenerate it from Mongo. The other
   direction doesn't work.
2. **Why TTL-only and not cache busting.** Bust-on-write means every
   `POST /events` triggers an extra `DEL` against Redis. Under load
   that amplifies writes without gaining correctness (the endpoint
   says "realtime", not "strictly consistent").
3. **Why not index `metadata.*`.** Metadata is open-ended. A multikey
   index per partner field fragments the catalog. ES is the right
   tool for ad-hoc metadata queries.
4. **Why the queue is `asyncio.PriorityQueue` and not `asyncio.Queue`.**
   Retries with delay and messages with `DelaySeconds` need the same
   mechanism. A priority queue keyed on `visible_at` gives them one
   codepath.
5. **Why the worker acks on ES failure.** If I re-queue, I double-write
   to Mongo. If I don't re-queue, ES falls behind Mongo but stays
   recoverable. I pick the second.

## 1.8 Phased plan of attack

| Phase | What | Status |
| ----- | ---- | ------ |
| 1. Planning and project understanding | This document | done |
| 2. Scaffolding and repo architecture | Folder layout, deps, config, module boundaries | _upcoming_ |
| 3. Implementation | Models → queue → worker → storage → cache → API → middleware → lifecycle | _upcoming_ |
| 4. Testing | Unit + integration with fakes | _upcoming_ |
| 5. Manual QA against real stack | docker-compose with Mongo + ES + Redis + curl; install Docker on the host | _upcoming_ |
| 6. Final documentation pass | README + ARCHITECTURE with every finding | _upcoming_ |
| 7. Code review → ARB → release | PR, ARB sign-off, merge, release tag, ops hand-off | _upcoming_ |

## 1.9 Risks identified upfront

- **The real stack reveals bugs invisible to fakes-based tests.** Fakes
  don't exercise Mongo-specific syntax (`$dateTrunc`), ES mapping
  (`dynamic_template`), or Redis encoding. **Mitigation:** reserve time
  for phase 5 manual QA against real docker-compose.
- **`ARCHITECTURE.md` ends up as an oversized README.** The reasons
  behind tradeoffs need to be explicit, not assumed. **Mitigation:**
  force the document's structure — diagram, responsibilities,
  rationale, failure modes, scaling, what I'd do differently.
- **The "AI in My Workflow" section sounds like marketing copy.**
  The team has agreed to document AI use for shared learning across
  the team; that's only useful if it's specific — where we pushed
  back on the model, where it was used as a sounding board for
  tradeoffs. **Mitigation:** record concrete examples during
  development, not retroactively at the end.
- **Underestimating documentation time.** The platform team's design
  brief explicitly weights ARCHITECTURE.md as on par with the code:
  *"the architecture document is as important as the code, so budget
  time for it accordingly"*. **Mitigation:** write rationale as I
  decide things, not all on the final day.

## 1.10 Success criteria (how I know I'm done)

- The 7 functional requirements in the platform team's brief are met
  and verifiable.
- All 5 secondary items are implemented (Docker, rate limit, dedupe,
  DLQ, SQS notes).
- Test suite is green + manual QA against real stack is green.
- `ARCHITECTURE.md` covers all 6 mandatory sections (diagram,
  responsibilities, rationale, failure modes, scaling, what I'd do
  differently).
- `README.md` includes setup, endpoints, testing approach, and
  AI in My Workflow.
- **I can defend any decision in the document at ARB**, and a
  future maintainer who's never seen this code can read the doc
  and understand why each tradeoff was made.

---

_Phase 2 in progress..._
