# Phase 2 — Scaffolding and repo architecture (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_2_scaffolding_technical.md`](phase_2_scaffolding_technical.md).
> **Previous phase:** [`phase_1_planning_stakeholder.md`](phase_1_planning_stakeholder.md).

---

## 2.1 Where we are in the project

Phase 1 was the **planning** phase. We decided what we were building,
why, and how the pieces would fit together — all on paper.

Phase 2 is **putting up the frame** before we paint any walls. No
real functionality yet; just structure. Think of it like a
construction site after the foundation and framing are done, but
before any drywall or wiring goes in.

You can walk through the rooms, but nothing is functional yet. The
point of this phase is to make sure the building's shape is right
**before** we make it expensive to change.

## 2.2 Why this matters before writing actual logic

There's a temptation to skip this phase and "just start coding". On a
small one-person project that sometimes works. On anything bigger, it
backfires for two reasons:

1. **Moving walls is cheap before drywall, expensive after.** Once
   logic starts to depend on a structure, changing it gets harder.
   We do the structural work first so the costly stuff lands on
   stable ground.
2. **The frame tells the team how to work together.** A clear layout
   means a new engineer can find the right room without asking. That
   matters when this project goes from one person to a team — which
   the platform team has explicitly committed to planning for.

## 2.3 What we decided in this phase

### How the team will work — the folder layout

The system has five clear "rooms", each with one job:

- **Reception** (`app/api/`) — the people who answer the door.
- **Mailroom inbox** (`app/queue/`) — where events wait their turn.
- **Back office** (`app/workers/`) — staff who process events.
- **Archives + index room** (`app/storage/`) — where everything is
  filed and indexed.
- **Lobby scoreboard** (`app/cache/`) — the public display of
  current numbers.

Each room owns its own work. None of them reaches across into
another's job. This is what makes the system easy to maintain:
**if something's wrong with the scoreboard, you only debug the
scoreboard's room**.

### What we explicitly chose NOT to build

Three things that would have been easy to add but we deliberately
skipped:

- **A generic "utilities" closet.** Every codebase that has one ends
  up with a closet full of unrelated junk that nobody owns. Helpers
  go in the room that owns the concept, not in a shared dumping
  ground.
- **A "business logic" floor.** The "business logic" of this system
  *is* the back office worker. Adding a separate floor would just be
  a place to dump code that doesn't fit anywhere — which is the
  start of every spaghetti codebase.
- **An umbrella "data" room** combining the archive and the
  index. They have different responsibilities and different failure
  modes. Putting them in the same room would hide that, and would
  make it tempting to write code that assumes they behave the same.

These are small decisions, but they're the kind of thing a
platform engineer is expected to be intentional about.

## 2.4 The tools we picked

We committed to a specific set of software libraries. Each one
costs something to maintain (every version upgrade, every security
patch, every learning curve for a new hire), so we kept the list
short and justified each one.

The full list and reasoning lives in the technical version of this
document. For non-technical context, the key points:

- **No exotic libraries.** Each tool is the conventional, widely
  used choice for its job. New engineers don't have to learn niche
  tools to contribute.
- **All versions pinned.** "It works on my machine" is a real
  problem; pinning versions means everyone on the team runs the
  same thing.
- **One library per job.** No two libraries doing the same thing
  in slightly different ways.

## 2.5 How operators will configure the system

Software has two audiences: **developers** (who change the code) and
**operators** (who run the code in production without changing it).
We made sure the operator audience can do their job without ever
opening the code:

- **Every tunable knob is a setting.** How many events per batch?
  How long to cache numbers? When to give up retrying? All
  configurable from outside the code.
- **Every setting is type-checked at startup.** If an operator
  fat-fingers a number, the system refuses to start with a clear
  error message. It doesn't start in a half-broken state.
- **Defaults are sensible.** A new operator who deploys without any
  configuration still gets a working system.

This is the kind of operational care that's invisible when it's
done right and very expensive when it isn't.

## 2.6 The "swap a piece later" decision

One of the more important architectural choices we made in this
phase is what's called a **storage seam** — a clear line between
"the worker" and "where data is stored" such that we can swap one
storage technology for another without rewriting the worker.

The business case for this:

- If we move from MongoDB to a different database later (say, our
  cloud provider offers a managed alternative), the worker code
  doesn't change.
- If Elasticsearch is replaced with a different search engine (say,
  OpenSearch), the worker code doesn't change.
- If we want to write all events to S3 for long-term archival in
  addition to MongoDB, that's a new "store" plugged into the same
  seam — not a rewrite.

Most importantly: the **test suite** can plug in fake stores so we
can test the system without standing up real databases. That makes
the test cycle faster, which makes the team iterate faster.

## 2.7 What we deliberately deferred

A few things we knew we'd need eventually, but didn't build in this
phase:

- **The actual events flowing through the system.** That's phase 3.
- **Automated tests.** That's phase 4. We built phase 2's foundation
  *so* phase 4 would be cheap.
- **Real data validation.** Stub validation went in here; the real
  rules (e.g. "metadata can have at most 64 fields") get added in
  phase 3 alongside the tests that prove them.

This is a key habit: **don't build something just because you
could**. Wait until there's a clear reason to. Every line of code
is a line that has to be maintained.

## 2.8 What the system looks like at the end of this phase

Imagine an office building. The framing is up. You can walk through
and see where reception goes, where the mailroom goes, where the
back office goes. Nothing is functional yet — no phones, no
computers, no desks — but **the shape is right**.

A new employee walking through can already understand "oh, this is
where my desk would go" and "that's where the printer would be".
That's the value of this phase: clarity before activity.

The next phase (phase 3) is where the desks, phones, and computers
go in.

---

_Phase 3 in progress..._
