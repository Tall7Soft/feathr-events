# Phase 5 — Manual QA against the real stack (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_5_manual_qa_technical.md`](phase_5_manual_qa_technical.md).
> **Previous phase:** [`phase_4_testing_stakeholder.md`](phase_4_testing_stakeholder.md).

---

## 5.1 Where we are in the project

Phase 4 closed with the test suite all green. By the standard
"my tests pass, I'm done" measure, the project would be over.

Phase 5 is what separates **"my tests pass"** from **"the system
actually works in production."** It's where a platform engineer
earns their salary.

This phase is the **most valuable** in the whole project — by far.
It uncovers six real bugs that none of the automated tests could
catch. One of them would have silently lost data in production.

## 5.2 Why "tests pass" isn't enough

Automated tests in phase 4 used **fake versions** of the databases.
The fakes behave correctly for normal cases, but they're not the
real thing. There's a category of bugs that only show up when you
talk to the **real** database — bugs that come from:

- The database's specific quirks and defaults.
- Network and timing issues (what if the database is slow? frozen?
  unresponsive?).
- The way different libraries serialize data on the wire.
- Interactions between several real services running at once.

A senior platform engineer **assumes these bugs exist** and goes
looking for them before declaring victory.

## 5.3 Setting up the real environment

The first task in this phase: install Docker (the tool that runs the
real databases) on the development machine. Sounds simple. Wasn't.

The machine's CPU had **virtualization disabled in the firmware** —
a low-level setting that only the machine's BIOS can change. So
before any actual QA work, the laptop had to be rebooted into BIOS,
the setting enabled, then rebooted back to Windows.

**Why mention this in the doc?** Because real-world engineering work
includes this kind of friction. A submitted answer that says "I
installed Docker and ran the tests" hides the messy reality. Being
honest about the friction is part of how platform work actually
gets done.

Once Docker was up, the four services (the web app, the central
archive, the search index, the scoreboard) all booted up healthy
and ready to talk to each other.

## 5.4 How we tested

The methodology was straightforward: take each feature and **try to
use it the way a real user would**. Don't trust the automated tests.
Don't trust the design doc. **Watch what actually happens** when you
send a real request to the real system.

For some tests, that meant pretending to be a normal user — send
the right data, get the right response.

For others, it meant **deliberately breaking things** — paused the
central archive to see how the system handled an outage; sent
malformed data to see how the system rejected it; spammed the
front door to test the rate limiter.

This is the kind of testing that catches what automation can't.

## 5.5 The six bugs we found

Listed by severity, worst first:

### Bug #6 (CRITICAL) — Silent data loss

**The most consequential bug in the entire project.**

When the central archive is unreachable for a moment (which happens
in any real production system — network blips are normal), the
system **reported the event as successfully saved** when in fact it
had been silently lost. The counters said "all good." The reality
was data on the floor.

This would have caused **invisible data loss in production**. Not
a crash, not an alert — just events quietly missing from the
record. The worst kind of bug.

The root cause was a too-broad error-handling pattern in the code.
The fix narrows the catch to only the specific case it was meant
for. After the fix, the same scenario correctly retries the save
and, if it still can't succeed, sends the event to the dead-letter
inbox for an operator to investigate.

**Why automated tests missed it:** the fake archive doesn't know
how to throw a "network timeout" error in the same shape the real
archive does. The test was checking the wrong-shaped failure.

### Bug #5 (HIGH) — Worker hangs indefinitely on a frozen database

When I deliberately froze the central archive (without disconnecting
it — just paused the process), the worker waited **forever** for a
response. No retry. No timeout. The whole ingest pipeline backed up
behind one stalled operation.

The root cause was a missing timeout setting on the database
connection. The fix adds it: now the worker gives up after a
configurable number of seconds, marks the operation as failed, and
retries with backoff.

**Why automated tests missed it:** it's not a logic bug — it's a
configuration bug. The fakes don't model real-world timeouts.

### Bug #3 (HIGH) — Search breaks when partners send numbers

If any partner sent an event with a **numeric metadata field**
(like a purchase amount), it broke **all subsequent text searches
across the system**. A search for `"spring campaign"` would
hard-fail with a 500 error because the search engine couldn't
figure out how to apply the text search to the numeric field.

**Real-world impact:** the moment a single conversion event with
an amount field landed in the system, search broke for everyone
until we deployed a fix.

The fix is a single configuration flag on the search query that
tells the search engine "skip fields whose type doesn't match
the search term, don't fail the whole query."

