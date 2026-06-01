# Phase 7 — Final documentation pass (technical)

> **Audience:** Platform Engineering teammates, ARB reviewers, and
> future maintainers of this service.
> **Stakeholder counterpart:** [`phase_7_final_documentation_stakeholder.md`](phase_7_final_documentation_stakeholder.md).
> **Previous phase:** [`phase_6_fixes_and_hardening_technical.md`](phase_6_fixes_and_hardening_technical.md).
>
> The code is done. The tests are green. Manual QA is green.
> Phase 7 is the final pass over the two artifacts that go into
> review: `README.md` and `ARCHITECTURE.md`. The bar is high — the
> platform team's design brief explicitly weights the architecture
> document on par with the code.

---

## 7.1 What changed since phase 6

Phase 6 added the findings appendix to `ARCHITECTURE.md`. Phase 7 is
a deliberate pass over **every section** of both top-level docs to
make sure each one stands on its own:

- The reader can land on any section, in any order, and not be lost.
- Every claim that depends on code is cross-referenced to a file
  path (or line).
- The tradeoffs identified in phase 1 are visible, not implicit.
- The "AI in My Workflow" section is honest — concrete examples,
  not marketing copy.

End-state: two documents an ARB reviewer (or a maintainer joining
the project six months from now) can read cold, in 20 minutes, and
emerge able to reason about the service without needing to read the
code first.

## 7.2 `ARCHITECTURE.md` — section-by-section audit

A discipline of this phase: read the doc as a first-time reader
would (a new teammate, an ARB reviewer, an SRE picking up the
service), and ask of each section, *"can someone reason about
this without opening the code?"* If the answer is no, the section
gets rewritten.

The check applied to each section:

| Section | Audit question | Outcome |
| ------- | --------------- | ------- |
| §1 System diagram | Does the ASCII diagram show the actual data flow, not an idealized one? | Includes retry/DLQ branch, cache miss path, ES indexing — production reality, not a sales sketch |
| §2 Component responsibilities | Does each layer have an "owns / does NOT own" line? | Yes — explicit boundaries; e.g. "API never writes to Mongo directly" |
| §3 Data flow | Can a reader trace one event from POST to query without ambiguity? | Walks the three lifecycles (ingest, worker, query) with the order made explicit |
| §4 Storage rationale | Does it justify the three-store split and explicitly explain why **not** collapse? | Yes — and includes the failure cases each collapse would produce |
| §5 Indexing strategy | Are the indexes I deliberately **didn't** add documented? | Yes — `source_url`, `metadata.*` with rationale |
| §6 Caching strategy | Does it cover TTL rationale, invalidation, **and** what changes at higher write volume? | Yes — three concrete options for high-volume (write-through-on-aggregate, Redis-side single-flight, pre-bucket HINCRBY) |
| §7 Queue + SQS comparison | Does it list specifically what would change for real SQS, not just "we'd use SQS"? | 5-item table + 5-item migration list |
| §8 Failure modes | One subsection per failure type? | Yes: Mongo, ES, Redis, worker crash, replica fail, DLQ overflow. Plus the phantom-write scenario from phase 5 |
| §9 Scaling | Does it list breakpoints in order, with the unexpected one at the end? | Yes — 5 internal bottlenecks + the producer-side answer |
| §10 SLOs and operational targets | Are latency / throughput / availability / RTO targets numeric and justified? | Yes — tables per dimension, with the load-bearing one (POST /events) called out |
| §11 Security and trust boundaries | Trust model, threat matrix, data sensitivity, sign-off items? | Yes — and explicit about what's NOT auth'd at v0.1 (customer-side ingestion) |
| §12 What I'd do differently | Is it specific, prioritized, and honest? | 8 items, ordered by payoff, including the test gap from phase 6 |
| Appendix (findings) | Does it tie back to the phase 5 manual QA? | 6-row table of finding ↔ fix ↔ severity |

### The single rewrite that mattered most

§8 (failure modes) originally said "ES is best-effort: log and ack."
That's correct but underspecified — it leaves the reader wondering
**"is this safe? what's the recovery path?"**

The phase 7 rewrite makes the implicit invariants explicit:

> ES is **best-effort**: on failure we log and ack the message anyway,
> because **Mongo is authoritative and ES is rebuildable**. Recovery
> is a one-shot reindex from Mongo (a job, not a per-request retry).
> This is the single most load-bearing design decision in the worker:
> it keeps ES outages from cascading into ingest backpressure.

Same content, but the **why** is now front-and-center. An ARB
reviewer who quotes that paragraph back at me in design review
has already pre-loaded the answer; we spend the meeting time on
the questions that *aren't* in the doc.

## 7.3 `README.md` — section-by-section audit

`README.md` has a different job from `ARCHITECTURE.md`. It's the
**operating manual**: how to run, how to use, how to extend.

Sections:

