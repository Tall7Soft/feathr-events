# Phase 1 — Planning and project understanding (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_1_planning_technical.md`](phase_1_planning_technical.md).
>
> This document explains the same project as the technical one, but
> in plain language. The goal isn't to "dumb it down" — it's to
> **translate** it. The concepts are the same; the vocabulary changes.

---

## 1.1 What are we building, in one sentence?

A system that **receives events from a website** (page views, clicks,
purchases) and **stores, searches, and summarizes** them so a team can
answer questions like *"how many people visited my page today?"* or
*"what did this user do last week?"*.

It's the kind of system that sits behind any analytics dashboard.
What's distinctive about this one is that it's designed to handle
**a lot of volume** without falling over.

## 1.2 Why are we doing this?

We're a marketing-tech company (Feathr) that builds software for
non-profits. Our product team has committed to launching a
**per-customer analytics dashboard** next quarter — a new product
surface that shows each of our customers a live view of how visitors
are interacting with their campaigns: live counts of events, history
they can search through, and breakdowns by event type over time.

The problem: our current event pipeline can't do any of that at the
scale we're already quoting. It writes every event synchronously to
a single database, breaks above ~50 events per second per customer,
and has no search capability at all. Sales has already sold the
dashboard to several enterprise customers whose traffic is **10×
that**.

**What I'm building:** the new event platform that will replace the
current pipeline. Same customer-facing contract (so the existing
integrations keep working), but the new internals are:

- An async ingestion pipeline that doesn't make every visitor wait
  for the database.
- A separate search capability for the dashboard's
  "search-your-history" feature.
- A fast realtime stats path for the dashboard's live counters.

**Why now:** the dashboard launch is the forcing function, but every
product surface downstream (search, exports, customer segmentation,
on-call investigation tools) will eventually sit on this platform.
Skipping the investment now means rebuilding it from the wrong place
later.

**How "done" looks:**
- **Deadline:** one sprint (one week) for the platform-side milestone.
- **What I deliver:** the code for the system + a design document
  that explains every architectural decision.
- **Who signs off:** peer code review → the Architecture Review Board
  (ARB) → the ops team that takes it over for production.
- **Important detail:** the design document carries **as much weight
  as the code**. Working software isn't enough; the next engineer
  to maintain this system has to be able to understand the reasoning
  without re-deriving it from scratch.

## 1.3 What problem does the system solve?

When a website has heavy traffic, every visit generates an **event**:
someone saw this page, someone clicked this button, someone completed
a purchase. A company wants to do three things with those events:

