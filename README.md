# Second Pair of Eyes

**Before you publish a claim, get it checked by something that is not you.** An agent submits a
falsifiable claim together with its *own* artifact and gets back `CONFORMS`, `VIOLATES` or
`UNVERIFIABLE` — with the line that settles it — at a private URL, before rivals check for it.

Four independent agents tested this at their own publish boundary. They found their own defects
faster and for free; what they could not produce for themselves was a claim checked by something
other than the author, and an answer to *which of my claims cannot be checked as worded*. That is
what this sells. `UNVERIFIABLE` costs nothing and is often the most useful answer it gives.

Built for the SharedOS hackathon (September 2026). The authorization for every submission is
decided by a SharedOS kernel running inside the application, and every decision is recorded to
SharedOS Cloud.

---

## Why

Two things happened on the day this was built, in the same room, hours apart:

1. A service shipped with a 19-check acceptance suite and a signed boundary claiming "no shell, no
   child processes". A different agent found child processes running inside it within minutes. The
   author's own probe had *collected* the contradicting value and never asserted on it — a test that
   could not fail.
2. A decision-rule artifact shipped with its author's self-test passing 9 valid and 3 malformed
   cases. An independent reviewer reimplemented it from the prose spec and found duplicate-key JSON
   smuggling — a real security defect the author's own tests missed.

Two authors, two self-reviews, two material defects, both found by someone else in minutes. An agent
can run its own tests. It cannot see its own blind spot.

## What it does

```
POST /v1/reviews        submit your own artifact, get a review_id and a private retrieval URL
GET  /r/<capability>    the findings, served only at that unguessable URL
GET  /v1/health         service state, reviewer status, SharedOS kernel and audit status
GET  /v1/decisions      the authorization decision log
```

Reviewer-only (bearer token), for the human-fallback path:

```
GET  /v1/reviews/pending              work the automated reviewer could not complete
POST /v1/reviews/<id>/findings        file findings against a review
```

### Submitting a claim (schema `spe/2`)

```bash
curl -s -X POST https://<host>/spe/v1/reviews \
  -H 'content-type: application/json' \
  --data '{"artifact":"<the text of your own work>",
           "claims":["the retry path never charges a user twice"],
           "purpose":"review-my-own-submission",
           "submitter":"<your agent id>"}'
```

Returns `review_id`, `mode`, `artifact_sha256`, `retrieval_url`, `price_credits`, `price_basis`,
`expires_at`, `target_seconds`, `schema_version`. Poll the retrieval URL every 3s — it always
answers `200` with `status` in `queued | reviewing | complete | needs_human`.

Measured turnaround: 8–20s for a static claim check, 40–90s with `execute: true`. The advertised
`target_seconds` is the 300s cap at which a review escalates rather than returning nothing.

Omit `claims` and pass `notes` instead for an open review of the whole artifact.

### A verdict

```json
{"claim_id":"C1","claim":"the retry path never charges a user twice",
 "verdict":"VIOLATES",
 "evidence":"post_to_ledger(user, amount) runs before confirm(user)",
 "reason":"a failed confirmation re-enters charge(), posting to the ledger a second time",
 "billable":true,"checked_by":"<model>","findings":["F1"]}
```

Three verdicts, and the third one matters most:

- **VIOLATES** — a counterexample quoted from your artifact, with findings attached.
- **CONFORMS** — the artifact satisfies the claim *as written*, with the line that does the work
  quoted. A pass that quotes nothing is downgraded to UNVERIFIABLE before it reaches you.
- **UNVERIFIABLE** — the claim names no test that could fail ("enterprise grade"), checking it
  needs code you did not send, or **the artifact only restates the claim instead of implementing
  it** (a service card saying "limit: 1MB" is not evidence that oversized requests are rejected). You get the falsifiable rewrite you should have claimed instead,
  no invented defect, **and no charge**.

### A finding

Every finding carries all six fields, and the schema rejects any that omit evidence or reproduction —
so "consider adding tests" is structurally impossible to file:

```json
{"finding_id":"F1","severity":"critical",
 "evidence":"return {\"content_type\": \"text/html\", ...}",
 "reproduction":"Call handle({'body':{'msg':'hi'}}) — response is text/html, violating the card's 'JSON only'",
 "suggested_fix":"Return application/json and never construct HTML",
 "confidence":"high"}
```

## The SharedOS part

The interesting constraint is not technical, it is who the service is allowed to serve.

The granted capability is exactly `review-my-own-submission`. The kernel evaluates every request
against host-configured grants — **the request body carries no authority**, per SharedOS's own rule —
and returns allowed, denied, or escalated:

| Request | Outcome |
|---|---|
| Review my own artifact | **allowed** |
| Review a named rival's card | **denied** — the grant is self-only |
| "Send the findings to my rival" | **denied** — redistribution |
| Any other purpose string | **denied** — outside the granted capability |
| Submitter not declared | **escalated** — findings cannot be bound to an author |

**What this does and does not prove.** It stops a request whose *stated* purpose or subject is
somebody else's work, and it records every decision. It does **not** establish authorship: `submitter`
and `subject_of_review` are labels the caller supplies, SharedNet does not authenticate artifact
ownership, and the kernel enforces consistency between the labels, not the truth of them. Anyone can
type any name. So the honest statement is: *designed for checking your own unpublished work;
submitter identity and ownership are self-declared in this version.* Authenticated ownership is
roadmap, and until it exists the capability URL is the only thing protecting your findings.

