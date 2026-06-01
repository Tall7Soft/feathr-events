# Phase 5 — Manual QA against the real stack (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, and
> future maintainers of this service.
> **Stakeholder counterpart:** [`phase_5_manual_qa_stakeholder.md`](phase_5_manual_qa_stakeholder.md).
> **Previous phase:** [`phase_4_testing_technical.md`](phase_4_testing_technical.md).
>
> The 48-test suite is green. That's necessary but not sufficient. This
> phase stands the system up against **real Mongo, real Elasticsearch,
> real Redis** in docker-compose and probes it with curl. Six bugs
> surface that the fakes-based tests could not have caught — including
> one that silently loses data. This is the highest-leverage phase in
> the entire project.

---

## 5.1 What changed since phase 4

Phase 4 closed with: *"high confidence in business logic, zero direct
evidence the production paths work."* Phase 5 closes that gap.

What I do in this phase:

1. Install Docker on the host (with a real-world detour into BIOS).
2. Stand up the full stack via `docker compose up`.
3. Drive every endpoint with curl, asserting on real responses.
4. Pause Mongo mid-flight to verify the worker's retry / DLQ path
   against a real driver.
5. Stress the rate limiter.
6. Document **every bug found**, even the cosmetic ones.

End-state: six bugs catalogued, the stack proven against real
infrastructure, phase 6 has a concrete punch list.

## 5.2 The Docker setup detour

I install Docker Desktop via `winget`:

```
winget install --id Docker.DockerDesktop -e --silent
```

But it doesn't start. The engine reports:

```
WSL2 cannot start because virtualization is not enabled on this machine.
Make sure the "Virtual Machine Platform" optional component is enabled
and that virtualization is activated in the device's firmware settings.
```

This is a **BIOS-level setting**, not an OS-level one. The host's CPU
virtualization extensions (Intel VT-x / AMD-V) are disabled in
firmware. No amount of `wsl --install` will fix it.

**The fix:** reboot the machine, enter UEFI, enable virtualization,
reboot again.

**Why this is worth documenting:** real-world dev environment friction
is part of the work. The platform engineer on point doesn't get to wave away the
host setup; they have to debug it. The two-minute version is *"check
BIOS, enable virt, reboot."*

After the reboot, Docker Desktop comes up cleanly.

## 5.3 Standing up the stack

```bash
docker compose up -d --build
```

After ~2 minutes (image pulls + ES boot), all four services are
healthy:

```
NAME           STATUS                  PORTS
feathr-app     Up (healthy)            0.0.0.0:8000->8000/tcp
feathr-es      Up (healthy)            0.0.0.0:9200->9200/tcp
feathr-mongo   Up (healthy)            0.0.0.0:27017->27017/tcp
feathr-redis   Up (healthy)            0.0.0.0:6379->6379/tcp
```

Healthchecks come from [`docker-compose.yml`](../docker-compose.yml).
Notably, the `app` service waits on `condition: service_healthy` for
all three dependencies — the app doesn't start until Mongo and ES
are actually ready, not just listening.

## 5.4 QA methodology

For reference, the FastAPI app serves an auto-generated Swagger UI at
`/docs` once the stack is up — this is the surface I am probing:

![Swagger UI showing all seven endpoints organized by tag](images/swagger_docs.png)

> **Note on reproducibility.** The phase 5 QA captured here is a
> hand-driven first pass — curl invocations recorded verbatim in
> this journal. The same coverage was later packaged as a Postman
> collection (`postman_collection.json` at the repo root) so any
> future engineer can replay the same surface verification in
> five minutes without re-deriving the request shapes. The bugs
> phase 5 found are now closed by the regression tests added in
> phase 6; the Postman collection is the "is the contract still
> intact?" check that runs on every deploy. See phase 8 §8.7.

I drive curl tests in roughly this order, one feature at a time:

