# Phase 3 — Core implementation (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, and
> future maintainers of this service.
> **Stakeholder counterpart:** [`phase_3_implementation_stakeholder.md`](phase_3_implementation_stakeholder.md).
> **Previous phase:** [`phase_2_scaffolding_technical.md`](phase_2_scaffolding_technical.md).
>
> Phase 2 produced a scaffolded repo with no behavior. Phase 3 fills
> it in: queue, worker, persistence, search, cache, API, middleware,
> lifecycle. This is the longest phase by line count, but the
> sequencing matters — each piece is built against the seams the
> previous one exposed.

---

## 3.1 What changed since phase 2

Phase 2 froze the **shape** of the system. Phase 3 puts the
**behavior** in, in this order:

1. **Models** — the contract for what an event is.
2. **Queue** — the async transport between API and worker.
3. **Worker** — batching, retry, dedupe, DLQ.
4. **Storage** — Mongo (source of truth) + ES (secondary index) +
   in-memory fakes.
5. **Cache** — Redis-backed realtime stats with single-flight.
6. **API + middleware** — endpoints + rate limiting.
7. **Lifecycle** — startup/shutdown wiring.

The order matters because each layer is built **against the next one
down's seam**. The API doesn't get built until the cache and stores
exist; the worker doesn't get built until the queue and storage
protocols are in place.

## 3.2 Models — [`app/models/event.py`](../app/models/event.py)

Two Pydantic v2 models:

- **`EventIn`** — the wire format. What clients send.
- **`StoredEvent`** — the internal format. What lives in Mongo + ES.

The split matters because:

- **`EventIn` is strict.** `extra="forbid"`, `min_length=1`,
  `max_length` caps on every string, a custom validator that lowercases
  `event_type`, and a metadata-size cap (`> 64 keys` rejects). The
  goal is to make malformed payloads fail at the boundary, before
  they hit the queue.
- **`StoredEvent` is enriched.** It carries `event_id` (UUID
  generated server-side, used as the idempotency key downstream) and
  `ingested_at` (server timestamp, distinct from the client's
  `timestamp`). The producer doesn't choose these.

### Why a metadata cap?

The design brief lets metadata be a "flexible JSON object." Without a cap,
a misbehaving producer could send `{"k1": ..., "k2": ..., ...}` with
thousands of keys. That would:

1. Blow up Elasticsearch's mapping (every new field becomes a mapping
   entry; mappings have a hard limit).
2. Cripple Mongo's working set (per-document size).
3. Make the worker do work that has no analytic value downstream.

64 keys is generous for legitimate events and stops abuse before it
reaches storage.

## 3.3 Queue — [`app/queue/memory_queue.py`](../app/queue/memory_queue.py)

The single most consequential design decision of this phase.

### Why `asyncio.PriorityQueue` and not `asyncio.Queue`

`asyncio.Queue` is FIFO. That works for happy-path delivery, but two
features the worker needs don't compose with FIFO:

1. **Delayed redelivery on retry.** When the worker NACKs a message
   with backoff, the message should be invisible for `delay` seconds.
2. **Per-message visibility timeout** (SQS-shape semantic).

The clean solution is a priority queue keyed on `(visible_at, seq)`:

```python
self._queue: asyncio.PriorityQueue[
    tuple[float, int, QueueMessage]
] = asyncio.PriorityQueue()
```

Inserting a delayed message just puts it in the queue with
`visible_at = now + delay`. `receive()` pops the head; if it's not
yet visible, puts it back and sleeps until it should be. Both cases
share one codepath.

### The lifecycle: `send` → `receive` → `ack` / `nack`

- `send(body, delay=0)` — wraps the body in a `QueueMessage` and
  enqueues with `visible_at = now + delay`.
