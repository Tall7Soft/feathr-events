# Phase 8 — Code review, ARB, release, and hand-off (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, Ops /
> SRE picking up the service, and future maintainers.
> **Stakeholder counterpart:** [`phase_8_submission_stakeholder.md`](phase_8_submission_stakeholder.md).
> **Previous phase:** [`phase_7_final_documentation_technical.md`](phase_7_final_documentation_technical.md).
>
> The repo is feature-complete, tested, and documented. Phase 8 is the
> last mile: a clean PR, peer review, ARB sign-off, an ops readiness
> review, merge, release tag, and hand-off to the team that will run
> it in production. This phase is deliberately mechanical — at this
> stage, **not making a mistake** is the whole job.

---

## 8.1 What changed since phase 7

Nothing in the code. Nothing in the docs. Phase 8's deliverable is
**the hand-over** — taking the repo from "ready" to "released and
running, owned by ops, supported by us for the bake-in period."

The temptation at this stage is "one more cleanup." Resist it. Every
change at this stage is a chance to introduce a bug into a system
that was working five minutes ago. The discipline is: **ship what
was tested**.

## 8.2 Pre-merge checklist

A short list, run in order. Each step is cheap; the cost of
skipping any of them is paid downstream.

### 8.2.1 Working tree is clean

```bash
git status   # no uncommitted changes
```

If anything's dirty, it's a sign I forgot to commit work from QA.
Investigate, don't just `git stash`.

### 8.2.2 The test suite is green

```bash
$ pytest -q
................................................                  [100%]
48 passed in 1.98s
```

Full suite. Not just the parts I think I changed. The whole suite.

### 8.2.3 No secrets in the repo

```bash
# Scan for accidentally committed credentials
grep -rE "(SECRET|TOKEN|PASSWORD|API_KEY)" --include="*.py" \
     --include="*.yml" --include="*.toml" --exclude-dir=.venv .

# Verify .env is gitignored, .env.example is not
cat .gitignore | grep -E "^\.env$"
ls .env.example
```

The only env-shaped file in the repo should be `.env.example` —
defaults, zero secrets. The repo-secrets check is the kind of
thing that's catastrophic to get wrong and trivial to verify.

### 8.2.4 No local-only state in the repo

Things that should **not** end up in the commit:

- `.venv/`
- `__pycache__/`
- `.pytest_cache/`
- `.ruff_cache/`
- Container volume mounts
- Anything from outside the `feathr-events/` directory

`.gitignore` covers the first batch. Anything outside
`feathr-events/` is naturally excluded.

### 8.2.5 The README quick-start works for someone with nothing installed

A mental run-through of the README from the perspective of an
engineer who just cloned the repo:

- `docker compose up --build` → does the build complete? Do all
  four containers reach healthy? Does `curl localhost:8000/health`
  return `{"status":"ok"}`?
- `pip install -r requirements-dev.txt && pytest` → does the suite
  run green without external dependencies?

This was already verified in phase 5/6, but it's worth running
once more right before submitting the PR. The most embarrassing
failure mode for any service hand-off is: **the next engineer
can't even start it**.

## 8.3 PR shape decision

Two options worth considering:

### Option A — Single PR with the full service
**Pros:** No history to defend. The reviewer reads the diff
top-to-bottom. The `docs/` journal already captures the
evolution; commits would duplicate it.
**Cons:** Big diff. Reviewers have to take the change in one
bite.

### Option B — Stacked PRs per phase
**Pros:** History mirrors the journal. Smaller diffs per PR.
**Cons:** Adds review overhead (each PR needs sign-off). Risk of
the stack going stale between reviews. For a service this size,
the overhead doesn't pay back.

Going with **option A**:

- The `docs/` journal in the repo already shows the evolution. Git
  history would be duplicative.
- A reconstructed-after-the-fact commit history is the worse kind
  of theater — it implies a development order that wasn't real.
