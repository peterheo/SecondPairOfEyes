# Architecture

```
 submitter agent                  this application                     SharedOS Cloud
 ---------------                  ----------------                     --------------
      |  POST /v1/reviews              |                                     |
      |------------------------------->|                                     |
      |                                |  authorize(purpose, submitter)      |
      |                                |------------> kernel sidecar         |
      |                                |<------------ allowed/denied/escalated
      |                                |  decision event --------------------->  /v1/audit/events
      |  201 {retrieval_url}           |                                     |
      |<-------------------------------|                                     |
      |                                |  queue -> reviewer pool             |
      |                                |------------> independent model      |
      |                                |<------------ findings (validated)   |
      |  GET /r/<capability>           |                                     |
      |------------------------------->|                                     |
      |  findings                      |                                     |
      |<-------------------------------|                                     |
```

## Components

**`src/spe.py`** — the service. Accepts submissions, enforces the finding schema, serves capability
URLs, runs the reviewer pool, and ships decision events to Cloud on a background thread with retry.

**`src/kernel_sidecar.mjs`** — the SharedOS kernel, running inside the application. Grants come from
host configuration; the request body carries no authority. Loads `@aicoo/sharedos` when the registry
allows it and otherwise evaluates the same grant contract directly — the health endpoint reports
which, rather than claiming the SDK is present.

## Decisions worth knowing about

**The kernel decides; the local rules are a labelled fallback.** If the sidecar is unreachable the
service keeps working, and every stored decision records `decided_by: kernel | local-fallback`. A
degraded mode that lies about which one ruled would be worse than an outage.

**Audit events retry rather than drop.** A hole in an audit trail is a defect, not a blip. Delivery
is best effort on a background thread so the console can never gate a user request, with backoff and
counters for retried and dropped.

**An empty review is never published.** When the upstream model stalls, the job escalates to a human
queue. Latency is recoverable; a confident empty answer is not.

**Reviews run concurrently.** One slow upstream call must not block every other buyer in a market
window.

**The redistribution guard distinguishes our output from the buyer's plans.** "Self-check before I
publish this" is the *normal* reason to buy a review and must be allowed; "publish the findings in
the room" must not. An early version denied the first, which is a guardrail failing its own customer.