| Section | What it has to do | Outcome |
| ------- | ----------------- | ------- |
| Quick start | docker-compose path **and** local Python path, with realistic prereqs | Two options with full command sequences, including the BIOS virt detour for fairness |
| Endpoints | Tabular surface + one example per endpoint | 7-row table, ready-to-paste curl examples below |
| Project layout | Mirror the actual tree, with one-line annotations | Mirrors `app/` and `tests/` directly |
| Development journal | Pointer to `docs/` | One paragraph + link to `docs/README.md` |
| Testing | Test commands + the testing philosophy | Both included; philosophy points to phase 4's track |
| Configuration | Tunables table with default and effect | 8 most important env vars in a table |
| AI in My Workflow | The required section | See §7.5 |

### The README's most important sentence

In "Project layout":

> Module boundaries map 1:1 to the layers we want to scale or replace
> independently: HTTP, async transport, persistence, search, cache.

This is what a maintainer needs to know in one sentence. Everything
else in the README is reference; this is **the orientation**.

## 7.4 The cross-link discipline

A doc pass like this is also a chance to fix the broken-internet
problem: docs that reference code that no longer exists, or examples
that don't compile.

Audit:

- Every file mentioned in a doc → verified to exist at the given
  path.
- Every code snippet in a doc → checked against the actual file.
- Every test mentioned by name → verified `pytest -k <name>` finds
  it.
- Every endpoint mentioned in README → verified against the
  running app's `/openapi.json`.

The cost of this audit is low (it's just `grep` + a `pytest -k`
sanity check). The cost of *not* doing it is an ARB reviewer
pulling on a thread that's gone stale and losing confidence in
everything else.

## 7.5 "AI in My Workflow" — the platform-engineering version

This is the section most "AI in our workflow" write-ups botch. The
team agreed to document AI use for shared learning across the
group, and the failure mode is generic: *"I used Claude to help me
write code."* That doesn't help the next engineer calibrate when
or how to use it.

The version I committed to follows four principles:

1. **Name the tools.** Specific tools, specific contexts.
2. **Cite specific value, not generic vibe.** "Pydantic validators"
   not "scaffolding help."
3. **Show where AI was wrong.** The model's first proposal for
   the worker's queue used per-message `create_task(sleep)` — I
   rejected it for priority-queue semantics. Naming that is more
   credible than naming successes.
4. **Frame AI as judgment-amplifying, not work-replacing.**
   Senior-engineer AI use looks like *"I ran this tradeoff through
   the model as a sounding board, then decided X."* It does **not**
   look like *"I asked Claude to design the system."*

The README section delivers on all four. It also closes with the
framing we'd want junior teammates to internalize:

> *At a principal level, that's where AI earns its keep: not in
> code volume, but in shortening the time between "I have a design
> instinct" and "I can defend it under scrutiny."*

## 7.6 What I deliberately didn't include in either doc

A few things I considered including and decided against, with
reasoning:

- **A "performance" section.** I have no benchmarks against real
  traffic. Including one without data would be theater.
- **A list of "lessons learned."** Lives in the docs/ journal,
  not the deliverable.
- **An ER diagram for Mongo.** The schema is one collection with
  six fields. An ER diagram would add visual noise without
  information.
- **A roadmap for v2.** Premature; the platform team's design
  brief already asks "what would you do differently," which §12
  covers.

The discipline: ask of every candidate addition, *"does this help
reviewers (or future maintainers) reason about the design, or am I
padding to look thorough?"* Pad-to-look-thorough makes the doc
worse.

## 7.7 The README ↔ ARCHITECTURE separation

A trap with these two docs is letting them overlap. They have
**different** jobs:

- `README.md`: how to run / use / extend. **Operational.**
- `ARCHITECTURE.md`: how to reason about / defend the design.
  **Conceptual.**

The phase 7 audit deliberately removes overlap. If a section in
the README was explaining a design tradeoff, it gets cut to a
one-line summary + a pointer to ARCHITECTURE. If a section in
ARCHITECTURE was explaining how to run something, it gets cut to a
pointer to README.

This separation matters because reviewers will read both. If
they're 60% the same, the second one looks like padding.

## 7.8 What's still pending (deliberately)

Some loose ends I'm not closing in phase 7, with rationale:

- **No screenshots embedded in the main docs.** Any reader can
  run `docker compose up` and see the swagger UI themselves.
  Inline images bloat the repo without proportional information
  value. (A standalone reference image lives in `docs/images/`
  for the journal's phase 5/7 visual cue; it's outside the main
  docs.)
- **No CHANGELOG.md.** Single-author, single-week project; one
  commit is plenty.
- **No CONTRIBUTING.md.** Not a public project.

Each of these would be table-stakes for a real open-source repo.
Adding them at this milestone is the same kind of pad-to-look-thorough
the doc audit was screening against. They can land when there's a
real reason — e.g. when we open-source any of this.

## 7.9 What carries forward to phase 8

The repo is ready for review and release. Phase 8 is the
last-mile — final review checklist, PR for merge, ARB sign-off,
release tag, ops hand-off. The hard work is done.

## 7.10 Cross-references

- The final `README.md`: [`../README.md`](../README.md).
- The final `ARCHITECTURE.md`: [`../ARCHITECTURE.md`](../ARCHITECTURE.md).
- The journal index: [`README.md`](README.md).

---

_Phase 8 in progress..._