- The diff is reviewable in a single sitting; ARB will discuss
  ARCHITECTURE.md, not line-by-line.

## 8.4 PR description

The PR description is the orientation for everyone who reads the
diff. It does four things:

1. State the business reason.
2. Point to the docs that explain the design.
3. Summarize the test/QA state.
4. Call out what's deliberately not in this PR (so reviewers
   don't ask).

Draft:

```
# Platform: Distributed Event Processing Platform

Replaces the current synchronous ingest pipeline ahead of the
per-tenant analytics dashboard launch next quarter. Supports the
volume profile sales has already quoted (~10x current per-tenant
cap) without backing up the producer side.

## What's in this PR
- Async ingestion pipeline (FastAPI + in-process SQS-shaped queue
  + worker pool)
- MongoDB as source of truth (events + aggregations); ES as
  rebuildable secondary index for full-text + ad-hoc analytics;
  Redis for realtime stats cache
- Token-bucket rate limiter, in-process dedupe + DLQ
- Docker compose for the full stack
- 48 unit + integration tests (pytest), ~2s

## How to review
- `ARCHITECTURE.md` carries the design rationale, failure modes,
  scaling analysis, and the post-QA findings appendix. Read this
  first — it's the load-bearing artifact for ARB.
- `README.md` — operating manual + AI in My Workflow.
- `docs/` — phased development journal. Optional context; skip if
  short on time.

## QA state
- Test suite green (`pytest -q` → 48 passed)
- Manual QA against real Mongo / ES / Redis via docker compose;
  six bugs found during that pass are documented + fixed +
  covered by regression tests. See ARCHITECTURE.md → Appendix.

## What's explicitly NOT in this PR
- Real SQS migration — the in-process queue is shaped for the
  swap; ARCHITECTURE.md §7 documents what changes.
- Multi-replica deployment — rate limiter + cache single-flight
  are in-process; migration path documented in §6 / §12.
- Performance benchmarks against real traffic — these belong to
  the ops readiness review, not the design review.

## Hand-off plan
- Ops takes over deployment after release tag.
- I'm on point for the bake-in week (one-week SLA on issues that
  surface in prod).
```

A few discipline notes:

- **Lead with the business reason.** Reviewers want to know why
  the diff exists before they look at it.
- **Be specific about what's in and what's out.** A list of "not
  in this PR" pre-empts the most common review nit ("why didn't
  you do X?").
- **Name the artifact that carries the weight.** `ARCHITECTURE.md`
  is the load-bearing doc. Say so.

## 8.5 ARB sign-off

ARB is the architecture review board (staff+ engineers from
across the company). For a platform service of this scope, the
sign-off bar is:

1. **The design is documented in a way that the next maintainer
   can understand it without re-deriving it from code.**
2. **The failure modes are explicit, including the ones we're
   accepting (e.g., ES lag during outages).**
3. **The scaling path is documented, not aspirational.**
4. **The "what I'd do differently" section is honest about the
   gaps.**

The phase 7 audit was specifically aimed at this bar. ARB asks
hard questions; the doc is what they probe.

Things they're likely to push on, with the answer pre-loaded in
the doc:

| Likely ARB question | Where the doc already answers it |
| ------------------- | -------------------------------- |
| "Why MongoDB, not Postgres + a JSONB column?" | §4 (storage rationale) |
| "Why not just write to ES and use it as the source of truth?" | §4 + §8 (failure modes) |
| "How do we get to 10× without a rewrite?" | §9 (scaling) |
| "Why this in-process queue and not real SQS now?" | §7 (queue + SQS comparison) |
| "What happens to in-flight messages during a deploy?" | §8 (failure modes — worker crash) |
| "Why is the cache invalidation TTL-only?" | §6 (caching strategy) |
| "How do you know the silent-data-loss bug is actually fixed?" | Appendix (findings) + tests/unit/test_mongo_store.py |

## 8.6 Ops readiness review