- `receive()` — blocks until a message is visible. Increments
  `receive_count` (analogous to SQS's `ApproximateReceiveCount`),
  bumps the in-flight gauge.
- `ack(msg)` — message done. Decrements in-flight.
- `nack(msg, delay)` — re-enqueue with delay. The worker uses this
  for retry; the message carries its incremented `receive_count`.

### Observability primitives

`approximate_size`, `in_flight`, and `join(timeout=...)` are the
foundations for both `/health/queue` and the test helper `drain_queue`.
`join` waits until queue is empty **and** in-flight = 0, so tests
can deterministically wait for the worker to fully consume before
asserting.

### What we don't simulate (and why it's fine here)

- **Visibility timeout as a separate clock.** The in-process worker
  holds the message in memory for the duration of processing; there
  is no separate consumer that could double-deliver. Real SQS needs
  this because consumers are remote.
- **Cross-process durability.** Messages live in memory; a process
  restart loses anything in flight. `ARCHITECTURE.md` §7 has the
  durable upgrade path.

Calling these out in the worker's module docstring is deliberate —
the next person to read the code shouldn't have to reason about
what's missing.

## 3.4 Worker — [`app/workers/event_worker.py`](../app/workers/event_worker.py)

### Batching

The worker spawns `N` concurrent loops (`concurrency` config). Each
loop pulls messages with a max-size / max-wait trigger:

- Wait up to `batch_size` messages.
- If `batch_timeout` elapses with a partial batch, flush anyway.

This bounds latency on low-traffic and gains throughput on high-traffic
without needing two code paths.

### Validation inside the batch loop

Each message body is re-validated against `StoredEvent` before being
added to the batch. The HTTP layer already validates with `EventIn`,
but **the queue is at-least-once**, so a poisoned message that
somehow got in should not block its batch siblings. Invalid payloads
go straight to the DLQ with `reason="invalid_payload"` and the batch
continues.

### Dedupe — two layers

```python
fresh: list[tuple[QueueMessage, StoredEvent]] = []
seen_in_batch: set[str] = set()
for msg in batch:
    event = StoredEvent.model_validate(msg.body)
    key = event.dedupe_key()
    if key in seen_in_batch or key in self._dedupe:
        self.duplicate_count += 1
        await self.queue.ack(msg)
        continue
    seen_in_batch.add(key)
    fresh.append((msg, event))
```

- **`seen_in_batch`** — local set per batch. Catches "same `event_id`
  twice in one flush."
- **`self._dedupe`** — `_BoundedSet` LRU across batches. Catches
  redeliveries from the queue.

### The retry-vs-redelivery distinction

This was the subtle bug I almost shipped. Early version added to the
LRU set **before** the commit. That meant a retry-after-failure had
the same `event_id` already in the set and got silently dropped.

The fix: only add to the LRU set **after** a confirmed Mongo commit:

```python
try:
    await self.store.insert_many(events)
except Exception:
    # retry / DLQ; do NOT add to dedupe
    ...
    return

# Only mark dedupe after commit succeeds
for _, event in fresh:
    self._dedupe.add(event.dedupe_key())
```

A unit test pins this: `test_worker_retries_then_dead_letters_on_store_failure`.

### ES is best-effort

```python
try:
    await self.search.index_many(events)
except Exception as exc:
    logger.warning("worker.es_index_failed",
                   extra={..., "recovery": "rebuild_from_mongo"})
```

The worker does **not** re-queue on ES failure. The rationale lives in
phase 1's tradeoffs and `ARCHITECTURE.md` §3: ES is rebuildable from
Mongo, and re-queueing would double-write Mongo on next attempt.

### Retry policy

```python
async def _retry_or_dlq(self, msg, *, reason):
    if msg.receive_count >= self.max_receive_count:
        await self._send_to_dlq(msg, reason=reason)
        await self.queue.ack(msg)
        return
    delay = self.base_retry_delay * (2 ** (msg.receive_count - 1))
    await self.queue.nack(msg, delay=delay)
```

Exponential backoff: 1s, 2s, 4s. After `max_receive_count` attempts,
DLQ. Each step is observable via the `failed_count` counter; the DLQ
is its own queue, queryable via `/health/queue`.

## 3.5 Storage — Mongo

[`app/storage/mongo.py`](../app/storage/mongo.py).

### Indexes

```python
await self._collection.create_index(
    [("event_type", ASCENDING), ("timestamp", DESCENDING)],
    name="evt_type_ts",
)
await self._collection.create_index(
    [("user_id", ASCENDING), ("timestamp", DESCENDING)],
    name="user_ts",
)
await self._collection.create_index([("timestamp", DESCENDING)], name="ts")
await self._collection.create_index(
    [("event_id", ASCENDING)],
    name="event_id_unique",
    unique=True,
)
```

Each index ties directly to a query pattern (phase 1 specified which).
The `event_id` unique index is **not** for queries — it's for
idempotency under at-least-once delivery. A redelivered message hits
`BulkWriteError` with code 11000 and the wrapper absorbs it.

### Aggregation for `/events/stats`

```python
pipeline = [
    {"$match": {"timestamp": {"$gte": start, "$lt": end}}},
    {"$group": {
        "_id": {
            "event_type": "$event_type",
            "bucket_start": {"$dateTrunc": date_trunc},
        },
        "count": {"$sum": 1},
    }},
    {"$sort": {"_id.bucket_start": 1, "_id.event_type": 1}},
    {"$project": {"_id": 0, ...}},
]
```

The `$match` stage uses the `event_type, timestamp` index. The
half-open window (`$gte` + `$lt`) is deliberate: it composes cleanly
when callers chain buckets. `$lte` would double-count boundary
events.

> Note: the `startOfWeek: "monday"` parameter inside `$dateTrunc` was
> added in phase 6 after manual QA caught Mongo's default Sunday
> boundary. The phase 3 version of this code did not have it.

### `insert_many` semantics

```python
try:
    result = await self._collection.insert_many(docs, ordered=False)
    return len(result.inserted_ids)
except BulkWriteError as exc:
    write_errors = (exc.details or {}).get("writeErrors", [])
    duplicates = sum(1 for e in write_errors if e.get("code") == 11000)
    if write_errors and duplicates == len(write_errors):
        return len(docs) - duplicates
    raise
```

`ordered=False` means a single duplicate doesn't block its batch
siblings. All-duplicates is reported as success. Mixed errors propagate.

> Note: the phase 3 version of this had a broader `except Exception`
> that silently swallowed `NetworkTimeout`. Phase 6 narrowed it to
> `BulkWriteError` after manual QA caught the data-loss bug. The
> current version above is the post-fix one.

## 3.6 Storage — Elasticsearch

[`app/storage/elasticsearch_repo.py`](../app/storage/elasticsearch_repo.py).

### Mapping

```python
"mappings": {
    "dynamic_templates": [
        {
            "metadata_strings": {
                "path_match": "metadata.*",
                "match_mapping_type": "string",
                "mapping": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                },
            }
        }
    ],
    "properties": {
        "event_id": {"type": "keyword"},
        "event_type": {"type": "keyword"},
        "user_id": {"type": "keyword"},
        "timestamp": {"type": "date"},
        "ingested_at": {"type": "date"},
        "source_url": {
            "type": "keyword",
            "fields": {"text": {"type": "text", "analyzer": "standard"}},
        },
        "metadata": {"type": "object", "dynamic": True},
    },
}
```

Three decisions worth defending:

1. **Enums as `keyword`.** Exact terms, fast aggs, no analyzer cost.
2. **URL with `text` subfield.** Exact filter via the parent; partial
   match (path fragments) via `source_url.text`.
3. **Dynamic template for metadata strings.** Open partner-controlled
   metadata mapped consistently as `text` + `.keyword` without
   coupling the index mapping to specific partner fields.

### Search query

`simple_query_string` over `metadata.*` and `source_url.text`, with
filters for `event_type` / `user_id` / time window.

> Note: `lenient: true` was added in phase 6 after manual QA found
> that a numeric `metadata.amount` broke unrelated string searches.
> Phase 3 version did not have it.

## 3.7 Storage — fakes

[`app/storage/fakes.py`](../app/storage/fakes.py) implements both
protocols in-memory. The fakes are intentionally faithful to the
**query semantics** that matter — same half-open windows for stats,
same event_type-as-keyword matching for find, same substring matching
for search. They're simpler than the real impls, but the tests that
exercise them catch real bugs, not mock-shaped ones.

> Note: phase 5 / 6 found cases where fakes diverged from real Mongo /
> ES behavior in ways tests couldn't see (timestamps tz-naive, ES
> mapping inference). The fakes are useful for the **business logic**
> they test; they're not a substitute for phase 5's stack QA.

