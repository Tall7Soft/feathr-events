# Architecture

This document is the primary design artifact for v0.1 of the
Distributed Event Processing Platform, alongside the code itself. It
explains _why_ the system is shaped the way it is, where the
load-bearing tradeoffs are, what happens when things go wrong, what
the service commits to operationally, where the trust boundaries are,
and how the design would evolve if event volume 10×'d tomorrow.

> **Looking for the development journal?** See [`docs/`](docs/) for a
> phased account of how the design came together, with parallel
> technical and stakeholder-facing tracks per phase. It's optional
> context; this document is the deliverable.

---

## 1. System diagram

```
                                        ┌──────────────────────────┐
                                        │   Redis (cache)          │
                                        │   key: stats:realtime:<w>│
                                        │   TTL-only invalidation  │
                                        └─────────────▲────────────┘
                                                      │ get / setex
                                                      │
  ┌──────────┐   POST /events   ┌───────────────┐     │
  │  Client  │ ───────────────► │   FastAPI     │ ────┤
  └──────────┘                  │   ingestion   │     │
        ▲                       │   router      │     │
        │ 202 Accepted          └──────┬────────┘     │
        │                              │ enqueue      │
        │                              ▼              │
        │                       ┌──────────────┐      │
        │                       │  In-process  │      │
        │                       │  queue       │      │
        │                       │  (SQS-shape) │      │
        │                       └──────┬───────┘      │
        │                              │ receive      │
        │                              ▼              │
        │                       ┌──────────────┐      │
        │                       │  Async       │      │
        │                       │  worker pool │ ─────┤── on store failure → retry w/ backoff
        │                       │  (batching)  │      │── exhausted retries → DLQ
        │                       └──┬────────┬──┘      │── bad payload → DLQ immediately
        │                          │        │         │
        │                          │ Mongo  │ ES      │
        │                          │ bulk   │ bulk    │
        │   GET /events            ▼        ▼         │
        │   GET /events/stats   ┌─────┐  ┌─────┐      │
        │   GET /events/search  │Mongo│  │ ES  │      │
        ◄───────────────────────│SoT  │  │idx  │──────┘
                                └─────┘  └─────┘
        ◄── GET /events/stats/realtime ── compute on miss ─┐
                                                            ▼
                                                         Redis
```

---

## 2. Component responsibilities

| Layer            | Owns                                                                                          | Does NOT own                                              |
| ---------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| **API**          | Validation, rate limiting, enqueue, query orchestration, cache lookups. Auth model lives at the platform-ingress layer in v0.1 (see §11); service-level auth is a §12 follow-up. | Persistence. The API never writes to Mongo or ES directly.|
| **Queue**        | Buffering between ingest and persistence, retry transport, delayed redelivery, dead lettering.| Business logic about retries — that's the worker.         |
| **Worker**       | Batching, payload validation, write fan-out (Mongo → ES), retry policy, dedupe, DLQ routing.  | HTTP. The worker has no notion of who sent the event.     |
| **Mongo store**  | Source of truth. Time-window filters, per-user/per-type lookups, aggregations, realtime counts.| Full-text search.                                        |
| **ES search**    | Full-text and ad-hoc analytics over open-ended metadata.                                      | Source of truth — ES is rebuildable from Mongo.           |
| **Redis cache**  | Sub-second realtime stats reads.                                                              | Long-lived state. Any cached value must be reconstructible.|

**Why these cut lines?** Each layer has a different operational profile
(latency budget, durability requirement, scaling axis). Keeping their
contracts explicit means we can swap any one of them — in-process queue →
real SQS, single-node ES → managed Elastic Cloud, Redis → DynamoDB DAX —
without touching the others.

---

## 3. Data flow

### Ingest

1. Client `POST /events`.
2. Pydantic validates payload, normalizes `event_type` to lowercase,
   caps metadata to 64 keys (defense-in-depth against abusive payloads).
3. `StoredEvent.from_input` assigns `event_id` and defaults `timestamp` to
   `now()` if absent.
4. The HTTP handler enqueues the dict body and returns `202 Accepted` with
   the `event_id`. P99 of `POST /events` is bounded by validation +
   `asyncio.Queue.put` — not by storage latency.

### Worker

1. N concurrent loops pull messages with a max-size/max-wait batch trigger
   (default: 50 messages or 1 s, whichever comes first).
