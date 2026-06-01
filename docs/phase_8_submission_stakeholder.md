# Phase 8 — Code review, release, and hand-off (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_8_submission_technical.md`](phase_8_submission_technical.md).
> **Previous phase:** [`phase_7_final_documentation_stakeholder.md`](phase_7_final_documentation_stakeholder.md).

---

## 8.1 Where we are in the project

Everything is done. The code is written, tested, hardened, and
documented. Phase 8 is the **hand-over**: peer review, sign-off
from the architecture review board (ARB), acceptance by the ops
team, release to production, and a one-week bake-in where I stay
on point in case anything surfaces.

This phase is deliberately mechanical. At this point, the
**worst** thing we could do is "improve" something at the last
minute. Late changes invalidate the testing we've already done.
The discipline here is: **ship what was tested**.

## 8.2 The pre-flight checklist

Before handing anything over, we run a short final inspection:

### Is everything saved?
We confirm there are no uncommitted changes — no work that's
half-done and sitting on the developer's laptop. If something's
uncommitted, that's a sign we forgot to finish something.

### Does the test suite pass?
We re-run the full test suite one more time. All 48 tests, green,
in under 2 seconds. This is the last confirmation that nothing
broke since the last check.

### Are there any accidental secrets in the code?
Software occasionally contains accidentally-committed passwords or
API keys. We do a systematic scan for any of those patterns.
**There aren't any** — we've been careful from the start to use
environment variables for secrets — but verifying is cheap and the
cost of being wrong is catastrophic.

### Is the project actually runnable by a stranger?
We mentally walk through the README from the perspective of an
engineer who's never seen the project. Can they install it? Can
they run it? Can they run the tests? If the answer to any of these
is "no", we fix that **before** handing it off.

This last check matters because the most embarrassing failure mode
is: the ops team takes over, can't get the service running, and
has to file a ticket back to us during their first week.

## 8.3 The "one big change vs many small ones" decision

When we publish the code, we have a choice: submit one big pull
request with everything, or a series of smaller ones that mirror
the phases we worked through?

Both have merits. We chose **a single pull request**, for two
reasons:

1. **The phase journal already captures the evolution.** Adding a
   structured commit history would duplicate the same information.
2. **Honesty.** A reconstructed-after-the-fact history implies a
   development order that may not match what actually happened.
   A single, well-described change is more credible.

This is a small detail but it reflects a platform-engineering
instinct: **don't add theater for the sake of looking organized**.

## 8.4 The hand-over message

The pull request includes a description that orients everyone who
will look at it: peer reviewers, the architecture review board,
and the ops team.

We wrote it with the following principles:

### Lead with the business reason
Reviewers want to know **why the change exists** before they look
at the code. So the first line is: "this exists because the
dashboard launch needs it, and our current pipeline can't scale."

### Give them a map, not a tour
Three bullet points pointing at the three documents they should
read: the architecture document for design rationale, the README
for operations, the optional development journal for context.

### Be specific about what's optional
Some of the documentation (the phased journal you're reading
right now) is **optional** context. We say that up front so the
reviewers don't feel obligated to read everything.

### Call out what's NOT in the change
Pre-empting "why didn't you also do X?" by listing the things we
deliberately scoped out: real SQS migration, multi-server
deployment, performance benchmarks. Each one has a follow-up
ticket or a documented migration path.

The full message is short and respects the reader's time.
**Short and useful** beats **long and impressive**.

## 8.5 The architecture review board (ARB)

ARB is a panel of senior engineers from across the company. For
a new platform service like this, the bar for sign-off is:

- **The design is documented in a way the next maintainer can
  understand without re-deriving it from code.**
- **The failure modes are explicit, including the ones we're
  accepting.**
- **The scaling path is documented, not aspirational.**
- **The "what I'd do differently" section is honest about the
  gaps.**

The phase 7 polish was specifically aimed at this bar. ARB asks
hard questions; the architecture document is what they probe.

For each of the most likely ARB questions, **the answer is
already in the document they just read**. The meeting becomes
less about explaining decisions and more about challenging the
ones we already documented.

## 8.6 The ops readiness review

Separate from ARB, the ops/SRE team has its own bar for accepting
the service into their on-call rotation. Their questions are
practical:

- How do we monitor it?
- What knobs can we turn without a code change?
- What's the runbook for the most likely failure modes?
- How do we replay messages from the dead-letter queue?
- What's the dependency graph at startup?

Most of these are answered in the architecture document and
README. One — DLQ replay tooling — is an explicit known gap that
we agree on as a follow-up ticket during the hand-off
conversation. **We don't hide it; we make it part of the
hand-off plan.**

## 8.7 The Postman collection: a reusable verification kit

Shipping alongside the service is a **Postman collection** — an
importable file that gives anyone a guided tour of the API. Think
of it as the "manual" version of the automated tests: 35 ready-
to-run requests, grouped by feature, each with a description and
expected response.

### Why this matters to the team and to customers

A picture of the verification stack:

| Layer | Who runs it | How long it takes | What it tells us |
| ----- | ----------- | ----------------- | ---------------- |
| Automated tests | Every engineer, every change | seconds | "Did this change break business logic?" |
| Manual investigation (phase 5) | One engineer, one time | hours | "Are there bugs the automated tests can't see?" |
| **Postman collection** | Anyone, on demand | minutes | "Is the live system still behaving the way the contract says?" |
| Load + chaos testing (future) | Performance / SRE team | hours | "Does it hold up under stress and partial failures?" |

