# Phase 4 — Testing strategy and coverage (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, and
> future maintainers of this service.
> **Stakeholder counterpart:** [`phase_4_testing_stakeholder.md`](phase_4_testing_stakeholder.md).
> **Previous phase:** [`phase_3_implementation_technical.md`](phase_3_implementation_technical.md).
>
> Phase 3 produced a working system. Phase 4 locks the behavior in
> with a test suite that's fast enough to run on every save, exercises
> the seams that actually matter, and is honest about its blind spots
> (which phase 5 then addresses).

---

## 4.1 What changed since phase 3

Phase 3 had behavior but no guardrails. Phase 4 adds:

- **30 tests** (this number grows to 48 after phase 6) across unit
  and integration boundaries.
- A test fixture topology that wires the real FastAPI app, the real
  queue, the real worker, and the real (fakeredis) cache, with
  **in-memory fakes substituting for Mongo and Elasticsearch**.
- An end-to-end "drain the queue and assert" pattern for integration
  tests.
- A `pythonpath` + `asyncio_mode = "auto"` config that keeps test
  files free of decorator boilerplate.

End-state: `pytest` runs in ~2 seconds, green.

## 4.2 Testing philosophy

Four explicit commitments:

1. **Tests catch behavior changes in the seams, not third-party code.**
   We don't test that Pydantic validates fields, or that Motor inserts
   documents. We test that **the worker treats an ES failure
   differently from a Mongo failure**, that **the queue's retry
   delays are honored**, that **the cache is single-flighted**.
2. **Fakes implement the same protocol, with the same query
   semantics.** Substituting a fake for `MongoEventStore` shouldn't
   change a single line of business logic. The integration tests
   exercise the **same** worker and **same** API as production —
   only the storage backend differs.
3. **Fast feedback over comprehensive coverage.** A test suite that
   takes 5 minutes won't run on every save. We don't chase the last
   percentage of coverage; we chase the behaviors that matter.
4. **Honest about blind spots.** This phase doesn't pretend to cover
   Mongo's `$dateTrunc` syntax, ES mapping inference, or Redis
   encoding. Those need real infrastructure to test, and that's
   phase 5's job.

## 4.3 Test topology

```
tests/
├── unit/
│   ├── test_queue.py          # queue semantics: send/receive/ack/nack/delay/join
│   ├── test_worker.py         # batching, retry, DLQ, dedupe, ES-failure-ack
│   ├── test_validation.py     # EventIn validators
│   ├── test_cache.py          # TTL, single-flight, invalidation
│   └── test_aggregation.py    # stats math against deterministic time buckets
├── integration/
│   ├── test_ingest_to_query.py     # POST → drain → GET (happy path + 422)
│   └── test_search_and_realtime.py # POST → drain → search + realtime cache hit/miss
├── conftest.py                # fixtures: settings, stack, app, client
├── helpers.py                 # drain_queue helper
└── __init__.py
```

Five unit modules + two integration modules. The phase 6 additions
(mongo store, ES query, stats pipeline, rate limit) are not yet here
— they come from manual QA findings in phase 5.

## 4.4 The fixture topology — [`tests/conftest.py`](../tests/conftest.py)

```python
@pytest_asyncio.fixture
async def stack(test_settings, fake_redis):
    store = InMemoryEventStore()
    search = InMemoryEventSearch()
    cache = RealtimeStatsCache(fake_redis, ttl_seconds=2)
    queue = InMemoryQueue("events")
    dlq = InMemoryQueue("events-dlq")
    worker = EventWorker(queue=queue, dlq=dlq, store=store, search=search, ...)
    await worker.start()
    try:
        yield {"settings": ..., "store": ..., ...}
    finally:
        await worker.stop(drain_timeout=1.0)
```

Then:

```python
@pytest_asyncio.fixture
async def app(stack):
    application = FastAPI()
    application.include_router(health_router)
    application.include_router(ingestion_router)
    application.include_router(queries_router)
    attach_state(application, **stack)
    return application

@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

Each integration test gets a freshly-constructed app with isolated
fakes. The `attach_state` helper (defined in `app/lifecycle.py`) is
the seam that lets us skip the real Mongo/ES connection during tests.

## 4.5 The "drain the queue" pattern

The big mistake to avoid in async integration tests is **asserting on
the result before the worker has finished consuming**. We avoid this
with an explicit `drain_queue` helper:

```python
async def drain_queue(queue: InMemoryQueue, *, timeout: float = 2.0) -> None:
    drained = await queue.join(timeout=timeout)
    if not drained:
        raise AssertionError("queue did not drain within timeout")
    await asyncio.sleep(0.05)  # post-ack bookkeeping tick