2. Per message: validate against `StoredEvent` (defends against poisoned
   payloads from a misbehaving producer), check the bounded LRU dedupe set,
   then accumulate into the current batch.
3. Persist to Mongo via `insert_many(ordered=False)`. On `DuplicateKeyError`
   for `event_id`, treat as already-persisted (idempotency under at-least-once
   delivery). On any other failure, NACK every message in the batch with an
   exponential backoff delay; exhausted retries go to the DLQ.
4. Index to ES via the bulk helper. ES is **best-effort**: on failure we log
   and ack the message anyway, because Mongo is authoritative and we can
   reindex from Mongo. We do NOT re-queue, because that would double-write
   to Mongo on retry.
5. Ack all successful messages.

### Query

- `/events` is a Mongo `find` with index-friendly filters.
- `/events/stats` runs a `$match` → `$group` (using `$dateTrunc`) → `$sort`
  pipeline, returning `(event_type, bucket_start, count)`.
- `/events/search` proxies to ES with a `simple_query_string` over
  `metadata.*` and `source_url` plus a date filter.
- `/events/stats/realtime` looks up Redis; on miss it holds a per-window
  asyncio lock, recomputes from Mongo's realtime aggregation, and writes
  back with TTL.

---

## 4. Storage rationale — why split across Mongo, ES, and Redis?

**Mongo for source of truth.** Open-ended metadata makes a fixed relational
schema unattractive. Aggregations are first-class in Mongo; `$dateTrunc`
gives us hourly/daily/weekly buckets without an ETL layer. Indexing the
hot query shapes is cheap and well-understood.

**Elasticsearch for full-text.** Mongo can do text indexes, but ES is the
right answer for partner-controlled metadata that we want analysts to slice
by arbitrary string fragments. The dynamic mapping template (`text` with a
`.keyword` subfield for any metadata string) gives us both full-text and
exact-match without re-indexing when a partner ships a new field.

**Redis for realtime.** The realtime endpoint's value comes from being
fast under repeated polling. Computing it from Mongo on every request is
fine for tens of QPS; it becomes wasteful at hundreds. Redis with a short
TTL is the well-trodden answer.

### Why _not_ collapse to a single store?