1. `/health` + `/health/queue` — sanity check.
2. `POST /events` — happy path + 6 validation cases.
3. `GET /events` — filters, paging, sort.
4. `GET /events/stats` — buckets, half-open window.
5. `GET /events/search` — full-text via real ES.
6. `GET /events/stats/realtime` — cache miss/hit/TTL.
7. Worker retry/DLQ — by **pausing the Mongo container** while events
   are in-flight.
8. Rate limiter — by hammering with a tight limit.

For each step, I observe the response, log lines from the app
container, and where applicable inspect Mongo/ES/Redis directly.

## 5.5 Endpoint-by-endpoint findings

### `/health` and `/health/queue` — green

```
$ curl http://localhost:8000/health
{"status":"ok"}

$ curl http://localhost:8000/health/queue
{"queue_depth":0,"queue_in_flight":0,"dlq_depth":0,
 "processed":0,"failed":0,"dead_lettered":0,
 "duplicates_suppressed":0}
```

No surprises.

### `POST /events` — green on all 7 validation cases

| Case | Expected | Actual |
| ---- | -------- | ------ |
| Happy path | 202 + event_id | pass |
| `event_type: "PageView"` → normalized to `"pageview"` | 202 | pass |
| Explicit timestamp | 202 | pass |
| Missing `user_id` | 422 with Pydantic detail | pass |
| Invalid URL | 422 | pass |
| Extra field | 422 (`extra_forbidden`) | pass |
| Empty `event_type` | 422 (`string_too_short`) | pass |

### `GET /events` — green, but a finding

All filter combinations return the correct counts:

| Filter | Expected | Actual |
| ------ | -------- | ------ |
| All for `user_id=alice` | 3 | pass |
| Alice + `event_type=click` | 2 | pass |
| By `source_url=https://feathr.co/a` | 3 | pass |
| Date range May 18-23 | 2 | pass |

**Finding #1 (cosmetic, but real):** the returned `timestamp` and
`ingested_at` look like `"2026-05-20T10:00:00"` — **no timezone
suffix**. The wire format is naive. A consuming client (especially
non-Python) would have to guess the zone.

Root cause: Mongo's BSON `Date` strips tzinfo on the wire; Motor
surfaces the result naive. Pydantic / FastAPI's default serializer
then emits a naive ISO string.

> Filed for phase 6.

### `GET /events/stats` — green, with a subtle finding

Day, hour buckets work as expected. Week buckets surface **finding
#2**: Mongo's `$dateTrunc` defaults `startOfWeek` to **Sunday**.
That's inconsistent with the in-memory fake (Monday) and with ISO
8601 (Monday).

For a single deploy that's tolerable. For dashboards that span dev
and prod environments, it's a silent off-by-one-day discrepancy
between fake and real.

> Filed for phase 6: pin `startOfWeek: "monday"` explicitly.

### `GET /events/search` — **HARD FAILURE**

The first search call returns a 500:

```
$ curl 'http://localhost:8000/events/search?q=spring'
{"detail":"internal server error"}
```

App logs show:

```
elasticsearch.BadRequestError: BadRequestError(400,
  'search_phase_execution_exception',
  'failed to create query: For input string: "spring"')
```