**Why automated tests missed it:** the fake search engine does
simple substring matching across everything. The real search
engine's type system is stricter.

### Bug #1 (MEDIUM) — Timestamps without timezone

Every timestamp returned by the system was missing its **timezone
suffix**. Instead of `2026-05-31T12:00:00Z` (the `Z` means UTC),
the system returned `2026-05-31T12:00:00` — no zone info at all.

Why this matters: clients that consume this API in other
programming languages (especially Java and Go) would have to
**guess** the timezone, and might guess wrong. Reports might be
shifted by hours depending on the consumer's local time.

The fix tags every timestamp as UTC before returning it.

**Why automated tests missed it:** the fakes don't strip timezone
info the way the real database does. The data round-trips
correctly in tests; only with the real database does the bug
show up.

### Bug #4 (LOW) — Timestamps in two different formats

A subtle cosmetic version of the same problem. When the system
computed a fresh timestamp, it returned `2026-05-31T12:00:00Z`
(ending in `Z`). But when the same value was read from the cache,
it returned `2026-05-31T12:00:00+00:00` (ending in `+00:00`).

Both are valid representations of "noon UTC", but they're not
the same string. Strict parsers in some languages reject one but
not the other.

The fix forces both code paths to use the same format.

**Why automated tests missed it:** the test was comparing only
the values, not the string format.

### Bug #2 (LOW) — Weekly buckets start on the wrong day

When grouping events by week, the real database started the week
on **Sunday**, while the fake database (and most internationally-
oriented analytics conventions) start on Monday.

This would cause dashboards to be off by a day, depending on
whether they were looking at the fake or the real environment.

The fix is one line of configuration to force the start of week
to Monday.

**Why automated tests missed it:** the fake and the real database
have different defaults; the test only ran against the fake.

## 5.6 A bonus discovery: the "phantom write"

While running the bug #6 experiment, I noticed something
interesting. The bug had been fixed, the system was correctly
sending failed events to the dead-letter inbox. But when I
**unfroze** the central archive at the end of the experiment, I
found the same event was now also in the archive.

So: the event was both in the dead-letter inbox (for the operator
to investigate) **and** in the central archive (saved successfully).
Both at once.

What happened: while the archive was frozen, the event's data was
sitting in the operating system's network buffer waiting to be
processed. When the worker timed out and gave up, the system
thought the save had failed and sent the event to the dead-letter
inbox. But when the archive un-froze, it processed the buffered
data and saved the event anyway. The worker never got the success
response (it had already given up by then).

**Why this is OK:** the system was designed with this scenario in
mind. Each event has a unique ID. If an operator later replays
the dead-letter inbox, the archive will recognize the duplicate
event ID and gracefully skip it. **No data corruption, no double
counts.**

This is exactly the kind of edge case the platform team's design
brief asks us to think through. We documented it explicitly in the
architecture doc.

## 5.7 What this phase says about the project

The phase 4 test suite was green. The system "worked" by any
automated measure. But manual QA found:

- **One bug that would have silently lost data in production.**
- **One bug that would have stalled the ingest pipeline during
  any database hiccup.**
- **One bug that would have broken search for the entire system
  the moment a single numeric value was indexed.**
- **Three cosmetic bugs that would have caused subtle reporting
  errors across systems.**

A junior engineer would have shipped after phase 4 and discovered
these in production. A senior platform engineer **assumes** these bugs
exist, goes looking for them, and finds them.

This is the most important phase in the project for what the
platform team values most: **judgment**.

## 5.8 What happens next

Phase 6 takes the six bugs found here and:

1. Fixes each one in code.
2. Writes an **automated test** for each fix so the bug can never
   come back without us knowing.
3. Updates the architecture document to document the lessons
   learned (timeouts, the phantom-write scenario, the
   ES-of-numeric-data trap).

The test count goes from 30 to 48. The system gets meaningfully
more robust.

## 5.9 What the system looks like at the end of this phase

Imagine the office building from phase 4. We've now opened the
doors and run a controlled trial — real visitors, real packages,
some on purpose-broken scenarios to see how the building responds.

We found six things that needed fixing — including one that would
have been a disaster in production. None of them were visible
during construction. All of them are now fixable, with confidence,
because we found them **before** the building opened to the
general public.

The next phase (phase 6) is fixing them and adding alarms so
they can't come back.

---

_Phase 6 in progress..._
