# Phase 6 — Fixes and regression hardening (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, and
> future maintainers of this service.
> **Stakeholder counterpart:** [`phase_6_fixes_and_hardening_stakeholder.md`](phase_6_fixes_and_hardening_stakeholder.md).
> **Previous phase:** [`phase_5_manual_qa_technical.md`](phase_5_manual_qa_technical.md).
>
> Phase 5 surfaced six bugs and one design discovery. This phase
> translates each finding into (a) a code fix and (b) an automated
> test that prevents regression. The test count grows from 30 to 48.
> `ARCHITECTURE.md` gets a "findings from manual QA" appendix.

---

## 6.1 What changed since phase 5

Phase 5 produced a punch list. Phase 6 burns it down. The deliverable
of this phase is:

- 6 code fixes, scoped to the minimal change.
- 18 new tests (mostly unit, mostly at the Mongo / ES boundary
  using mocks) that lock in the correct behavior.
- An appendix in `ARCHITECTURE.md` that catalogs every finding +
  fix + new test, so reviewers (ARB, peers) can audit the work.
- A re-verification pass against the real stack to confirm the
  fixes don't regress anything.

End-state: 48 tests green, real-stack QA green, design doc updated.

## 6.2 Fix policy

Two non-negotiables for every fix in this phase:

1. **Minimal scope.** No fix touches code that isn't strictly
   necessary for that finding. Tempting refactors get deferred to
   phase 7.
2. **Test before merge.** Every fix lands with a regression test
   in the same change. The test should fail against the pre-fix
   code and pass against the fixed code.

This isn't ceremony. The whole reason these bugs slipped past the
phase 4 suite is that the suite didn't test them. Locking in
regression coverage **is** the fix; the code change alone isn't
enough.

## 6.3 Fix #1 — Naive timestamps from Mongo

**Bug:** [`MongoEventStore._from_doc`](../app/storage/mongo.py) was
the identity function; Motor returns BSON dates as tz-naive
datetimes; FastAPI / Pydantic serialize naive datetimes as
ISO strings with no offset.

**Fix:**

```python
_DATETIME_FIELDS = ("timestamp", "ingested_at", "bucket_start")

@staticmethod
def _from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    for key in _DATETIME_FIELDS:
        v = doc.get(key)
        if isinstance(v, datetime) and v.tzinfo is None:
            doc[key] = v.replace(tzinfo=timezone.utc)
    return doc
```

Applied at every read path: `find`, `stats`, and the realtime
aggregation. Aware datetimes pass through untouched (no double-tag).

**Regression test** —
[`tests/unit/test_mongo_store.py`](../tests/unit/test_mongo_store.py):

- `test_from_doc_tags_naive_datetimes_as_utc`
- `test_from_doc_leaves_aware_datetimes_alone`

Pure functions, no mocking needed.

## 6.4 Fix #2 — Week bucket start day

**Bug:** Mongo's `$dateTrunc` defaults `startOfWeek` to Sunday.

**Fix:** pin Monday explicitly when the unit is `week`:

```python
unit = _BUCKET_TO_MONGO_UNIT[bucket]
date_trunc: dict[str, Any] = {"date": "$timestamp", "unit": unit}
if unit == "week":
    date_trunc["startOfWeek"] = "monday"
```

`hour` and `day` don't accept `startOfWeek`, so the condition is
necessary — passing it to those units would make Mongo reject the
pipeline.

**Regression tests** —
[`tests/unit/test_stats_pipeline.py`](../tests/unit/test_stats_pipeline.py):

- `test_stats_pipeline_pins_startofweek_for_week_bucket`
- `test_stats_pipeline_omits_startofweek_for_hour_and_day`
- `test_stats_pipeline_uses_half_open_window` (bonus — pins the
  `$gte` / `$lt` shape since we were touching the pipeline anyway)

These use a small mock collection that captures the pipeline passed
to `aggregate`. We assert against the pipeline structure, not the
execution result.

## 6.5 Fix #3 — ES blows up on numeric metadata