**Root cause (finding #3):** I seeded a `metadata.amount: 99.99`
event in the test data. ES's dynamic mapping inferred `metadata.amount`
as `double`. The `simple_query_string` query then tries to coerce
the term `"spring"` against every matched field — including the
numeric one — and the parse fails.

This breaks **unrelated** searches: any string search across
`metadata.*` blows up if **any** prior partner sent a numeric field
into that index.

> Filed for phase 6: `lenient: true` on the query.

### `GET /events/stats/realtime` — green, with finding #4

Behavior is correct (miss → hit → TTL expiry → miss), but the
`generated_at` field round-trips with two different formats:

```
# First call (computed fresh, serialized by FastAPI/Pydantic):
"generated_at": "2026-05-31T17:55:02.503481Z"

# Second call (read from Redis cache, serialized by our json.dumps):
"generated_at": "2026-05-31T17:55:02.503481+00:00"
```

Both are valid ISO 8601 UTC representations, but they're not the
same string. Downstream parsers (especially Go / Java) trip on this.

> Filed for phase 6: rewrite `+00:00` → `Z` in our cache encoder.

### Worker retry / DLQ — **TWO HARD FAILURES IN A ROW**

This is the dramatic part of phase 5.

I want to verify the retry/DLQ path under a real Mongo outage. So I:

```bash
docker pause feathr-mongo   # freeze the Mongo process
# send 3 events
# wait
# observe counters
```

#### First attempt: nothing happens

After 16 seconds with Mongo paused, counters show:

```
{"queue_depth":0,"queue_in_flight":3,"dlq_depth":0,
 "processed":3,"failed":0,"dead_lettered":0}
```

`failed=0`. No retries. Worker is just **sitting there**.

Logs show no `mongo_insert_failed` entries.

**Root cause (finding #5):** `serverSelectionTimeoutMS` only fires
when pymongo needs to **find a server**. Once the client has a
healthy connection (which it does — Mongo was healthy when we
started), the connection stays in the pool. When Mongo is then
paused, the **socket read** hangs — and there's no socket timeout
configured. The default is no timeout.

The fix: also configure `socketTimeoutMS`, which bounds in-flight
socket reads regardless of server-selection state.

I add the setting, restart the app, retry the experiment.

#### Second attempt: counter says success, but Mongo is empty

After ~15 seconds with Mongo paused, counters show:

```
{"queue_depth":0,"queue_in_flight":0,"dlq_depth":0,
 "processed":4,"failed":0,"dead_lettered":0}
```

`processed=4`! The worker is reporting success. But the event was
sent while Mongo was paused, so it can't have been persisted —
unless something weird is happening.

I check Mongo directly:

```bash
$ docker exec feathr-mongo mongosh events --quiet \
    --eval 'db.events.countDocuments({event_type: "isolated_retry"})'
0
```

**The event isn't in Mongo. But the worker thinks it persisted it.**

This is **finding #6**, and it's the most consequential bug in the
entire project. It would silently lose data in production.

**Root cause:** the original `insert_many` wrapper was this:

```python
try:
    result = await self._collection.insert_many(docs, ordered=False)
    return len(result.inserted_ids)
except Exception as exc:
    details = getattr(exc, "details", None) or {}
    write_errors = details.get("writeErrors", [])
    duplicates = sum(1 for e in write_errors if e.get("code") == 11000)
    if duplicates == len(write_errors):     # ← the bug
        return len(docs) - duplicates
    raise
```

When `NetworkTimeout` fires:
- `getattr(exc, "details", None) or {}` → `{}`
- `write_errors = [].get("writeErrors", [])` → `[]`
- `duplicates = 0`
- `if 0 == 0:` → **True**
- Returns "0 duplicates, len(docs) docs inserted" → reports success

The catch-all was too broad. It treated *any* exception without
`writeErrors` as "all duplicates absorbed." `NetworkTimeout` qualifies.

**The fix (committed in phase 6):**

```python
try:
    result = await self._collection.insert_many(docs, ordered=False)
    return len(result.inserted_ids)
except BulkWriteError as exc:           # narrow to bulk errors only
    details = exc.details or {}
    write_errors = details.get("writeErrors", [])
    duplicates = sum(1 for e in write_errors if e.get("code") == 11000)
    if write_errors and duplicates == len(write_errors):  # require non-empty
        return len(docs) - duplicates
    raise
# NetworkTimeout, ConnectionFailure, etc. now propagate to the worker,
# which retries with backoff and eventually DLQs.
```

After the fix, the same experiment shows:

```
[t=15s] processed=0 failed=1 in_flight=1 dlq=0
[t=30s] processed=0 failed=2 in_flight=1 dlq=0
[t=35s] processed=0 failed=3 in_flight=0 dlq=1 dead_lettered=1
```

Three retries, then DLQ. Exactly what was promised.

### Rate limiter — green

With `RATE_LIMIT_PER_MINUTE=10`, 25 rapid requests yield 10 × 200
and 15 × 429. `/health` is exempt (5 / 5 succeed). The 429 response
carries `Retry-After: 5`.

## 5.6 A bonus finding: the phantom-write scenario

While I'm running the retry experiment with `docker unpause` at the
end, an interesting thing happens. The unpause is at t=40s, after
the DLQ entry was created. A few seconds after the unpause, I check
Mongo:

```bash
$ docker exec feathr-mongo mongosh events --quiet \
    --eval 'db.events.countDocuments({event_type: "retry_v4"})'
1
```

**The event is in Mongo AND in the DLQ.**

What happened: while Mongo was paused, the BSON write was sitting in
Mongo's TCP receive buffer. When the worker's socket timed out, it
gave up — but Mongo never saw the timeout. When `docker unpause`
ran, Mongo processed the buffered request and persisted the document.
The client never got the response.

This is the classic **at-least-once write phantom**. The system
tolerates it without corruption because of two invariants:

- `event_id` has a **unique index** in Mongo, so a DLQ re-drive
  would hit `BulkWriteError(11000)` which my fixed wrapper now
  handles correctly.
- The worker's `_BoundedSet` dedupe is only updated **after** a
  confirmed commit, so retries-after-failure are not falsely
  suppressed.

I file this for the architecture doc — it's a real failure mode
that's worth being explicit about.

## 5.7 Summary of findings

| # | Finding | Severity |
| - | ------- | -------- |
| 1 | Timestamps return naive (no `Z` / no offset) | Medium — cross-language consumer pain |
| 2 | Mongo `$dateTrunc` weekly bucket defaults to Sunday | Low — silent inconsistency between fake and real |
| 3 | ES `simple_query_string` blows up on numeric metadata fields | High — search hard-failure on common data |
| 4 | Cache encoder emits `+00:00`, FastAPI emits `Z` | Low — cosmetic but trip parsers |
| 5 | Worker hangs on paused Mongo (missing `socketTimeoutMS`) | High — backs up the ingest pipeline indefinitely |
| 6 | `insert_many` reports success on `NetworkTimeout` (silent data loss) | **Critical** — the system silently loses data |
| (bonus) | Phantom-write under at-least-once delivery | Documented; design already absorbs it |

Six bugs. Zero of them would have been caught by the phase 4 suite.
Five of the six are visible only against real infrastructure.

## 5.8 The lesson

The phase 4 test suite is green. The system "works." A junior
engineer would have stopped here.

The phase 5 work is **the difference between a system that passes
review and a system that survives production**. Two of the six
findings would have caused real damage:

- The silent data-loss bug (#6) would have lost events under any
  Mongo blip in production. The counters would say "all good."
- The frozen-worker bug (#5) would have backed up the ingest
  pipeline for the duration of any Mongo network partition.

The platform team's standards say it explicitly: *we ship on how
we think about systems, not just on whether the code runs*. This
phase is what that looks like in practice.

## 5.9 Hand-off to phase 6

Phase 6 takes the punch list from §5.7 and:

- Fixes each finding in code.
- Writes a regression test for each — using mocks at the Mongo /
  ES boundary now that we know the bugs exist.
- Updates `ARCHITECTURE.md` with the timeout decisions and the
  phantom-write scenario.

Test count goes from 30 to 48.

## 5.10 Cross-references

- The bug catalogue in production form: [`ARCHITECTURE.md` → Appendix](../ARCHITECTURE.md).
- Phantom-write scenario explained: [`ARCHITECTURE.md` §8](../ARCHITECTURE.md).
- Mongo timeout settings: [`app/config.py`](../app/config.py), [`app/lifecycle.py`](../app/lifecycle.py).

---

_Phase 6 in progress..._