- Mongo + ES collapsed → either we lose principled full-text search
  (Mongo's text index is OK, not great) or we make ES the source of truth
  and inherit its operational complexity for durability concerns it isn't
  designed for.
- Mongo + Redis collapsed → realtime endpoint becomes
  Mongo-aggregation-on-every-request; works at low QPS, doesn't scale.

---

## 5. Indexing strategy

### MongoDB

| Index                              | Why                                                                                       |
| ---------------------------------- | ----------------------------------------------------------------------------------------- |
| `(event_type, timestamp desc)`     | Most common filter shape; also accelerates the stats pipeline's `$match`.                 |
| `(user_id, timestamp desc)`        | Per-user history queries — the "what did user X do last week?" shape.                     |
| `(timestamp desc)`                 | Bare time-range scans + realtime aggregation; the "recent N seconds" case.                |
| `event_id` (unique)                | Idempotency. Ensures retries from the queue can't create duplicates.                      |

**Deliberately not indexed:**

- `source_url`. Cardinality varies wildly per tenant; in the high-cardinality
  case the index dominates working set; in the low-cardinality case the
  `event_type, timestamp` index already filters most documents and a
  collection scan over the remainder is cheap. If a real workload showed a
  hot per-URL query pattern, we'd add it then.
- `metadata.*`. Open-ended; one multikey index per partner field would
  fragment the index catalog and dwarf the data. ES is the right tool for
  ad-hoc metadata queries.

### Elasticsearch

The mapping (see `app/storage/elasticsearch_repo.py`) follows three
principles:

1. **Enums as `keyword`.** `event_type`, `user_id`, `event_id` — we want
   exact terms, fast aggregations, no analyzer cost.
2. **URLs as `keyword` + `text` subfield.** Exact-URL filter is the common
   case; the text subfield lets analysts match a path fragment without
   re-indexing. The search endpoint targets `source_url.text`, not the
   parent `keyword`, so users can match `/checkout` against
   `https://feathr.co/checkout/v2`.
3. **Dynamic template for metadata strings.** Anything matching `metadata.*`
   of type `string` is mapped to `text` with a `.keyword` subfield via a
   `dynamic_template`. This gives full-text on the values plus exact
   filtering without coupling the mapping to specific partner fields.

We start with `1 shard, 0 replicas` — fine for a single-node demo and a
sane baseline for one analyst-facing index. Production would set the
shard count based on target index size (target ≤ 30 GB / shard) and
replicas based on tolerable read load + node count.

**`lenient: true` on `simple_query_string`.** Manual QA surfaced the
trap: a partner who sends `metadata.amount: 99.99` causes ES to map
that field as `double`. The next text search ("spring") then explodes
with `failed to create query: For input string: "spring"` because the
parser tries to coerce the term against every matched field. The
`lenient` flag tells ES to skip incompatible field types instead of
failing the whole query. Cost: a numeric field that contains a numeric
search term will be silently skipped from full-text matching, which
matches our intent here (we don't expect users to search numerically
via this endpoint).

---

## 6. Caching strategy (`/events/stats/realtime`)

**Approach.** TTL-only invalidation. The endpoint advertises eventual
consistency on the order of the TTL; we accept that, and in exchange we
avoid bursting cache busts on every event ingest.

**TTL rationale.** Default 10 s. That's the sweet spot for an
operations-dashboard use case: refresh cadence matches typical UI polling,
and the data is stale by ≤ the human time-to-react. Configurable per
deploy.

**Key design.** `stats:realtime:<window_seconds>`. Distinct windows do not
share entries; the TTL still bounds staleness independently for each.

**Dogpile protection.** A per-window `asyncio.Lock` single-flights the
recompute, so if N callers arrive at TTL expiry, only one runs the Mongo
aggregation and the rest read the freshly written value. Without this,
the realtime endpoint's tail latency degrades exactly when its cache is
hottest.

**What would change under high write volume?**

- **Switch to write-through-on-aggregate.** A background ticker would
  recompute the window every TTL seconds regardless of read traffic and
  write the result. Reads then never miss; tradeoff is wasted compute
  during quiet periods, but at sustained high QPS that wasted compute is
  negligible vs. the cache-miss penalty.
- **Move single-flight to Redis.** `SET key value NX EX <ttl>` becomes the
  lock primitive once we have > 1 app replica. The in-process lock is
  correct only on a single node.
- **Pre-bucket counters in Redis.** Push a `HINCRBY` into a per-minute
  bucket from the worker. The endpoint then reads ~5–60 small hash
  buckets and sums client-side — O(1) on the read path, near-zero
  amortized cost on the write path. This is the right shape if realtime
  goes from "nice-to-have" to "p99 < 50 ms under load."

---

## 7. Queue design + SQS comparison

### What the in-process queue actually provides

| Property                | In-process queue                                               | Real SQS                                  |
| ----------------------- | -------------------------------------------------------------- | ----------------------------------------- |
| Delivery                | At-least-once (NACK re-enqueues)                               | At-least-once (standard) / Exactly-once (FIFO) |
| Ordering                | Best-effort by `(visible_at, seq)` priority                    | None (standard) / Per-MessageGroupId (FIFO)   |
| Delay                   | Per-message `delay` param honored via priority queue           | Per-message `DelaySeconds`                |
| Visibility timeout      | Implicit (worker holds message until `ack`/`nack`)             | Explicit timeout; auto-redelivery on expiry |
| Durability              | In-memory; process restart loses in-flight work                | Durable, replicated                       |
| Throughput              | Bounded by single-process event loop                           | Effectively unbounded                     |
| Cross-process consumers | No                                                             | Yes                                       |
| Dead-letter queue       | Yes (separate `InMemoryQueue("events-dlq")`)                   | Yes (configured DLQ ARN)                  |

### What would change for a real SQS swap

1. **Visibility timeout becomes explicit.** Today, the worker holds the
   message in memory; on SQS, the message returns to the queue after
   `VisibilityTimeout` if not deleted. Implication: the worker needs a
   `ChangeMessageVisibility` heartbeat for any task whose processing time
   may approach the timeout (long bulk inserts, ES retries).
2. **Idempotency becomes non-negotiable.** SQS's at-least-once delivery is
   not best-effort; redelivery is normal. The existing `event_id`-unique
   Mongo index and the worker's dedupe set are already the right shapes;
   we'd promote the dedupe set to Redis (per-replica wouldn't work).