```

This works because the queue tracks both pending and in-flight messages,
and `join` only completes when both are zero. The 50 ms sleep gives the
worker time to update its post-ack counters before the test reads
`/health/queue`.

Without this pattern, integration tests would either be flaky (`sleep
1`) or rely on internal state inspection (brittle).

## 4.6 Tests by category

### Queue semantics — [`tests/unit/test_queue.py`](../tests/unit/test_queue.py) (5 tests)

| Test | What it pins |
| ---- | ------------ |
| `test_send_receive_roundtrip` | Basic happy path, `receive_count = 1` after receive |
| `test_receive_increments_receive_count_on_retry` | NACK preserves message identity, increments counter |
| `test_delayed_send_respects_visibility` | `delay=0.2s` actually takes ~0.2s to be visible |
| `test_join_blocks_until_acked` | `join` doesn't return while a message is in-flight |
| `test_join_times_out_when_unacked` | `join(timeout=)` returns `False` instead of hanging |

The queue is the foundation; if these are wrong, every higher-level
test gives a false signal.

### Worker behavior — [`tests/unit/test_worker.py`](../tests/unit/test_worker.py) (5 tests, grows to 6 in phase 6)

| Test | What it pins |
| ---- | ------------ |
| `test_worker_persists_to_store_and_search` | Happy path: events land in both stores |
| `test_worker_dedupes_repeated_event_ids` | Same `event_id` twice → 1 persisted + 1 duplicate counter |
| `test_worker_retries_then_dead_letters_on_store_failure` | Mongo always fails → retries up to max, then DLQ |
| `test_worker_acks_when_es_fails_but_mongo_succeeds` | **The key tradeoff in test form.** ES failure does NOT re-queue |
| `test_worker_dead_letters_invalid_payloads_immediately` | Poison payload → DLQ on first attempt, not after retries |

The fourth test is the one to read carefully: it's the executable
proof of "Mongo is source of truth, ES is rebuildable."

### Validation — [`tests/unit/test_validation.py`](../tests/unit/test_validation.py) (5 tests)

| Test | What it pins |
| ---- | ------------ |
| `test_event_in_normalizes_event_type_case` | `"PageView"` → `"pageview"` |
| `test_event_in_rejects_extra_fields` | `extra="forbid"` is on |
| `test_event_in_rejects_bad_url` | URL validator actually validates |
| `test_event_in_caps_metadata_keys` | 100-key metadata → ValidationError |
| `test_stored_event_from_input_assigns_timestamp_and_id` | Enrichment defaults are non-empty |

### Cache — [`tests/unit/test_cache.py`](../tests/unit/test_cache.py) (5 tests, grows to 6 in phase 6)

| Test | What it pins |
| ---- | ------------ |
| `test_set_then_get_roundtrip` | Basic JSON roundtrip via fakeredis |
| `test_get_returns_none_for_missing_key` | Negative path |
| `test_get_or_compute_runs_once_under_concurrency` | **Single-flight under 10 concurrent callers** |
| `test_get_or_compute_reports_cache_hit_after_warm` | `cache_hit` flag toggles correctly |
| `test_invalidate_clears_window` | Manual invalidation works (used by tests, not prod) |

The third test is the one that matters: it spawns 10 concurrent
`get_or_compute` calls with a 50 ms sleep inside `compute`, and
asserts that `compute` is called exactly **once**. That's the
single-flight guarantee in executable form.

### Aggregation — [`tests/unit/test_aggregation.py`](../tests/unit/test_aggregation.py) (3 tests)

| Test | What it pins |
| ---- | ------------ |
| `test_stats_buckets_by_hour` | Hourly bucketing math against handcrafted timestamps |
| `test_stats_excludes_events_outside_window` | Half-open window math |
| `test_realtime_counts_groups_by_type` | Realtime aggregation matches stats aggregation shape |

Not a Mongo test — a math test. The Mongo-specific aggregation pipeline
syntax (`$dateTrunc`, `$match`, `$group`) is exercised in phase 5.

### Integration — [`tests/integration/test_ingest_to_query.py`](../tests/integration/test_ingest_to_query.py) (4 tests)

| Test | What it pins |
| ---- | ------------ |
| `test_ingest_then_list_returns_event` | The whole HTTP→queue→worker→GET round-trip |
| `test_ingest_then_stats_aggregates_count` | Same as above but for the aggregation endpoint |
| `test_ingest_rejects_invalid_payload` | 422 on bad URL |
| `test_stats_requires_window` | 422 when start/end missing |

### Integration — [`tests/integration/test_search_and_realtime.py`](../tests/integration/test_search_and_realtime.py) (3 tests)

| Test | What it pins |
| ---- | ------------ |
| `test_ingest_then_full_text_search` | ES path round-trip (against fake ES) |
| `test_realtime_stats_caches_subsequent_calls` | Cache miss → hit transition |
| `test_queue_health_reports_processed_events` | The `/health/queue` observability surface |

The design brief asks for "at least two full request lifecycles."
We deliver seven.

## 4.7 What this phase deliberately doesn't cover

Three categories of test we **did not** write, with the rationale:

### 1. Mongo-specific syntax

The `$dateTrunc` pipeline, the `BulkWriteError` handling, the
`event_id` unique index — none of these are exercised by the
fakes-based tests because **the fakes don't speak Mongo**. They
implement the same protocol, but the Mongo-isms are implementation
details.

**Why not write contract tests against testcontainers-style Mongo?**
Because phase 5 (manual QA) is a higher-leverage way to find those
bugs. A testcontainer's Mongo is itself a model of real Mongo;
manual QA against the actual docker image catches bugs both the
fake **and** the testcontainer would miss.

In phase 6 we end up adding mock-based unit tests for the
`BulkWriteError` handling, but those are regression tests for bugs
phase 5 found — not "tests we should have written first."

### 2. ES mapping inference + query syntax

Same reasoning. The fake ES does substring matching; the real ES
does inverted-index search. Most of the time the difference
doesn't matter for our business logic — until phase 5 found that
the real ES blows up on a query the fake handles fine.

### 3. Cross-replica behavior

Our rate limiter is in-process. Our cache single-flight is
in-process. These would behave differently across N replicas — but
testing that requires multi-process test infrastructure that's not
worth the cost at this milestone. The single-replica behavior is
asserted; the multi-replica change path is documented in
`ARCHITECTURE.md` §6 + §12.

## 4.8 What phase 4 explicitly tells phase 5

The honest output of phase 4 is: *"we have high confidence in the
business logic. We have **zero direct evidence** that the production
Mongo / ES / Redis paths work."* Phase 5 is what closes that gap.

This is not a hedge. It's a deliberate split — fast tests for
behavior, slow QA for integration. Conflating them is what makes
test suites take 20 minutes to run.

## 4.9 The test command surface

```bash
pytest                       # full suite
pytest tests/unit -q         # fast feedback
pytest -m integration -q     # lifecycle tests
pytest -k worker             # filter by name
```

Configured in [`pyproject.toml`](../pyproject.toml):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-ra --strict-markers"
markers = [
    "integration: end-to-end tests that exercise the full ingest pipeline",
]
```

`asyncio_mode = "auto"` is the win — every `async def test_*` is
automatically run on the event loop, no per-test decorator.

## 4.10 State at end of phase 4

```
$ pytest -q
..............................                       [100%]
30 passed in 1.80s
```

Sub-2-second feedback. Every commit can run it. CI doesn't need to
boot 3 containers to get a signal.

The trap is now obvious: **30 green tests don't mean the production
paths work**. Phase 5 takes the trap seriously.

## 4.11 Cross-references

- Test layout in operation: [`README.md` → Testing](../README.md#testing).
- Storage seams that make in-memory testing possible:
  [`phase_2_scaffolding_technical.md` §2.5](phase_2_scaffolding_technical.md).
- Bugs that the phase 4 suite couldn't find: phase 5 docs.

---

_Phase 5 in progress..._
