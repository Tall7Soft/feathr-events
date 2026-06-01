# Phase 3 — Core implementation (stakeholder)

> **Audience:** people who don't write code but need to understand
> what we're building, why, and which decisions matter.
> **Technical counterpart:** [`phase_3_implementation_technical.md`](phase_3_implementation_technical.md).
> **Previous phase:** [`phase_2_scaffolding_stakeholder.md`](phase_2_scaffolding_stakeholder.md).

---

## 3.1 Where we are in the project

Phase 2 built the **frame** of the building — rooms laid out, but
nothing functional inside. Phase 3 is when we **furnish each room
and turn the lights on**.

This is the longest phase. There's a lot to build, and the order
matters: we can't put a printer in the back office until the back
office exists; we can't open the front door to customers until
reception can actually answer them.

## 3.2 The order we built things in

We built the system **inside-out**, in this order:

1. **The "rulebook for events"** — what an event looks like and what
   counts as valid.
2. **The mailroom inbox** — where events wait their turn.
3. **The back-office worker** — who reads from the inbox and files
   things.
4. **The archive and the search index** — where things get filed.
5. **The lobby scoreboard** — the live numbers display.
6. **Reception and the velvet rope** — the public-facing endpoints
   plus the bouncer who keeps a single client from flooding the
   system.
7. **The opening-and-closing routine** — making sure all the pieces
   start up and shut down in the right order.

We chose this order because each step assumes the previous one
exists. You can't write the worker until you know what events look
like; you can't open reception until the worker is ready to receive
work.

## 3.3 The rules for what counts as an event

Before anything else, we wrote the **contract**: what fields does an
event need, what's optional, what's outright forbidden?

A few decisions worth calling out:

- **Event types are lowercased automatically.** A partner who sends
  `"PageView"` and a partner who sends `"pageview"` end up with the
  same event type. Reporting doesn't fragment across capitalization.
- **Free-form metadata, capped at 64 keys.** Partners can attach any
  custom data they want — campaign IDs, browser info, anything. But
  we cap the size to prevent a misbehaving partner from sending
  thousands of fields and overloading the search index.
- **Server-generated IDs.** Every event gets a unique ID generated
  by the system, not by the client. This is critical for **not
  losing or duplicating events**: when the worker retries a failed
  save, the unique ID lets the database recognize it as the same
  event.

These small choices reflect a platform-engineering habit: **anticipate
abuse before it happens**. A friendly partner won't send 5,000 keys;
but the system has to assume one might, accidentally or otherwise.

## 3.4 The mailroom inbox and the back-office worker

These two pieces work as a pair, and they're the heart of how the
system handles high volume gracefully.

### The mailroom (the queue)

Events don't get saved to the database the moment they arrive. They
go into a **mailroom inbox** — a list of work to be done. The
person who sent the event gets an instant "received, thanks" and
doesn't have to wait around.

The mailroom is smarter than a simple list:

- **Delayed redelivery.** If a save fails because the database is
  down for a moment, the worker doesn't keep banging on it. The
  event goes back in the inbox **with a delay** — wait 1 second,
  try again. If that fails, wait 2 seconds. If it fails again,
  wait 4. This is called **exponential backoff** and it's how
  well-behaved systems handle transient failures without making
  them worse.

- **Give up after a while.** If after several attempts the event
  still can't be saved, it goes to a separate "dead letter"
  inbox. An operator can investigate later. The main inbox keeps
  moving.

### The back-office worker

The worker is who reads from the inbox and does the actual filing.
We made it smart in a few ways:

- **Multiple workers at once.** Several workers run in parallel
  (configurable) so a flood of events doesn't pile up.