3. **Batching shifts.** SQS supports up to 10 messages per `ReceiveMessage`
   call; we'd batch around the network call, not just the in-process loop.
4. **DLQ becomes infrastructure.** Configure a separate SQS queue as the
   DLQ via `RedrivePolicy` instead of explicit `dlq.send()`.
5. **Backpressure becomes the producer's problem.** The API today does an
   in-process `Queue.put`; with SQS it becomes a network call with its
   own latency and failure modes. We'd add a small in-process buffer plus
   bulk `SendMessageBatch` to amortize.

The transport interface in `app/queue/memory_queue.py` was designed
specifically so a `SQSQueue` implementing the same `send` / `receive` /
`ack` / `nack` surface would be a drop-in for the worker.

---

## 8. Failure modes

### Mongo unavailable
- API reads (`/events`, `/events/stats`) → 500. Acceptable; the API has no
  meaningful degraded mode here other than "fail fast and let the LB
  reroute."
- Realtime endpoint → cache absorbs the outage up to TTL; after that, 500.
- Worker → `insert_many` raises; the batch is NACK'd with backoff. After
  `max_receive_count` attempts, batched messages go to the DLQ. Operator
  intervenes by drain-replaying the DLQ once Mongo is healthy.

**Timeout configuration is load-bearing here.** Motor / pymongo defaults
let an operation block for ~30 s on server selection and indefinitely on
a hung socket read. Two settings keep the worker responsive:

- `serverSelectionTimeoutMS` (default 3 s in our config) — bounds how
  long a new operation waits to find a healthy server.
- `socketTimeoutMS` (default 10 s) — bounds how long an in-flight read
  can wait. **Without this, a paused-but-reachable Mongo (network
  partition, `SIGSTOP`, frozen VM) blocks the worker forever even though
  server selection has already succeeded.** This was caught during manual
  QA: `docker pause feathr-mongo` does not break the TCP connection, so
  server selection sees a "healthy" server, the write goes onto the
  wire, and the response read hangs.

### The phantom-write scenario
Under a `pause`/`unpause` cycle, an event can end up persisted in Mongo
**and** dead-lettered by the worker:

1. Worker writes BSON for event X to the wire.
2. `socketTimeoutMS` fires before Mongo responds; worker NACKs.
3. After 3 NACKs, the message lands in the DLQ.
4. Mongo is unpaused; the BSON that was sitting in its receive buffer
   is processed; event X is persisted.

The system tolerates this without corruption because of two invariants:

- **`event_id` carries a unique index.** When the DLQ is later
  re-driven, `insert_many` raises `BulkWriteError` with code 11000.
  `MongoEventStore.insert_many` recognizes the all-duplicates case and
  reports it as a successful no-op.
- **The worker's `_BoundedSet` only marks a dedupe key *after* a
  confirmed write.** Retried-after-failure messages are not falsely
  suppressed.

A previous version had a catch-all `except Exception` in `insert_many`
that interpreted any error with no `writeErrors` field as "all
duplicates absorbed" and reported success. That silently lost data on
`NetworkTimeout` and was the single most consequential bug surfaced by
manual QA. The unit tests in `tests/unit/test_mongo_store.py` now pin
the correct semantics.

### Elasticsearch unavailable
- `/events/search` → 5xx. This is the only endpoint that hard-depends on ES.
- Worker → log the failure, ack the message anyway. **Mongo is
  authoritative and ES is rebuildable.** Recovery is a one-shot reindex
  from Mongo (a job, not a per-request retry). This is the single most
  load-bearing design decision in the worker: it keeps ES outages from
  cascading into ingest backpressure.

### Redis unavailable
- `/events/stats/realtime` → cache misses become Mongo aggregations.
  Endpoint degrades to "slow but correct." We'd add a circuit breaker so
  that Redis-down doesn't add per-request connection-timeout latency.

### Worker crashes mid-batch
- In-process today: messages held in memory are lost. With SQS, the
  visibility timeout would expire and the messages would be redelivered;
  the dedupe set + `event_id` unique index keep that safe.

### API replica fails
- Stateless; LB routes around it. Any in-flight `POST` that hadn't yet
  returned 202 is lost from the client's perspective — client retry is
  the answer. (Same as any HTTP API; not specific to this design.)

