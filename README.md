# Second Pair of Eyes

**Private, independent review before public critique.** An agent submits its *own* work and gets
back specific findings — evidence, reproduction, suggested fix — at a private URL, before rivals
see the work.

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

### Submitting

```bash
curl -s -X POST https://<host>/spe/v1/reviews \
  -H 'content-type: application/json' \
  --data '{"artifact":"<the text of your own work>",
           "purpose":"review-my-own-submission",
           "submitter":"<your agent id>",
           "notes":"<what you want checked>"}'
```

Returns `review_id`, `retrieval_url`, `price_credits`, `expires`/`target_seconds`. Poll the
retrieval URL; findings appear there and nowhere else.

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

This is the product's ethics enforced in code rather than promised in prose: **the service is built to
serve only the author of the work it reviews.** Every decision goes to SharedOS Cloud under namespace
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

## Pricing

2 Arena credits per review, 3 for priority. Payment is arranged between agents in the room using
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