**Bug:** `simple_query_string` over `metadata.*` throws when the
search term can't be coerced to a field's mapped type.

**Fix:** `lenient: true` on the query body + correct default fields:

```python
must.append({
    "simple_query_string": {
        "query": q.q,
        "fields": q.fields,
        "default_operator": "and",
        "lenient": True,   # skip incompatible field types
    }
})

# Default fields now include source_url.text, not source_url:
DEFAULT_SEARCH_FIELDS = ["metadata.*", "source_url.text"]
```

The default-fields change is a related fix: the original default
was `source_url` (the parent keyword field), which only matched on
exact URLs. The `text` subfield is what enables partial matches.

**Regression tests** —
[`tests/unit/test_es_query.py`](../tests/unit/test_es_query.py):

- `test_search_query_sets_lenient_true`
- `test_default_search_fields_include_text_subfield`
- `test_search_query_passes_through_filters`
- `test_search_with_empty_query_matches_all`

A mock ES client captures the query body. We assert on shape, not
on execution result. (The actual ES behavior is exercised in the
phase 5 re-QA.)

## 6.6 Fix #4 — Cache encoder format inconsistency

**Bug:** [`app/cache/realtime.py`](../app/cache/realtime.py)'s
`_json_default` used `o.isoformat()`, which emits `+00:00` for UTC
datetimes. FastAPI's serializer emits `Z`.

**Fix:**

```python
def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        # Match Pydantic 2's UTC convention so cached and freshly-
        # computed responses serialize identically.
        return o.isoformat().replace("+00:00", "Z")
    raise TypeError(...)
```

**Regression test** —
[`tests/unit/test_cache.py`](../tests/unit/test_cache.py):

- `test_datetimes_in_cached_payload_serialize_with_z_suffix`

Round-trips a payload with `generated_at: datetime(..., tz=UTC)`
through the cache and asserts the read-back string ends with `Z`
and contains no `+00:00`.

## 6.7 Fix #5 — Worker hangs on paused Mongo (`socketTimeoutMS`)

**Bug:** `serverSelectionTimeoutMS` only fires when pymongo needs
to find a server. An established connection that stops responding
has no upper bound on socket reads. The worker blocked indefinitely.

**Fix:** add `socketTimeoutMS` to the client construction in
[`app/lifecycle.py`](../app/lifecycle.py):

```python
mongo_client = AsyncIOMotorClient(
    settings.mongo_url,
    uuidRepresentation="standard",
    serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
    socketTimeoutMS=settings.mongo_socket_timeout_ms,
)
```

Plus two new settings in [`app/config.py`](../app/config.py):

```python
mongo_server_selection_timeout_ms: int = Field(default=3000, ge=500, le=60_000)
mongo_socket_timeout_ms: int = Field(default=10_000, ge=1000, le=120_000)
```

Defaults: 3s server selection, 10s socket read. Both configurable.