## 3.8 Cache — [`app/cache/realtime.py`](../app/cache/realtime.py)

### Single-flight

```python
async def get_or_compute(self, window_seconds, compute):
    cached = await self.get(window_seconds)
    if cached is not None:
        return cached, True

    lock = self._locks.setdefault(self._key(window_seconds), asyncio.Lock())
    async with lock:
        cached = await self.get(window_seconds)  # double-check
        if cached is not None:
            return cached, True
        value = await compute()
        await self.set(window_seconds, value)
        return value, False
```

If N concurrent requests arrive at TTL expiry, only one runs the
Mongo aggregation; the rest wait for the lock and read the freshly
written value. Without this, the realtime endpoint's tail latency
degrades exactly when its cache is hottest.

### TTL-only invalidation

`set` uses Redis's `EX` flag. There is no explicit invalidation path.
The `invalidate(window_seconds)` method exists for tests but is not
called from the ingest path. The rationale is in `ARCHITECTURE.md` §6
and was committed in phase 1.

## 3.9 API + middleware

### Endpoints

- [`app/api/ingestion.py`](../app/api/ingestion.py) — POST /events.
  Validates with EventIn, enriches via `StoredEvent.from_input`,
  enqueues. Returns 202 with `event_id`.
- [`app/api/queries.py`](../app/api/queries.py) — the four query endpoints.
  Each is thin: parameter validation via Pydantic Query, delegate to
  the store/search/cache, return.