Separate from ARB, ops/SRE has its own bar for accepting a
service into the on-call rotation. The questions:

| Ops question | Where it's answered |
| ------------ | ------------------- |
| How do we monitor it? | `/health/queue` exposes counters; structured JSON logs go to stdout for our log pipeline |
| What are the operational tunables? | `.env.example` + README → Configuration |
| What runbook do we have for the 3 most likely failure modes? | ARCHITECTURE.md §8 |
| What's the DLQ replay process? | Documented gap — `ARCHITECTURE.md` §12 calls out "DLQ tooling" as a near-term follow-up |
| What's the dependency graph at startup? | docker-compose healthchecks + `app/lifecycle.py` |

The DLQ tooling gap is real and called out explicitly. We don't
hide it; we agree on a follow-up ticket with ops as part of the
hand-off.

## 8.7 The Postman collection as a reproducible verification layer

Shipping with the service is a Postman collection
(`postman_collection.json` at the repo root) covering all seven
endpoints plus a sequenced workflow folder. This is **not a
replacement** for the test suite or for the manual QA captured in
phase 5 — it sits in its own layer on the verification stack:

### The verification stack as it now stands

| Layer | Audience | Runtime | What it catches |
| ----- | -------- | ------- | --------------- |
| Unit tests (108) | Engineers, CI | ~2.5 s | Behavior changes in the seams between layers |
| Integration tests (7) | Engineers, CI | sub-second | Full HTTP → queue → worker → store control flow against in-memory fakes |
| Manual QA (phase 5) | The author, one-off | minutes | Bugs invisible to fakes-based tests; ran against the real Mongo / ES / Redis stack and surfaced six findings |
| **Postman collection** | Ops, customer engineers, future maintainers | minutes, repeatable | API surface verification post-deploy; onboarding new engineers; smoke tests after a config change |
| (Deferred) load + chaos | Ops, performance team | hours | p99 under steady load, behavior during partition |

The four manual layers (QA + Postman + load + chaos) progressively
trade depth for repeatability. Phase 5's QA was the deepest but
hardest to re-run from scratch. The Postman collection is the
**reproducible** version of that same QA — anyone can import the
JSON and get the same coverage in under five minutes.

### What the collection contains

- **35 requests** across 4 folders (Health, Ingestion, Queries,
  Workflow examples).
- **7 variables** so the collection works against local docker,
  staging, or production by editing one field.
- **Test scripts** on key responses (200 OK, 202 with `event_id`,
  201 cache-miss → cache-hit transition with byte-identical
  `generated_at`) — Postman runs the assertions automatically.
- **Pre-request scripts** where useful (the workflow demo stamps
  `new Date().toISOString()` so the demo event lands in the
  realtime window).
- **Variable capture** — the happy-path POST stores `event_id` for
  follow-up requests to reference.
- **Negative cases** — every 422 path the API can return
  (missing required fields, bad URL, extra fields, empty
  strings, oversized metadata).

### Why this is worth shipping (not just keeping in someone's local Postman)

A point each:

1. **Ops can re-run it on every deploy.** A two-minute smoke run
   answers "did this release break the API surface?" with no
   ambiguity. Replaces a hand-rolled curl script that drifts.
2. **Customer engineers onboard 4× faster.** The collection IS the
   API documentation in interactive form — open it, click around,
   see real responses. The `docs/images/swagger_docs.png` capture
   shows the same surface visually; Postman lets a reader probe it
   without standing up docker.
3. **It survives the human running the QA.** Phase 5's manual QA
   lives in my head and in the journal. The Postman collection
   lives in the repo and runs identically for anyone.
4. **Workflow demos = executable runbook.** The 6-step `Workflow
   examples` folder is "ingest one event, watch the worker
   process it, read it back four different ways" — exactly the
   thing the on-call engineer needs to validate a production
   incident.
5. **Diffs are reviewable.** When a future PR changes an endpoint,
   the collection is updated in the same change. Code review
   catches "did you also update the contract artifact?" without
   ceremony.
