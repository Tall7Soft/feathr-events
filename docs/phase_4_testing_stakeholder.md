# Phase 4 — Testing strategy and coverage (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_4_testing_technical.md`](phase_4_testing_technical.md).
> **Previous phase:** [`phase_3_implementation_stakeholder.md`](phase_3_implementation_stakeholder.md).

---

## 4.1 Where we are in the project

Phase 3 turned the lights on — the system runs. Phase 4 installs
the **alarms, sensors, and inspections** that tell us the lights
**stay on** every time we change something.

Without automated tests, every change is a gamble: "I think this
works, but I'm not sure I didn't break something else." With
automated tests, every change is a checkable claim.

## 4.2 The point of automated testing

Here's the simple version: **a test is a question you ask the system,
with an expected answer**. If you ask "if I send this event, does
it get saved?", the test sends the event and checks that it got
saved. If the answer ever changes, you know something broke.

The value isn't writing the tests once — it's running them on
**every change**. That's how teams move fast without breaking
things.

A few questions that matter to the business:

| Concern | What a test guarantees |
| ------- | ---------------------- |
| "Don't lose events" | We're never silently dropping work |
| "Don't break old features when adding new ones" | Regression is caught immediately, not in production |
| "New hires can change code with confidence" | A green test suite tells a junior engineer "your change is safe" |
| "We can defend our design choices" | Each test is executable proof of a design decision |

## 4.3 Our testing philosophy

Four explicit choices:

### 1. Test the boundaries, not the libraries

We don't test that the database library inserts records. The
database library's authors already tested that. We test the things
*we wrote* — that **our worker** handles a failed save the way we
intended, that **our** rate limiter actually blocks excess requests,
that **our** event validator rejects bad URLs.

This keeps the test suite focused and reasonably sized.

### 2. Fast feedback over comprehensive coverage

A test suite that takes 5 minutes to run **doesn't get run** on
every change. People skip it. The whole investment goes to waste.

Our suite runs in **under 2 seconds**. That means it runs
automatically every time we save a file. There's no excuse to skip
it.

We don't chase 100% coverage. We cover the behaviors that matter
and accept that the long tail of edge cases will be caught by
other means (phase 5's manual testing, for example).

### 3. Use fake versions of slow infrastructure

To test the system end-to-end without spending 30 seconds starting
up real databases, we built **fake versions** of MongoDB and
Elasticsearch that live in memory. They behave the same way from
the outside (same questions, same answers), but they start instantly.

This is the payoff of phase 2's "storage seam" decision: because we
designed the system so the storage layer was swappable, the tests
can plug in fakes for free.

### 4. Be honest about blind spots

The fakes are simpler than the real databases. They don't catch
every kind of bug a real database might catch. **We accept that
trade-off**, and we plan for it: phase 5 (manual testing against
real databases) is specifically designed to catch what phase 4
can't.

This is the platform-engineering instinct: **don't pretend your tests
prove more than they actually prove**.

## 4.4 What we test

A summary of what's covered:

| Area | What we check | Why it matters |
| ---- | ------------- | -------------- |
| Event validation | Bad event types are rejected, oversized metadata is rejected, URLs are validated | The system doesn't accept garbage |
| Mailroom (queue) | Delayed delivery works, retry counters increment, the system knows when work is done | The async pipeline doesn't lose work |
| Back-office worker | Retries on failure, gives up after N attempts (sends to dead letter), recognizes duplicates, doesn't re-queue on search-index failure | The save-to-storage logic does what we promised |
| Search index failure handling | If search fails, the worker continues; if the archive fails, the worker retries | **The key trade-off we promised in the design doc** |
| Scoreboard caching | One person triggers the recompute, everyone else reuses the result | Spike traffic doesn't degrade the scoreboard |
| End-to-end round trip | Submit an event → wait for it to be saved → query it back | The whole system actually works together |
| Health endpoint | The system reports its own state accurately | Operators can monitor in production |

## 4.5 The pattern that makes integration tests reliable

Tests that exercise async pipelines have a classic failure mode:
**asserting on the result before the work is done**. Without care,
tests become flaky — they pass most of the time but occasionally
fail randomly.

We avoid this with an explicit "wait for the inbox to drain" helper:
the test sends events, then waits until the queue confirms it's
fully processed, **then** asserts on the result.

This means our tests are deterministic. They pass or fail
consistently, never randomly.

## 4.6 What this phase deliberately doesn't cover

Three categories of test we **chose not to write** in this phase,
each with a reason:

### Database-specific quirks

The fakes don't speak MongoDB's exact query language or
Elasticsearch's exact indexing rules. So bugs that come from those
specifics aren't caught here.

**Why not?** Because phase 5 (manual testing against the real
databases) is a more honest way to find those bugs. Phase 4 covers
the business logic; phase 5 covers the integration.

### Multi-server behavior

Our rate limiter and our cache work correctly with one server, but
behave differently across many servers. Testing the many-server
behavior requires expensive test infrastructure.

**Why not?** It's outside the scope of this exercise. The plan for
"how this changes if we run multiple servers" is documented in the
architecture document.

### Catastrophic failure modes

We don't test "what happens if the disk fills up" or "what happens
during a network partition." Those require a real environment.

**Why not?** Phase 5 partially covers this (we test what happens
when the database is unavailable). The truly catastrophic failure
modes are documented in the architecture document as **what we'd
do differently with more time**.

## 4.7 What phase 4 explicitly tells phase 5

The honest summary of phase 4:

> **"We have high confidence in the business logic. We have zero
> direct evidence that the production databases work the way we
> assume."**

That's a precise statement, not a hedge. Phase 5 is designed to
close that gap.

This kind of honesty is part of what's being evaluated. A junior
engineer might say "tests pass, ship it." A platform engineer
says **"tests pass against fakes. Now let's verify against real
infrastructure before declaring done."**

## 4.8 What the system looks like at the end of this phase

Imagine the office building from phase 3, now with:

- A fire alarm that tests itself every hour.
- Motion sensors that confirm rooms are still active.
- A daily inspection that walks through every room and verifies
  the basics.

All of this happens automatically. If anything breaks, we know
within seconds, not hours.

But — and this is important — **we haven't actually used the
building with real people yet**. We've simulated visitors. We've
mocked up packages. Phase 5 is when we open the doors and see
what happens when reality walks in.

---

_Phase 5 in progress..._