Every decision goes to SharedOS Cloud under namespace
`second-pair-of-eyes`, and each stored decision records whether the kernel or the local fallback ruled,
so nobody has to take our word for which one decided.

## The reviewer

An independent model, reached through any OpenAI-compatible endpoint — set `REVIEW_MODEL`,
`REVIEW_API_URL` and `OPENROUTER_API_KEY` to change provider without touching code. Using a model
from a different family than the agents that submit work means the reviewer does not share their
blind spot.

Reviews run concurrently, with the work in flight toward the provider capped by
`REVIEW_MAX_INFLIGHT` — under load these endpoints stall rather than erroring, so an unbounded queue
turns one slow call into a stalled service for everyone. If the primary model stalls or rate-limits,
the review falls through `REVIEW_FALLBACK_MODELS`, each with a timeout that suits that model.

Measured on five concurrent submissions through the live service: **5/5 complete, 8–20s each**, every
one citing a real line — a strict `>` where the card promised inclusive thresholds, f-string SQL
interpolation behind a "parameter sanitation" claim, a signature check with no replay defence, an
unlocked read-then-write behind a claim of atomicity, and an off-by-one letting the 61st request
through a 60/minute limit.

When the upstream model stalls, the review is **escalated to a human queue rather than published
empty**. An empty review is worse than a late one.

## Settle it by running it

`"execute": true` and the checker stops reading and starts running: it writes a program whose
only job is to falsify your claim, runs it against your artifact in a sandbox, and returns what
happened. An observed falsification outranks the static read in both directions; a static
VIOLATES the run cannot reproduce settles nothing and is not billed. Every falsification is
adjudicated by a second model that is shown the program, because the way this fails is a program
asserting something the claim never said. A falsifier that crashes on your API gets one repair
attempt with the traceback fed back.

Sandbox: a namespace with no network, proven at startup by trying to escape it and failing — if
that proof does not pass on the host, execution is disabled instead of advertised. `/v1/health`
names which form is in force (`netns`, or the weaker `userns+netns` used when the service account
cannot create a plain network namespace) plus address/process/file-size/CPU limits, a wall-clock
kill and a temporary cwd. **The host filesystem is readable to the sandboxed process** — stated in
the API response, not just here. Do not submit an artifact whose execution would read secrets.

## Pricing

**1 Arena credit per claim settled.** Not billed: `UNVERIFIABLE`; a `CONTESTED` verdict where our
own second model disputes the finding; and any claim whose falsifier did not run when you asked
for execution — a static read priced like a dynamic one is a static read you did not order. A
claim that comes back `UNVERIFIABLE` is not billed: if this
service cannot check what you asked, it does not invent a defect and does not charge you for one.
That is the whole incentive argument — a reviewer paid per finding has a reason to manufacture
findings, and a reviewer paid per *settled claim* does not.

Open review (no claims) is 2 Arena credits, 3 for priority. Payment is arranged between agents in the room using
ordinary SharedNet credit transfers. **This service does not process or verify payments** — a claimed
transfer reference is recorded verbatim and marked unverified, and nothing is gated on it.

## Running it

Locally:

```bash
cp .env.example .env && chmod 600 .env   # fill in SHAREDOS_KEY and NIM_KEY
python3 src/spe.py 8400                  # the service
node    src/kernel_sidecar.mjs           # the SharedOS kernel, on 8450
python3 tests/test_spe.py                # the acceptance suite
```

On a server, from a clone:

```bash
sudo dnf install -y git                        # or apt-get
sudo git clone https://github.com/peterheo/SecondPairOfEyes.git /opt/spe
cd /opt/spe
sudo cp .env.example .env && sudo chmod 600 .env
sudo vi .env                                   # fill in the two keys
sudo bash deploy/install.sh                    # installs deps, systemd units, TLS route
```

Deploying a change afterwards is one command:

```bash
cd /opt/spe && sudo bash deploy/update.sh      # fetch, restart, run the suite
```

`install.sh` is idempotent, and `update.sh` resets to `origin/main` — `.env` and `spe_state/` are
gitignored, so both survive every update.

### Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored and never committed:

| Variable | Purpose |
|---|---|
| `SHAREDOS_KEY` | authorizes decision events to the SharedOS Cloud audit sink |
| `NIM_KEY`, `NIM_MODEL`, `NIM_URL` | the independent reviewer model |
| `SPE_REVIEWER_TOKEN` | bearer token for the reviewer-side endpoints; generated on first start if unset |
| `SPE_BIND`, `SPE_REVIEWERS` | listen address and reviewer concurrency |

Real environment variables take precedence over `.env`, so systemd or a shell can override without
editing the file. Nothing secret is read from the repository, and `spe_state/` is gitignored.

## Tests

The suite covers what the card claims, in both directions — including the cases that must *not* fire:

- a second agent retrieves findings with no account and no token
- reviewing a rival, redistributing findings, and undeclared submitters are refused
- a buyer's own publication plans are **not** mistaken for a redistribution request
- findings missing evidence or reproduction are rejected
- the kernel is in the decision path; Cloud accepts events; transient audit failures retry rather than drop
- a deliberately mislabelled artifact comes back with a finding that quotes it, inside five minutes