6. **It composes with the swagger UI.** The auto-generated
   `/docs` page (captured in `docs/images/swagger_docs.png`) is
   what a reader sees first; the Postman collection is what they
   reach for second, once they want to actually drive the system.

### What the collection is NOT

Honesty about scope:

- **Not a replacement for the test suite.** The Postman test
  scripts assert on shape, not on subtle behavior (e.g.,
  single-flight under concurrency, retry-after-failure
  semantics). Those stay in pytest.
- **Not a load tool.** Postman's "Run collection" iterates
  sequentially. For throughput testing we'd use k6 / Locust.
- **Not a contract-test replacement.** It pins example payloads,
  not schema enforcement. A consumer-driven contract test would
  live in a different repo and run on producer + consumer CI.

### Where it lives in the deliverable

- `postman_collection.json` — at the repo root, importable via
  Postman's `File → Import`.
- README's quick-start references it as the recommended path for
  someone who wants to drive the API end-to-end without writing
  curl.
- Phase 5 references it as the reproducible follow-up to the
  manual QA — the bugs it found are now regression-tested
  automatically, and the Postman flow exists so anyone can
  re-verify the surface without re-deriving the curl invocations.

## 8.8 Merge, tag, release

```bash
# After PR is approved + ARB signs off + ops accepts:
git checkout main
git pull
git merge --ff-only <pr-branch>     # squash already done at the PR
git tag -a v0.1.0 -m "Initial release: distributed event processing platform"
git push origin main --tags
```

A `v0.1.0` tag because this is the first production release.
Subsequent ops-driven changes get patch bumps; feature work that
extends the platform gets minor bumps.

## 8.9 What I'm NOT doing in this phase

- **No "polish pass" on the code.** The code is what was tested.
  Late changes invalidate the QA.
- **No additional features.** All 7 functional requirements + 5
  secondary items are done.
- **No "I just realized" rewrites of ARCHITECTURE.md.** It's been
  through phase 7's audit.
- **No back-channel messages to ARB asking for early signal.**
  The review process is the channel.

Each "no" is a small instance of the same discipline: **knowing
when to stop**.

## 8.10 Post-release: the bake-in week

The hand-off isn't done at merge. I stay on point for the first
week:

- Watch the queue depth + DLQ depth dashboards.
- On call for any production-surfacing issues.
- Available for ops questions during the first deploys.
- Document anything that surfaces post-launch as a phase 8.5
  entry in the journal (we don't add a fake phase; we update the
  doc honestly).

After the bake-in week, ownership transitions fully to ops. We
remain available for follow-up tickets but no longer carry the
pager.

## 8.11 The full timeline summary

| Phase | Output | Test count | Status |
| ----- | ------ | ---------- | ------ |
| 1 | Planning document | — | done |
| 2 | Repo scaffold + deps + Settings | — | done |
| 3 | Core implementation (queue, worker, stores, cache, API) | — | done |
| 4 | Test suite (unit + integration with fakes) | 30 | done |
| 5 | Manual QA against real stack — 6 bugs found | 30 | done |
| 6 | Fixes + regression coverage for each finding | 48 | done |
| 7 | README + ARCHITECTURE.md polish | 48 | done |
| 8 | Code review, ARB, release, hand-off + Postman collection | 48 | done |
| Post-release | Coverage hardening sprint (Mongo / ES / fakes / app factory tests) | **108 (94%)** | done |

Eight phases plus a post-release coverage pass. One sprint of
build, one of hardening. Two parallel documentation tracks plus
the Postman collection as a reproducible verification layer. The
service that gets handed over is the result of every one of them.

## 8.12 Cross-references

- The PR template that informed §8.4: internal team standard.
- ARB rubric: internal architecture team standard.
- Ops readiness checklist: internal SRE standard.
- The technical artifacts being handed over: [`../README.md`](../README.md), [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

---

_End of journal._