1. **Store them** without losing any.
2. **Search through them later** ("show me everything from the spring
   campaign").
3. **See live totals** ("how many visitors do I have right now?").

The challenge is that **a single system rarely does all three well**.
Each one has different requirements:

| What the business wants | What the system needs to be |
| ----------------------- | --------------------------- |
| Store everything without losing it | Reliable, durable, ordered |
| Search free text | Fast for flexible queries |
| Show live numbers | Instant, even if approximate |

The solution is to **combine three specialized pieces**, each doing
what it does best.

## 1.4 How do we solve it? (analogies)

Picture the system as a **library with three sections**:

### The central archive — *MongoDB*
This is where all the books (events) live, in order, with their
inventory numbers. It is **the source of truth**: if someone asks
"does this book exist?", we ask the archive. If the rest of the
library burns down, the books can be reconstructed from here.

### The keyword index — *Elasticsearch*
Like the index in the back of an encyclopedia: it does not hold the
books, it holds a list of words and where to find them. Useful for
"find me everything that mentions *spring marketing*". It is fast at
free-text search because it is purpose-built for that.

### The lobby scoreboard — *Redis*
Shows the current numbers: *"1,247 people came in today"*. It does
not have all the detail, just the summary. It refreshes every few
seconds, not on every event.

### The mailroom inbox — *the queue*
When an event arrives, **it is not processed immediately**. It is
dropped in an inbox and an employee picks it up when they are free.
That way the client who sent the event **does not wait** for it to
be saved — it receives an instant "received, thanks".

## 1.5 Decisions that matter to the business

These are the questions a non-technical team will ask:

### What happens when one piece fails?
The system is designed so that **failures don't cascade**:

- If **the search index** (Elasticsearch) goes down → we can still
  save events and show totals. Only free-text search stops working.
- If **the central archive** (MongoDB) goes down → events aren't
  lost: they wait in the mailroom inbox, and when the archive comes
  back, they're processed.
- If **the scoreboard** (Redis) goes down → live totals are slower to
  compute, but they still work.

In short: **a failure in one piece doesn't take down the whole system**.

### What happens if traffic grows 10×?
Sales has already quoted customers whose traffic profiles are 10×
our current cap. My answer to the platform team and product:
**I know which piece will break first and what to do about it.**
This isn't "I'll see when it happens"; it's a documented plan.

This matters to the business because it means **the system isn't
surprised by success** — growth is something that's planned for, not
an emergency.

### How expensive is this to maintain long-term?
The system is organized into **clearly separated pieces**, each with
its own responsibility. Any new engineer can understand one piece
without having to understand the whole system. That reduces the cost
of bringing new people onto the team.

## 1.6 Decisions where I could have taken the easy path but didn't

At platform engineering scale, what matters is **judgment** — not the
ability to do the obvious thing. A few examples of decisions where I
picked the right path over the easy one:

- **Not indexing everything in the search engine.** It would be simpler
  to "index everything, just in case", but that makes the system slower
  and more expensive to maintain. I chose what to index and what not
  to, and documented why.
- **Not invalidating the cache on every event.** It would be more
  "correct" in theory, but in practice it multiplies the system's
  workload under load without a real benefit. I accepted that live
  numbers can be a few seconds out of date — which is exactly what
  users expect from a dashboard.
- **The search index is secondary.** If the search index returns
  something inconsistent, I regenerate it from the central archive.
  **The central archive wins.** This avoids the classic problem where
  "the search index says one thing and the database says another".

## 1.7 Work plan

| Phase | What I do | Why it matters |
| ----- | --------- | -------------- |
| 1. Understand the requirements | Read the platform team's design brief, understand the real problem | Know what I'm solving |
| 2. Design the architecture | Decide which pieces, what each one does | Have a plan before building |
| 3. Build | Implement one piece at a time | The main work |
| 4. Test each piece | Automated tests | Each part works in isolation |
| 5. Test the whole system | Stand it up for real and use it | The pieces work **together** |
| 6. Document the decisions | Write the architecture document | What carries the most evaluation weight |
| 7. Review and release | Peer code review, ARB sign-off, merge, hand off to ops | End of the build phase |

## 1.8 Risks I see from the start

The three most likely risks and how I'm managing them:

### "It works on my machine but not in production"
The classic risk. Mitigation: **I don't call the work done until it's
tested against the real services** (not simulations). I'm reserving
time for this phase.

### "The document ends up as a technical manual"
The architecture document has to explain the **why** of each decision,
not just the what. Mitigation: the document's structure **forces**
specific sections (diagram, what happens if something fails, what
happens if growth doubles). If a section comes out empty, that tells
me something I didn't think through.

### "Underestimating the time to write well"
The platform team's design brief states it directly: *"the
architecture document is as important as the code."* Mitigation: I
write the reasons **as I decide**, not at the end. This also makes
the reasons real — if I wrote them at the end, I'd tend to justify
what I did; writing them in the moment records what I actually
thought.

## 1.9 How will I know I'm done?

A clear list of criteria, not a feeling:

- The **7 functional requirements** the platform team agreed on are
  met.
- The **5 secondary items** (Docker setup, rate limiting, dedupe,
  dead-letter queue, SQS migration notes) are also done.
- All automated tests are green.
- The system has been tested against the real services, not just
  simulations.
- The architecture document covers the **6 mandatory sections**.
- The README explains how to run the service and how AI tools were
  used in the build.
- **Any decision in the document can be defended at the Architecture
  Review Board**, and a future maintainer can read the doc and
  understand the reasoning behind every tradeoff. This is the most
  important criterion: if a decision cannot be explained, that
  decision is not done.

---

_Phase 2 in progress..._