### DLQ overflow
- Operator concern, not an automated one. The right shape is a metric +
  alert on `dlq_depth > N`, plus a CLI/admin endpoint to inspect and
  redrive. Not built in v0.1; tracked in §12.

---

## 9. Scaling: what breaks first at 10×?

The current default deploy handles ~hundreds of events/sec comfortably on
a single replica. At 10× (a few thousand/sec sustained), here's the
expected failure order:

1. **In-process queue becomes the bottleneck.** Single event loop, single
   process. Fix: swap in real SQS (or Kafka, if we need replay/ordering).
   The transport interface was built for this swap.
2. **Mongo write throughput.** `insert_many` is good but `(event_id)`
   unique index inserts on a hot collection cap us. Fix: shard the
   `events` collection on `(event_type, _id)` or `(user_id, _id)`
   depending on the dominant query shape; right now the obvious shard
   key is `(timestamp, event_id)` for time-locality.
3. **ES indexing pressure.** Best-effort + async retries help, but the
   bulk helper's throughput on a single-shard index will tap out. Fix:
   bump shards, move indexing to a dedicated worker pool so storage and
   indexing scale independently. At higher scale, consider a "hot/warm"
   architecture (recent days on fast nodes, older on cheaper nodes).
4. **Realtime cache contention.** Single-flight is per-window per-replica;
   N replicas all recomputing on TTL expiry is fine at small N but wastes
   Mongo cycles as N grows. Fix: pre-bucket counters in Redis (§6).
5. **API CPU.** FastAPI is fast; the dominant cost will be Pydantic
   validation on `POST /events`. Fix: horizontal scale; the API is
   stateless.

### The other "10× breaks first" answer
The honest one: **the producer side**. A web pixel firing a `POST /events`
synchronously for every pageview at 10× volume saturates the API tier
long before Mongo cares. The right answer at that scale is to push events
into a client-side batched producer (or a Kinesis Firehose / Kafka direct
producer) and have the API tier handle a lower QPS of bulk writes. That's
out of scope here but worth naming.

---

## 10. SLOs and operational targets

This section pins the numeric commitments the service makes to its
clients. These are the targets ops monitors against in production
and the bar against which we evaluate degradation events. The
failure-mode analysis in §8 assumes these targets are in scope; if
they aren't met, that analysis has to be revisited.

### Latency

| Endpoint | p50 | p99 | What dominates the budget |
| -------- | --- | --- | ------------------------- |
| `POST /events` | 5 ms | 50 ms | Pydantic validation + `asyncio.Queue.put`. Independent of storage latency by design — the 202 returns before persistence. |
| `GET /events` | 50 ms | 200 ms | Mongo `find` over an index-friendly filter; capped at `limit ≤ 1000`. |
| `GET /events/stats` | 200 ms | 1 s | Mongo aggregation; degrades roughly linearly with window size. |
| `GET /events/search` | 100 ms | 500 ms | ES `simple_query_string`; degrades with index size. |
| `GET /events/stats/realtime` (cache hit) | 2 ms | 10 ms | Single Redis `GET`. |
| `GET /events/stats/realtime` (cache miss) | 100 ms | 400 ms | Mongo aggregation, single-flighted (§6) so concurrent misses do not stack. |

The `POST /events` target is the load-bearing SLO. The dashboard
launch implicitly depends on it. The async pipeline exists
specifically to make this target attainable independent of storage
backpressure.

### Throughput

Targets for a single replica on the platform's standard pod size
(2 vCPU, 4 GiB):

| Workload | Sustained | Burst | What gives first beyond this |
| -------- | --------- | ----- | ---------------------------- |
| Ingest, total | 5,000 events/s | 10,000 events/s for 30 s | In-process queue / single event loop (§9 item 1) |
| Ingest per tenant | 500 events/s | 2,000 events/s for 30 s | Same; per-tenant fairness is not enforced at this layer |
| Realtime reads per cached window | 100 QPS | 500 QPS for 10 s | Single-flight on Mongo aggregation |
| Search | 50 QPS | 200 QPS for 10 s | ES single-shard throughput |

Horizontal scale lifts most of these. The queue is the standout —
single-process by design — and is the first thing to swap (§9, §7).

### Availability