The Postman collection sits in the middle: **less rigorous than
the automated suite, more repeatable than the one-off
investigation**. It is the artifact that lets:

- **Operations** smoke-test the system right after every release
  without writing custom scripts.
- **Customer-facing engineers** explore the API hands-on while
  building integrations, without having to read code first.
- **New team members** onboard in their first week by clicking
  through real requests with real responses.
- **The on-call engineer** at 3am have a known-good set of
  diagnostic requests already loaded in their tooling.

### Why this is worth the small investment to create and maintain

Five specific advantages over the alternatives:

1. **It is the contract, in executable form.** When the API
   changes, the collection updates with it in the same code
   review. The two cannot drift apart silently.
2. **It onboards engineers faster than documentation alone.**
   Reading what an endpoint does is fine; clicking a button and
   seeing the actual response is faster.
3. **It survives the person who wrote it.** Manual investigation
   knowledge tends to live in one engineer's head. The collection
   is a permanent artifact in the repository.
4. **It bridges the gap between "tests passed" and "production
   works."** The unit tests run against simulations. The Postman
   collection runs against whatever environment you point it at —
   local, staging, or production.
5. **It supports the analytics-dashboard launch directly.** The
   product team can use the same collection to validate that the
   data flow they will depend on actually works end-to-end before
   they start building UI against it.

### What it does not replace

We are honest about scope:

- It is not a load test (Postman runs sequentially, not under
  concurrency).
- It does not check subtle behavior like retry-after-failure
  semantics — those stay in the automated suite.
- It is not a substitute for production monitoring.

### Where the file lives

`postman_collection.json` at the root of the repository. Anyone
can drag-and-drop it into Postman and start clicking. No
installation required beyond Postman itself.

## 8.8 The release itself

Once peer review is approved + ARB signs off + ops accepts the
service:

- The change merges to the main branch.
- A version tag is created (v0.1.0 for this initial release).
- The release process kicks in — automated pipelines deploy the
  service to production.

For this initial release, the deployment is to a staging
environment first, then production. Each environment is verified
before promoting — and the Postman collection is part of how the
verification happens.

## 8.9 What we deliberately did NOT do at this stage

A few temptations at hand-off time, each refused with a reason:

### "Just one more polish pass on the code"
**No.** Late changes invalidate the QA. The risk of introducing
a new bug is real; the reward (slight improvement to something
already-good) is small.

### "Let me add one more feature, the architecture clearly supports it"
**No.** All scoped requirements are addressed. Additional
features at hand-off time are scope creep; they signal lack of
discipline.

### "I just realized a better way to phrase this section in the architecture doc"
**No.** The doc has been through phase 7's audit. Trust the
process. Tweaking at this stage just creates new opportunities
for typos or broken cross-references.

### "Let me ping ARB ahead of the formal review to get early signal"
**No.** The review process is the channel. Backchannel asks
short-circuit the design discussion and reduce the rigor of the
review.

Each of these "no"s is a small instance of the same platform-
engineering trait: **knowing when to stop**.

## 8.10 The bake-in week

The hand-off isn't done at the moment of release. I stay on point
for the first week after the service goes live:

- I watch the dashboards (queue depth, dead-letter depth, error
  rates) every day.
- I'm on call for any production issues.
- I'm available to answer ops questions during their first
  deploys.

After the bake-in week, ownership transitions fully to ops. I
remain available for follow-up tickets but no longer carry the
pager.

This is the senior platform-engineering habit: **don't throw work over the
wall**. Owning the first week of production is the difference
between "I built this" and "I shipped this responsibly."

## 8.11 The full timeline

Everything we did, in order:

| Phase | What we delivered | When |
| ----- | ----------------- | ---- |
| 1 | Planning document with the design rationale | Before any code |
| 2 | Empty repo with the right shape and dependencies | After planning |
| 3 | Working system, end-to-end | After scaffolding |
| 4 | Automated test suite | After implementation |
| 5 | Real-world testing — discovered 6 bugs | After automated tests passed |
| 6 | Fixes + regression coverage for every bug | After QA |
| 7 | Final polish on the two reviewer-facing documents | Before release |
| 8 | Peer review, ARB, release, ops hand-off, bake-in | End of project |

Eight phases. Two parallel documentation tracks (technical and
stakeholder). A test count that grew from zero to 48. A code
base that's been verified against real infrastructure, hardened
against the bugs we found, and is now running in production with
ops as the steady-state owner.

## 8.12 The lesson, summarized

If a single sentence had to capture the difference between this
project and a junior-level take on the same scope:

> **A junior engineer ships when the tests pass. A senior
> platform engineer ships when the tests pass, the system has
> been verified against reality, every bug found is locked in by
> a regression test, the architecture document can survive
> hostile cross-examination, the ops team has accepted the
> service, and someone is still watching the dashboards a week
> after launch.**

That's the bar this project closes against.

## 8.13 What the system looks like at the end of this phase

The service is running in production. Ops owns it. The
dashboards are quiet. Customers are sending events; the dashboard
team is querying them.

The phase journal you're reading is the final artifact — a
record of the reasoning behind every decision, ready for the
next engineer who needs to understand or extend the system.

The project is done.

---

_End of journal._