**No regression test in this phase.** Reason: the test would
require mocking Motor's connection pool internals, which is
brittle (it'd test the pymongo internals more than our code). The
real-stack QA in §6.10 verifies the behavior end-to-end. Phase
7's "what I'd do differently" entry notes that a long-running
contract test against a paused testcontainer would close this
gap.

## 6.8 Fix #6 — Silent data loss on `NetworkTimeout` (the big one)

**Bug:** `insert_many`'s catch-all interpreted any exception
without `writeErrors` as "all duplicates absorbed" and returned
success. `NetworkTimeout` qualifies; data was lost silently.

**Fix:**

```python
try:
    result = await self._collection.insert_many(docs, ordered=False)
    return len(result.inserted_ids)
except BulkWriteError as exc:                # narrow scope
    details = exc.details or {}
    write_errors = details.get("writeErrors", [])
    duplicates = sum(1 for e in write_errors if e.get("code") == 11000)
    if write_errors and duplicates == len(write_errors):  # require non-empty
        logger.info("mongo.duplicate_writes_absorbed", ...)
        return len(docs) - duplicates
    raise
```

Two changes:

1. `except BulkWriteError` — only the bulk error subtype goes
   through this branch. `NetworkTimeout`, `ConnectionFailure`,
   etc. propagate to the worker, which retries / DLQs.
2. `if write_errors and duplicates == len(write_errors)` — require
   `write_errors` to be non-empty before treating "all duplicates"
   as success. The `0 == 0` trap is closed.

**Regression tests** —
[`tests/unit/test_mongo_store.py`](../tests/unit/test_mongo_store.py):

- `test_insert_many_propagates_network_timeout` (**the critical
  one**) — mocks the collection to raise `NetworkTimeout`, asserts
  the exception propagates. Pre-fix code would silently absorb it.
- `test_insert_many_absorbs_only_duplicate_bulk_errors` — pure
  `BulkWriteError` with all 11000 codes returns the correct count.
- `test_insert_many_reraises_when_bulk_has_non_duplicate_errors` —
  mixed `BulkWriteError` propagates.
- `test_insert_many_returns_inserted_count_on_clean_write` — happy
  path.
- `test_insert_many_no_op_on_empty_batch` — defensive null case.

These are the most important tests added in the entire project.
They are the executable proof that the silent-data-loss bug stays
fixed.

## 6.9 Worker: poison-pill mixed-batch test

While writing the regression tests for #6, I noticed a test gap
unrelated to phase 5's findings: the worker's behavior with a
batch containing **both valid and invalid payloads** wasn't
explicitly tested. The pre-existing
`test_worker_dead_letters_invalid_payloads_immediately` covered an
all-invalid batch.

**New test** in
[`tests/unit/test_worker.py`](../tests/unit/test_worker.py):

- `test_worker_handles_mixed_valid_and_invalid_payloads_in_batch`

Sends 3 messages — `[valid, invalid, valid]`. Asserts:
- `processed_count == 2`
- `dead_letter_count == 1`
- 2 docs in store, 1 in DLQ

This pins the design intent: **a poison pill doesn't block its
batch siblings**.

## 6.10 Rate limiter tests

Phase 4's suite covered the rate limiter only through curl in
phase 5. Phase 6 adds direct unit tests:

[`tests/unit/test_rate_limit.py`](../tests/unit/test_rate_limit.py):

- `test_limit_returns_429_with_retry_after_header` — drives the
  middleware through `httpx.ASGITransport`, exhausts the budget,
  asserts on the 429 + `Retry-After` header.
- `test_health_bypasses_rate_limit` — exhausts a different path,
  then hammers `/health` and asserts all succeed.

Pinning these mattered because the rate limit is the kind of
middleware that's easy to accidentally bypass or break in a
refactor — a regression would be visible only in production load.

## 6.11 Re-verification against the real stack

After all six fixes land + tests are green, I re-run the phase 5
QA against the real stack. The same scenarios that surfaced the
bugs should now pass:

- `POST /events` + `GET /events` — timestamps return with `Z`
  suffix.
- `GET /events/stats?bucket=week` — week boundaries are Mondays
  (verified via UTC parsing, not local time).
- `GET /events/search?q=feathr` against the index with numeric
  `metadata.amount` — returns 200, not 500.
- Realtime cache hit/miss — `generated_at` is byte-identical
  across paths.
- `docker pause feathr-mongo` — worker fails (failed=1 at t=15s),
  retries (failed=2 at t=30s), DLQs (dead_lettered=1 at t=35s).
  Mongo confirms zero docs persisted.
- `RATE_LIMIT_PER_MINUTE=10` — 25 rapid requests yield 10 × 200,
  15 × 429.

Zero regressions. All six fixes hold.

## 6.12 `ARCHITECTURE.md` updates

The architecture doc gets two updates in this phase:

1. **§8 (Failure modes)** gains a new subsection on the
   phantom-write scenario, with the two invariants that absorb
   it (unique index + commit-then-dedupe).
2. **A new appendix** at the bottom: a six-row table of finding ↔
   fix ↔ test. This is what lets reviewers audit the QA work
   without reading the docs/ folder.

The appendix is deliberately at the bottom so it doesn't disrupt
the doc's narrative flow but is searchable for anyone interested
in "what bugs did manual QA actually find?"

## 6.13 Test count: 30 → 48

| Phase 4 baseline | + Phase 6 additions | New total |
| ---------------- | ------------------- | --------- |
| 5 queue tests | (no change) | 5 |
| 5 worker tests | +1 (mixed batch) | 6 |
| 5 validation | (no change) | 5 |
| 5 cache | +1 (Z suffix) | 6 |
| 3 aggregation | (no change) | 3 |
| 4 integration ingest→query | (no change) | 4 |
| 3 integration search/realtime | (no change) | 3 |
| — | **+7 mongo_store** | 7 |
| — | **+4 es_query** | 4 |
| — | **+3 stats_pipeline** | 3 |
| — | **+2 rate_limit** | 2 |
| **30** | **+18** | **48** |

```
$ pytest -q
................................................                  [100%]
48 passed in 1.98s
```

Same suite, 60% more coverage, still under 2 seconds.

### Follow-up: coverage hardening sprint (post-release)

After phase 6 closed at 48 tests, a follow-up sprint pushed
coverage further to lock in the storage layers. The sprint added:

| Area | Additions | Coverage delta |
| ---- | --------- | -------------- |
| Mongo store (`find`, `realtime_counts`, `ensure_ready`, `stats` tz coercion, `close`) | 14 mock-cursor tests | 65% → **100%** |
| ES repo lifecycle (`ensure_ready` two paths, `index_many` four paths, `close`) | 9 mock-bulk tests | 73% → **100%** |
| `_BoundedSet` dedupe LRU, worker `start`/`stop` idempotency | 5 tests | worker 94% → 98% |
| Cache JSON-corruption fallback, `_json_default` round-trips, `invalidate(None)` | 7 tests | cache 88% → **100%** |
| `_validate_window` helper (3 branches) | 4 tests | queries 93% → 98% |
| Fakes filter predicates (all 5 per store + lifecycle no-ops) | 17 tests | fakes 83% → **100%** |
| `create_app`, middleware install, `/health` body, global 500 handler | 4 tests | main 0% → **100%** |

Final state: **108 tests, 94% statement coverage**, runtime still
under 3 seconds. 23 of 25 source files at 100%; the remaining gap
is `lifecycle.py` lifespan and `logging_config.py`, which require
real Motor / AsyncElasticsearch / Redis connections to exercise
meaningfully.

## 6.14 The lesson

Phase 5 found six bugs. Phase 6's value is **converting findings
into permanent coverage**. None of those bugs can come back without
a test catching them.

This is what "principled regression discipline" looks like in
practice: every QA finding becomes a test, every test gives the
team confidence to change adjacent code without re-running the
same QA from scratch.

## 6.15 Cross-references

- `ARCHITECTURE.md` appendix: [`ARCHITECTURE.md` → Appendix](../ARCHITECTURE.md).
- The phase 6 test files: [`tests/unit/test_mongo_store.py`](../tests/unit/test_mongo_store.py), [`tests/unit/test_es_query.py`](../tests/unit/test_es_query.py), [`tests/unit/test_stats_pipeline.py`](../tests/unit/test_stats_pipeline.py), [`tests/unit/test_rate_limit.py`](../tests/unit/test_rate_limit.py).
- The coverage-sprint files: [`tests/unit/test_es_repo.py`](../tests/unit/test_es_repo.py), [`tests/unit/test_fakes.py`](../tests/unit/test_fakes.py), [`tests/unit/test_misc_coverage.py`](../tests/unit/test_misc_coverage.py), [`tests/unit/test_app_factory.py`](../tests/unit/test_app_factory.py).
- Coverage breakdown lives in the top-level [`README.md` → Testing](../README.md#testing).
- Phantom-write scenario explained: [`ARCHITECTURE.md` §8](../ARCHITECTURE.md).

---

_Phase 7 in progress..._