- [`app/api/health.py`](../app/api/health.py) — `/health` (liveness) +
  `/health/queue` (worker observability).

### Rate limit middleware

[`app/middleware/rate_limit.py`](../app/middleware/rate_limit.py) —
token bucket keyed on client IP. Skips `/health*` so monitors don't
get rate-limited. The bucket lives in-process; a multi-replica deploy
would move it to Redis (see `ARCHITECTURE.md` §6 / §12).

## 3.10 Lifecycle — [`app/lifecycle.py`](../app/lifecycle.py)

FastAPI lifespan context manager that:

1. Reads settings.
2. Constructs Mongo / ES / Redis clients with the appropriate timeouts.
3. Calls `ensure_ready` on the stores (creates indexes, ES mapping).
4. Constructs the queue + DLQ + worker.
5. Starts the worker.
6. Attaches everything to `app.state` so the route handlers can access
   it via DI.
7. On shutdown, stops the worker, closes clients.

The `attach_state` helper is a separate function so tests can wire
fakes into `app.state` without running the real lifespan.

> Note: the Mongo client construction with `serverSelectionTimeoutMS`
> + `socketTimeoutMS` is the phase 6 version. The phase 3 version
> used defaults; this was caught and corrected in manual QA.

## 3.11 What carried forward to phase 4

Tests. The architecture decisions of phase 3 (Protocol-based stores,
in-memory fakes, observability primitives on the queue, structured
counters on the worker) were all made with phase 4 in mind. The next
phase locks the behavior down with a test suite that runs in under
2 seconds.

## 3.12 Cross-references

- Full module structure: [`README.md` → Project layout](../README.md).
- Failure mode catalogue per layer: [`ARCHITECTURE.md` §8](../ARCHITECTURE.md).
- Indexing rationale, full text: [`ARCHITECTURE.md` §5](../ARCHITECTURE.md).
- Queue ↔ SQS comparison: [`ARCHITECTURE.md` §7](../ARCHITECTURE.md).

---

_Phase 4 in progress..._