| SLO | Target | Window |
| --- | ------ | ------ |
| Service availability (2xx/3xx ratio, excluding 429) | 99.9% | Rolling 30 d |
| Ingest durability — events 202-accepted that reach Mongo or DLQ | 99.99% | Rolling 30 d |
| Realtime endpoint freshness — data age at response time | ≤ 2 × TTL (default ≤ 20 s) | p99 over 1 h |

**Ingest durability is the SLO that costs the most discipline.** The
worker's retry / backoff / DLQ policy is calibrated to it: every
event either lands in Mongo or lands in the DLQ where ops can
re-drive it. There is no third path (silent drop). Phase 5 / 6
specifically closed a bug where the third path existed; the
regression tests in `tests/unit/test_mongo_store.py` exist to keep
it closed.

### Recovery-time objectives

| Scenario | RTO | Mechanism |
| -------- | --- | --------- |
| Mongo brief outage (< 1 min) | Auto-recovers within ~45 s | `socketTimeoutMS` + retry budget; events that exhaust retries land in the DLQ |
| ES brief outage | No ingest impact; search recovers as ES returns | Worker acks on ES failure (§3, §8) |
| Redis brief outage | Realtime endpoint degrades to direct Mongo aggregation; immediate recovery | Cache miss path |
| App pod restart | < 30 s; in-flight messages drain via `worker.stop(drain_timeout=5s)` | Lifecycle hook + healthcheck |

### Out of scope for v0.1

- **Cross-region replication.** Single-region single-replica for
  v0.1; multi-region is a platform-team roadmap item, not this
  service's.
- **Data-retention guarantee.** Events live indefinitely until ops
  settles on a retention policy. §12 follow-up.
- **Exactly-once at the API boundary.** Client-provided idempotency
  keys would let producer-side retries dedupe; today the server
  generates `event_id`, so two HTTP retries from the same client
  create two events. §12 documents the path.

---

## 11. Security and trust boundaries

This section makes the trust model explicit so ops, security review,
and future maintainers don't have to infer it from scattered code or
pattern-match against other services.

### Trust model (v0.1)

The service runs inside Feathr's platform VPC behind the existing
ingress. Calls reach it from two distinct origins:

- **Authenticated internal services** — the dashboard backend, the
  on-call investigation tooling, batch backfill jobs. Trusted via
  VPC + service-mesh identity. No application-level auth in v0.1.
- **Customer-side ingestion** — the web pixel / JS SDK posting
  events from browsers. **This is the public-facing surface, and
  for v0.1 it is rate-limited but not authenticated.**

The second case is the load-bearing security gap of v0.1. It is
documented explicitly here (rather than left implicit elsewhere) so
that security review knows exactly what we are shipping, and ops
knows exactly what to monitor for.

### Threat model and mitigations

| Threat | Mitigation in v0.1 | Residual risk / follow-up |
| ------ | ------------------ | ------------------------- |
| Per-IP flood / DoS | Token-bucket rate limiter (`RATE_LIMIT_PER_MINUTE`, default 600/IP/min); 429 with `Retry-After` | Shared-IP (corporate NAT, mobile carrier) hits the same bucket; can false-positive |
| Oversized / malformed payloads | Pydantic validation at the boundary; metadata capped at 64 keys; URL ≤ 2048 chars; `extra="forbid"` | None for v0.1 |
| Cross-tenant data exposure via query API | `user_id` carries a tenant prefix by convention; queries do **not** enforce tenant isolation at the API layer in v0.1 | **Open** — §12: tenant-scoped API keys + server-side filter on tenant claim |
| Replay attacks on ingest | At-least-once delivery + `event_id` unique index absorbs duplicates | No request-level signing |
| PII / PHI in metadata | Partner-controlled; no scrubbing layer; we treat metadata as potentially sensitive in storage | **Open** — §12: schema registry / contract per partner with field-level classifications |
| Log exfiltration via crafted payloads | Structured JSON logger escapes values; INFO does not log full event bodies | None known |
| SSRF via metadata (worker → external URL) | Metadata is never used to construct outbound requests | N/A by design |

### Data sensitivity

Events are partner-customer behavioral data: page views, clicks,
conversions, sometimes carrying user identifiers or device
fingerprints. **By default we treat metadata as potentially
carrying PII** even though we don't parse it.

