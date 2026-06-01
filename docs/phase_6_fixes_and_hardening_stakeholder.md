# Phase 6 — Fixes and regression hardening (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_6_fixes_and_hardening_technical.md`](phase_6_fixes_and_hardening_technical.md).
> **Previous phase:** [`phase_5_manual_qa_stakeholder.md`](phase_5_manual_qa_stakeholder.md).

---

## 6.1 Where we are in the project

Phase 5 went looking for bugs and found six of them — including one
that would have silently lost data in production. Phase 6 is when
we fix them.

But fixing the bugs is only **half** of phase 6's job. The other
half is making sure **these specific bugs can never come back** —
or if they try, that we know within seconds.

This is the difference between "we fixed it" and "we *hardened*
it." Hardening is what keeps a fix from being undone six months
later by an unrelated change.

## 6.2 The fix policy

Every fix in this phase follows two rules:

### Rule 1: Minimal scope
We change **only** what's necessary to fix the specific bug. If
we spotted other things we'd like to refactor, we wrote them
down for later. Tangling unrelated changes into a fix is a great
way to introduce new bugs while fixing old ones.

### Rule 2: A regression test for every fix
Every fix lands together with an **automated test** that proves
the bug is fixed and would alert us if anyone undoes it. A fix
without a test is half a fix; in six months someone might
accidentally re-introduce the problem and nobody would notice.

This is the platform-engineering discipline. It's slower in the
short term and much faster over time.

## 6.3 The six fixes

A short summary of each fix and the new safety net we built
around it.

### Fix #1 — Timestamps now correctly tagged as UTC

**What was wrong:** every timestamp in API responses was missing
its timezone label.

**What we did:** added a small piece of code that explicitly tags
every timestamp from the archive as UTC before returning it.

**The new safety net:** two automated tests now check this
behavior. If anyone changes the code in a way that breaks the
tagging, the tests fail immediately.

### Fix #2 — Weekly reports start on the right day

**What was wrong:** when grouping events by week, the real
archive started the week on Sunday; everyone else expected
Monday.

**What we did:** explicitly told the archive "weeks start on
Monday" in our queries.

**The new safety net:** an automated test confirms that every
weekly query carries this setting.

### Fix #3 — Search no longer breaks on numeric data

**What was wrong:** if any partner sent an event with a numeric
field (like a price), text search across the system broke with
a 500 error.

**What we did:** added a setting to the search query that tells
the search engine "skip fields whose data type doesn't match the
search term, don't blow up."

**The new safety net:** automated tests verify the setting is
present and that the right fields are being searched. A
refactor that drops the setting will fail tests.

### Fix #4 — Cache and live data now use the same timestamp format

**What was wrong:** the same UTC time appeared as `Z` in some
responses and `+00:00` in others. Strict consuming systems
choked on the inconsistency.

**What we did:** forced both code paths to emit the same
format.

**The new safety net:** an automated test now sends a known
timestamp through the cache and verifies it comes back in the
expected format.

### Fix #5 — Worker no longer hangs on a frozen database

**What was wrong:** the worker would wait forever for a
response from a database that wasn't responding.

**What we did:** added a configurable timeout to database
operations. Now the worker waits up to a sensible amount of
time, then gives up and retries (or eventually sends to the
dead-letter inbox).

**The new safety net:** we re-ran the original phase 5
experiment to confirm the fix works against the real database.
We deliberately did NOT add an automated test for this fix —
the test would require simulating low-level network behavior,
which is brittle. We documented this choice in the architecture
doc as a known coverage gap that we'd close with more time.

This is a platform-engineering habit: **be honest about what your
test suite can and cannot verify**.

### Fix #6 — No more silent data loss (THE BIG ONE)

**What was wrong:** when the archive briefly couldn't respond
(a normal occurrence in any production system), the worker
reported events as successfully saved when they weren't.

**What we did:** narrowed an over-broad error-handler so that
"database unreachable" errors now correctly propagate up to the
worker's retry logic, instead of being silently treated as
"all duplicates, no problem."

**The new safety net:** five new automated tests covering the
exact failure modes that caused the bug, plus all the related
cases (genuine duplicates, mixed errors, clean writes, empty
batches).

The most important of these is a test that **deliberately makes
the archive throw the same network error** that caused the
original bug, and verifies that the error propagates correctly.
If anyone in the future widens the error handler again — even
accidentally — this test will catch it immediately.

This is the kind of test that pays for itself the first time it
prevents a regression.

## 6.4 Bonus: tests we added because we were already there

While writing the regression tests, we noticed a few other gaps
in the test coverage and filled them:

- **Mixed-batch handling.** What if the worker receives a batch
  with some valid events and some invalid? We added a test that
  proves the invalid ones go to the dead-letter inbox and the
  valid ones get saved normally — **a poison-pill doesn't block
  its siblings**.
- **Rate limiter end-to-end.** The bouncer (rate limiter) was
  tested through real HTTP calls in phase 5; we added direct
  unit tests so any regression is caught instantly, not the
  next time someone does manual QA.

A few hours of testing investment now saves days of debugging
later.

## 6.5 We re-verified everything against the real environment

After all the code fixes and new tests, we **re-ran the full
phase 5 QA** against the real stack of databases. Every scenario
that surfaced a bug now passes. No regressions introduced. The
fixes hold up against reality, not just against our tests.

This is what "done" looks like at principal level: tests pass,
*and* manual verification passes, *and* the manual verification
is something we can re-run in 30 minutes if we need to.

## 6.6 We updated the architecture document

Two new sections in the architecture document:

1. **A description of the "phantom write" scenario** we noticed
   in phase 5 — the case where an event ends up both in the
   dead-letter inbox AND in the archive. We explain why the
   system tolerates this without corrupting any data.
2. **An appendix of every finding from manual QA**, with the fix
   and the regression test. This is so the architecture review board can
   see at a glance "here are the bugs found, here's how they
   were fixed, here's how we prevent regression."

This appendix is what proves the manual QA actually happened.
Anyone can write "we tested it" in a README. The appendix shows
the receipts.

## 6.7 Test count: 30 → 48 (and later 108)

The test count went from 30 to 48 during phase 6 — a 60% increase.
Importantly, the suite **still runs in under 2 seconds**. We did
not slow it down by adding regression coverage.

A follow-up sprint after the release pushed the count further to
**108 tests with 94% statement coverage**, locking in the
production storage paths (Mongo, Elasticsearch, the in-memory
fakes used in tests) with mock-based unit tests. The full suite
still runs in under 3 seconds. The "with more time" item from
phase 4 — covering the storage layers without standing up
containers — is now closed.

This matters because a fast test suite is one that **actually
gets run**. A slow one gets skipped. The investment in keeping
tests fast pays off forever.

## 6.8 What the system looks like at the end of this phase

Imagine the office building from phase 5. We found six issues
during the controlled trial. By the end of phase 6:

- Every issue is fixed.
- Every fix has an alarm wired up so the issue can't come back
  without us knowing immediately.
- The most dangerous issue (the silent data loss) has the
  strongest alarm.
- We've added a few extra alarms for issues we noticed but
  weren't part of the original trial.
- We re-ran the controlled trial with everyone watching, and
  every scenario passes.

The architecture document now has receipts: not just "we tested
it" but **"here's everything we found, here's how we fixed it,
here's how it's prevented from recurring."**

The next phase (phase 7) is the final documentation polish —
making sure everything is written down clearly for the
architecture review board, the ops team taking over the service,
and any engineer who joins the project later.

---

_Phase 7 in progress..._
