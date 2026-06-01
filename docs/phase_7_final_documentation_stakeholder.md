# Phase 7 — Final documentation pass (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_7_final_documentation_technical.md`](phase_7_final_documentation_technical.md).
> **Previous phase:** [`phase_6_fixes_and_hardening_stakeholder.md`](phase_6_fixes_and_hardening_stakeholder.md).

---

## 7.1 Where we are in the project

The code is done. The tests pass. The system has been verified
against the real databases. Now we need to make sure the **two
documents reviewers will actually read** are in their best
possible state.

The platform team's design brief was unusually direct about this:

> *"The architecture document is as important as the code, so
> budget time for it accordingly."*

Most engineers underweight this. We don't. Phase 7 is a
deliberate, careful pass through every section of both documents,
with **the architecture review board and a future maintainer as
the imagined readers**.

## 7.2 The two documents we're polishing

The project has two top-level documents, each with a different
job:

### `README.md` — the operating manual
**Audience:** anyone who picks up the project for the first time
and needs to **use** it.

**Job:** answer "how do I run this?", "what are the endpoints?",
"how do I run the tests?", "how do I extend it?"

### `ARCHITECTURE.md` — the design rationale
**Audience:** the Architecture Review Board, plus any future
engineer inheriting the system.

**Job:** answer "why is it shaped this way?", "what tradeoffs were
made and why?", "what happens when something fails?", "what would
break if traffic grew 10×?"

Keeping these two **distinct** is part of the discipline. If the
README starts explaining design rationale, the audience gets lost.
If the architecture doc starts explaining how to run things, the
reader's eyes glaze over.

## 7.3 What this phase actually does

Three things, applied to every section of both documents:

### Check 1: Can someone land on this section cold and follow it?
ARB reviewers and on-call maintainers don't read top-to-bottom.
They jump to whatever catches their eye. So every section has to
work **on its own**.

We re-read each section asking *"if this is the first thing someone
reads, will they understand it?"* Sections that fail get
rewritten.

### Check 2: Every claim has receipts
When the architecture document says *"the worker handles ES
failures differently from Mongo failures,"* we point at the **exact
file and line** where that's implemented. That way a skeptical
reviewer can verify the claim immediately.

### Check 3: The "why" is more visible than the "what"
The architecture document's first job is **explaining the
reasoning** behind decisions. We re-read each section asking *"is
the rationale obvious, or buried?"*

The biggest rewrite was the section on **what happens when the
search index fails**. The original version was correct but
underspecified. The rewritten version makes the reasoning
explicit and unmissable.

## 7.4 The "AI in My Workflow" section

The platform team has agreed to document AI use for shared
learning across the group. This is the kind of section where most
write-ups produce generic content ("I used Claude to help me
code"), which doesn't help the next teammate calibrate where AI
adds value and where it doesn't.

Our version is intentionally different. It does four things:

1. **Names specific tools and specific use cases.** "I used the
   model to generate the database aggregation pipeline" is more
   credible than "I used AI for help."
2. **Cites concrete value, not vibes.** Real examples of how AI
   saved time on specific tasks.
3. **Shows where AI was wrong.** Pointing out where I pushed
   back on the model's suggestions is, counterintuitively, the
   most credible part of the section. It shows judgment, not just
   tool use.
4. **Frames AI as amplifying judgment, not replacing thinking.**
   At platform-engineering scale, what matters is how well we
   **think**, not how fast we **type**. AI is a multiplier on
   thinking, not a substitute for it.

The section closes with a sentence that captures the framing:

> *"That's where AI earns its keep on platform work: not in code
> volume, but in shortening the time between 'I have a design
> instinct' and 'I can defend it under scrutiny.'"*

This is the message we want the team to internalize.

## 7.5 What we deliberately left out

Discipline isn't just about what we include — it's about what we
**don't**. A few things we considered including and chose not to,
each with reasoning:

### A performance benchmark section
We haven't done any load testing. Adding a "performance" section
without real numbers would be theater. **Better to be silent than
performative.**

### A list of "lessons learned"
That's what the phased journal (the documents you're reading) is
for. Adding it to the main README would duplicate content.

### A diagram of the database schema
The schema is one collection with six fields. A diagram would add
visual noise without adding information.

### A roadmap for "version 2"
The design brief asks "what would you do differently with more time" —
that question is already answered in the architecture document's
final section. A separate roadmap would be duplicative.

The general principle: **ask of every candidate addition, "does
this help reviewers and future maintainers reason about the work,
or am I padding to look thorough?"** Padding makes the document
worse, not better. Editing is the platform-engineering discipline.

## 7.6 The separation between the two documents

A trap we deliberately avoided: letting the README and
architecture document overlap.

- If the README starts to explain a design tradeoff, we cut it
  to a one-line summary and point to the architecture document.
- If the architecture document starts to explain how to run
  something, we cut it to a pointer back to the README.

This separation matters because reviewers (and on-call
maintainers) will read **both**. If they're 60% the same content,
the second one feels like padding, and the reader loses interest
before getting to the parts that matter.

## 7.7 The cross-reference audit

Documentation rots over time. Files get renamed; functions get
removed; tests get refactored. Phase 7 includes a deliberate
"cross-reference audit" — every link, every file path mentioned,
every code snippet, every test name **is verified** to still be
accurate.

This is dull work. It's also the kind of thing that, when missed,
makes a reviewer pull on a thread and find broken references —
which makes them lose trust in **everything else** in the
document. So we don't skip it.

## 7.8 What "done" looks like for this phase

A reviewer can read both documents in 20 minutes and finish ready
to discuss the design at ARB **without having read the code
first**. A future maintainer can read both documents and
understand the system well enough to make a non-trivial change.
Every decision is explained, every tradeoff is visible, every
failure mode is accounted for, every claim is verifiable.

If an ARB reviewer asks "why MongoDB and not just
Elasticsearch?", the answer is **literally in the document they
just read**. The review meeting becomes **less** about explaining
the design and more about challenging the choices we already
documented.

That's the bar.

## 7.9 What the system looks like at the end of this phase

The system itself hasn't changed since phase 6. What's changed is
the **presentation**.

Imagine an architect who designed a beautiful building, but
handed the client a stack of unlabeled blueprints and said "just
read these." vs. an architect who walks the client through the
design, explains every tradeoff, and shows them the rationale
behind each decision.

The building is the same. The hand-off is completely different.

Phase 7 is the hand-off.

The next and final phase (phase 8) is the actual review,
release, and hand-off to the team that will run it in production.

---

_Phase 8 in progress..._