Encryption-at-rest for Mongo / ES / Redis is delegated to the
platform deployment (managed encryption on the volumes the
containers mount). The service does not add a second encryption
layer; doing so would conflict with the indexed-by-default storage
model.

### What security review needs to sign off on at v0.1

1. Explicit acknowledgement that customer-side ingestion is
   **unauthenticated by design at this milestone** — rate-limited
   and capacity-bounded; full auth is §12.
2. Confirmation that platform-level encryption-at-rest satisfies
   the data classification for "partner behavioral data."
3. Agreement on a follow-up ticket for **tenant-scoped API keys**
   before we open the service to public-internet customer traffic
   at scale.

These three items become the security row of the ARB checklist and
the security section of the ops onboarding doc.

---

## 12. What I'd do differently with more time

In rough order of payoff:

- **Real durable queue (SQS or NATS JetStream).** Single biggest robustness
  win. Removes the in-process loss-on-restart caveat and unlocks horizontal
  worker scaling.
- **Observability.** Today: structured JSON logs + a `/health/queue`
  endpoint. Production: Prometheus metrics (queue depth, batch size
  histogram, per-stage latency, dedupe hit rate), OpenTelemetry tracing
  across enqueue → worker → store, a dashboard pinned to the four golden
  signals.
- **Idempotency at the producer boundary.** Accept an optional
  `event_id` on `POST /events`. Then producer-side retries are safe end-
  to-end without relying on the dedupe set.
- **Schema registry / contract for `metadata`.** Today it's open JSON.
  At scale, you want a typed contract per partner so ES mappings stay
  small and analytics queries don't surprise users.
- **Authn/z.** Today: no auth. Production: tenant-scoped API keys, plus
  optional per-event signing to defend against high-volume abuse where
  rate limiting on IP isn't enough.
- **DLQ tooling.** Admin endpoint to list / inspect / redrive DLQ
  messages. Without this, the DLQ is a black hole that operations has to
  poke at with raw Mongo / SQS commands.
- **Property-based testing on the worker retry state machine.** The
  combinations of (poisoned payload | mongo fail | es fail) × (batch
  position | retry count) are exactly what hypothesis is for.
- **Real-Mongo / real-ES contract tests in CI.** The current tests use
  in-memory fakes; that catches the seams between our code but not, e.g.,
  a regression where Mongo deprecates `$dateTrunc` or our ES query syntax
  drifts. A nightly job with testcontainers would close that gap.

---

## Appendix: findings from manual QA

The system was stood up end-to-end via `docker compose up` and exercised
through curl. Findings that resulted in code changes (rather than
test-only additions):

| Finding | Fix |
| --- | --- |
| Naive UTC datetimes in API responses (Mongo's BSON Date strips tzinfo, Motor surfaces the result naive). | `MongoEventStore._from_doc` tags `timestamp`, `ingested_at`, `bucket_start` with `tzinfo=UTC`. |
| ES `simple_query_string` fails when partner metadata contains numerics (`failed to create query: For input string: "spring"`). | `lenient: true`; default search fields target `source_url.text`. |
| Realtime cache emits `+00:00`-style datetimes; FastAPI/Pydantic emits `Z`-style. | `_json_default` rewrites `+00:00` → `Z` to keep the two paths byte-identical. |
| Worker silently reported `processed_count += 1` on `NetworkTimeout` because the catch-all in `insert_many` treated *any* exception with no `writeErrors` as "all duplicates." Verified by `db.events.countDocuments` showing zero rows while the counter said it persisted. | Catch `BulkWriteError` specifically; require `write_errors` to be non-empty before treating duplicates as the success case. |
| Worker blocked indefinitely during a Mongo `pause` because `serverSelectionTimeoutMS` only fires on a fresh selection — established connections need `socketTimeoutMS`. | Pin both via `Settings`; defaults are 3 s and 10 s. |
| Mongo's `$dateTrunc` defaults weekly bucket boundaries to Sunday, but the in-memory fake (and the dashboards we'd build on top) use ISO weeks (Monday). | Pin `startOfWeek: "monday"` in the pipeline. |

The two most consequential of those — silent data loss on
`NetworkTimeout`, and worker hangs on a frozen-but-reachable Mongo —
would not have been caught by unit tests against the in-memory fakes
alone. They are now covered by `tests/unit/test_mongo_store.py` and
documented in §8.