- **Batches of events at a time.** Instead of filing each event one
  by one (slow), the worker collects up to 50 at a time (or
  whatever's in the inbox after 1 second) and files them as a
  batch. This is much faster.
- **Recognizes duplicates.** If the same event somehow arrives
  twice (which can happen with the mailroom's retry-on-failure
  design), the worker recognizes it and only saves it once. The
  count of duplicates is tracked so we can see if it ever becomes
  unusual.

## 3.5 The "search index is secondary" decision in action

In phase 1 we committed to a key architectural choice: **MongoDB is
the source of truth, Elasticsearch is a rebuildable copy**.

In phase 3 this is where that choice gets implemented in code. When
the worker files an event:

1. **First, save to the archive (MongoDB).** If this fails, the
   event isn't lost — it goes back to the inbox for retry.
2. **Then, copy to the search index (Elasticsearch).** If *this*
   fails, the worker **acknowledges the event anyway** and moves on.

Why ack on a partial failure? Because the alternative is worse:

- If we re-queue, the next attempt would try to file it in MongoDB
  again, creating a duplicate.
- If we treat it as a fatal error, a temporary search-index
  outage would back up the entire ingest pipeline.

We accept that the search index might briefly lag behind the
archive. If that happens, we have a documented recovery plan:
**rebuild the search index from the archive**. The archive is the
authoritative copy. This is the kind of "principled tradeoff" the
design brief is asking us to defend at ARB.

## 3.6 The lobby scoreboard

The scoreboard (Redis cache) is one of the simpler pieces but it
hides a clever piece of engineering.

When 50 people walk into the lobby at once and all ask "what's the
current count?", we don't want each of them to trigger a fresh
calculation against the archive. That would multiply the work by
50× for no gain.

Instead, we use a pattern called **single-flight**:

> The first person to ask triggers the calculation. Everyone else who
> asks while that calculation is running just waits for the answer
> the first person is computing. When the calculation finishes, all
> 50 people get the same fresh number.

The result is cached for a few seconds (configurable). After that,
the next person to ask triggers a fresh calculation, and the cycle
repeats.

This pattern means **traffic spikes don't degrade the scoreboard**.
The scoreboard's compute cost is bounded by the cache's refresh
rate, not by how many people are looking at it.

## 3.7 Reception and the velvet rope

The last piece is the **public-facing endpoints**: the URLs that the
outside world calls.

These are deliberately thin. Each endpoint does three things:

1. Check that the request is valid (right shape, right types).
2. Hand off to the right room (the inbox, the archive, the
   scoreboard).
3. Return a response.

No business logic lives in the endpoints themselves. If you want to
change how event types are normalized, you change it in the model
file (the rulebook), not in the endpoint. This keeps the rules in
one place.

### The velvet rope (rate limiting)

In front of the endpoints is a **rate limiter** — a bouncer who
counts requests per minute per IP address and starts saying "no" if
one IP exceeds the limit. The design brief listed this as
secondary scope; we
included it because in any real public-facing system, **one
misbehaving client can ruin the experience for everyone else** if
there's no rate limit.

The bouncer has a sensible default (600 requests per minute per
client) and is configurable for production.

## 3.8 The opening-and-closing routine

When the system starts up, a bunch of things have to happen in the
right order:

1. Read the configuration.
2. Connect to MongoDB, Elasticsearch, Redis.
3. Make sure the database indexes exist (create them if not).
4. Start the back-office workers.
5. Open the front door.

When the system shuts down, the reverse:

1. Stop accepting new requests.
2. Wait for in-flight work to drain.
3. Stop the workers.
4. Close the connections.

This is the kind of housekeeping that's invisible when it's done
right and very damaging when it isn't. A sloppy shutdown can mean
in-flight events are lost; a sloppy startup can mean the system
accepts requests before it's actually ready.

## 3.9 What's not in this phase

This phase intentionally **doesn't include any automated tests**.
Those are phase 4. We built phase 3 with a "test-friendly" shape on
purpose — every room can be tested in isolation, including with
fake versions of the archive, the search index, and the scoreboard.

Phase 4 is where that investment pays off.

## 3.10 What the system looks like at the end of this phase

Imagine the office building from phase 2, now with:

- Phones on every desk.
- Computers in the back office.
- The mailroom inbox installed.
- The scoreboard mounted in the lobby.
- A bouncer at the front door.
- A clear opening-and-closing routine.

You could call the front number, and someone would actually answer.
You could send a fake event, and it would get saved. The lights are
on.

The next phase (phase 4) is when we install all the alarms and
sensors that tell us **the lights stay on**.

---

_Phase 4 in progress..._
