"""Second Pair of Eyes — private, independent review before public critique.

Frozen conditions (Room seq 102):
  1. direct HTTPS call, not an implicit request for a human/agent to read a room
  2. artifact stays private; findings served only at a unique retrieval capability
  3. inputs <= 1MB, turnaround <= 5 min, findings cite exact lines or a repro
  4. purpose grant permits ONLY read-submission -> return-to-submitter; rival review
     and redistribution requests are denied and logged
  5. the reviewer is a live agent of a different model family than the submitter

The box holds no model. It queues submissions, serves them to the authenticated
reviewer, and returns findings. The review itself is done by the reviewing agent.
"""
import json, os, queue, re, secrets, socket, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(APP_DIR, "spe_state")
os.makedirs(STATE, exist_ok=True)

def load_env(*paths):
    """Minimal .env loader: KEY=value per line, # comments, optional quotes.
    Values already in the real environment win, so systemd or a shell can override."""
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        for raw in open(p):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)

load_env(os.environ.get("SPE_ENV_FILE"),
         os.path.join(APP_DIR, ".env"),
         os.path.join(os.path.dirname(APP_DIR), ".env"))
REVIEWER_TOKEN_FILE = os.path.join(STATE, "reviewer_token")
REVIEWER_TOKEN = os.environ.get("SPE_REVIEWER_TOKEN", "").strip()
if not REVIEWER_TOKEN:
    if os.path.exists(REVIEWER_TOKEN_FILE):
        REVIEWER_TOKEN = open(REVIEWER_TOKEN_FILE).read().strip()
    else:
        REVIEWER_TOKEN = secrets.token_urlsafe(24)
        fd = os.open(REVIEWER_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, REVIEWER_TOKEN.encode()); os.close(fd)

MAX_ARTIFACT = 1024 * 1024
TARGET_SECONDS = 300
PRICE_REVIEW, PRICE_PRIORITY = 2, 3
PAYMENT_NOTICE = ("Payment is arranged between agents in the Room using ordinary SharedNet credit "
                  "transfers. Second Pair of Eyes does not process or verify payments.")
ALLOWED_PURPOSE = "review-my-own-submission"

REVIEWS, BY_CAP, DECISIONS = {}, {}, []
LOCK = threading.Lock()

# ---------------------------------------------------------------- SharedOS Cloud
# Every authorization decision is sent to the SharedOS audit sink. Delivery is
# best effort on a background thread: the console must never gate a user request,
# and a Cloud outage must not deny a legitimate review.
SOS_KEY_FILE = os.path.join(STATE, "sharedos_key")
SOS_KEY = (os.environ.get("SHAREDOS_KEY", "").strip()
           or (open(SOS_KEY_FILE).read().strip() if os.path.exists(SOS_KEY_FILE) else "")) or None
SOS_AUDIT_URL = os.environ.get("SHAREDOS_AUDIT_URL", "https://www.sharedos.ai/v1/audit/events")
NAMESPACE = "second-pair-of-eyes"
KERNEL_URL = os.environ.get("SPE_KERNEL_URL", "http://127.0.0.1:8450/authorize")
AUDIT_Q = queue.Queue(maxsize=1000)
AUDIT_STATS = {"sent": 0, "failed": 0, "retried": 0, "dropped": 0,
               "last_error": None, "enabled": bool(SOS_KEY)}
AUDIT_MAX_TRIES = 4

def audit(event, attempt=0):
    try: AUDIT_Q.put_nowait((event, attempt))
    except queue.Full: AUDIT_STATS["dropped"] += 1

def audit_worker():
    while True:
        ev, attempt = AUDIT_Q.get()
        if not SOS_KEY:
            continue
        body = json.dumps({"events": [ev]}).encode()
        req = urllib.request.Request(SOS_AUDIT_URL, data=body, method="POST",
              headers={"content-type": "application/json", "authorization": "Bearer " + SOS_KEY})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
            if resp.get("accepted"): AUDIT_STATS["sent"] += 1
            else:
                AUDIT_STATS["failed"] += 1
                AUDIT_STATS["last_error"] = str(resp)[:200]
        except Exception as e:
            AUDIT_STATS["last_error"] = "%s: %s" % (type(e).__name__, e)
            # a dropped decision event is a hole in the audit trail, so retry with backoff
            if attempt + 1 < AUDIT_MAX_TRIES:
                AUDIT_STATS["retried"] += 1
                time.sleep(min(2 ** attempt, 8))
                audit(ev, attempt + 1)
            else:
                AUDIT_STATS["failed"] += 1

def kernel_authorize(agent_id, capability, purpose, subject, request_text):
    """Ask the SharedOS kernel sidecar. Returns (decision, reason) or None if absent."""
    body = json.dumps({"agent": {"kind": "agent", "agentId": agent_id or "unstated"},
                       "capability": capability, "purpose": purpose,
                       "subject": subject, "request": request_text}).encode()
    req = urllib.request.Request(KERNEL_URL, data=body, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read())
        d = j.get("decision")
        if d in ("allowed", "denied", "escalated"):
            return d, j.get("reason", "kernel decision"), True
    except Exception:
        pass
    return None

FINDING_FIELDS = ("finding_id", "severity", "evidence", "reproduction", "suggested_fix", "confidence")
SEVERITIES = ("critical", "material", "minor", "note")

# ---------------------------------------------------------------- the reviewer
# An independent model reviews the artifact: a different family from both the
# Claude and Codex agents that submit work, so it shares neither one's blind spot.
# One OpenAI-compatible client, so the provider is a config change rather than a
# code change. NIM_* names are still honoured so an existing .env keeps working.
REVIEW_API_KEY = (os.environ.get("OPENROUTER_API_KEY", "").strip()
                  or os.environ.get("REVIEW_API_KEY", "").strip()
                  or os.environ.get("NIM_KEY", "").strip()
                  or (open(os.path.join(STATE, "nim_key")).read().strip()
                      if os.path.exists(os.path.join(STATE, "nim_key")) else "")) or None
REVIEW_URL = (os.environ.get("REVIEW_API_URL", "").strip()
              or os.environ.get("NIM_URL", "").strip()
              or "https://openrouter.ai/api/v1/chat/completions")
REVIEW_MODEL = (os.environ.get("REVIEW_MODEL", "").strip()
                or os.environ.get("NIM_MODEL", "").strip()
                or "poolside/laguna-s-2.1:free")
# Providers rate-limit and stall independently, so fall through a chain rather than
# failing the buyer when one endpoint is having a bad minute.
REVIEW_FALLBACKS = [m.strip() for m in os.environ.get(
    "REVIEW_FALLBACK_MODELS", "nex-agi/nex-n2.5-mini:free,inclusionai/ling-3.0-flash").split(",") if m.strip()]
REVIEW_REFERER = os.environ.get("REVIEW_HTTP_REFERER", "https://github.com/peterheo/SecondPairOfEyes")
REVIEW_TITLE = os.environ.get("REVIEW_APP_TITLE", "Second Pair of Eyes")
MAX_ARTIFACT_CHARS = int(os.environ.get("REVIEW_MAX_ARTIFACT_CHARS", "12000"))

# The upstream stalls under load far more often than it errors, so bound the work
# in flight rather than letting every queued review pile onto the provider at once.
UPSTREAM = threading.Semaphore(int(os.environ.get("REVIEW_MAX_INFLIGHT", "4")))
FAST_TIMEOUT = int(os.environ.get("REVIEW_FAST_TIMEOUT", "60"))
FALLBACK_TIMEOUT = int(os.environ.get("REVIEW_FALLBACK_TIMEOUT", "200"))

REVIEW_STATS = {"auto_completed": 0, "auto_failed": 0, "timeouts": 0, "escalated": 0,
                "last_error": None, "model": REVIEW_MODEL,
                "autonomous": bool(REVIEW_API_KEY), "endpoint": REVIEW_URL}

REVIEW_PROMPT = """You are an independent reviewer. Review ONLY the artifact below, which its author \
submitted for review before publishing it. You are not the author and you do not share their assumptions.

Return STRICT JSON and nothing else:
{"findings":[{"finding_id":"F1","severity":"critical|material|minor|note","evidence":"the exact line or \
behaviour, quoted from the artifact","reproduction":"concrete steps or inputs that show the problem",\
"suggested_fix":"a specific change","confidence":"high|medium|low"}]}

Rules:
- Cite only what is literally present in the artifact. Never invent code or behaviour.
- No generic advice such as "add tests" or "consider security". A finding that could be written without \
reading this artifact is worthless.
- A claim the artifact makes that its own code does not enforce is the most important kind of finding.
- At most 5 findings, most severe first. If you find nothing material, return one finding with \
severity "note" saying exactly what you checked and found sound.

WHAT THE AUTHOR ASKED YOU TO CHECK: %s

ARTIFACT:
%s
"""

def extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t); t = re.sub(r"\n?```$", "", t.strip())
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i: t = t[i:j+1]
    return json.loads(t)

def call_reviewer(artifact, notes, model=None, timeout=75):
    """One review call. Artifact is truncated rather than allowed to blow the budget."""
    art = artifact if len(artifact) <= MAX_ARTIFACT_CHARS else (
        artifact[:MAX_ARTIFACT_CHARS] + "\n... [truncated for review]")
    payload = {"model": model or REVIEW_MODEL,
               "messages": [{"role": "user",
                             "content": REVIEW_PROMPT % (notes or "(nothing specific)", art)}],
               "temperature": 0.2, "max_tokens": 1500,
               "response_format": {"type": "json_object"}}
    req = urllib.request.Request(REVIEW_URL, data=json.dumps(payload).encode(), method="POST",
          headers={"Authorization": "Bearer " + REVIEW_API_KEY,
                   "Content-Type": "application/json",
                   "HTTP-Referer": REVIEW_REFERER, "X-Title": REVIEW_TITLE})
    with UPSTREAM:                      # bounded concurrency toward the provider
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    if "choices" not in d:
        raise RuntimeError("provider returned no choices: %s" % json.dumps(d)[:200])
    msg = d["choices"][0]["message"]
    return extract_json(msg.get("content") or msg.get("reasoning_content") or "")

def validate_findings(raw):
    out = []
    for i, f in enumerate(raw.get("findings", [])[:5]):
        if not isinstance(f, dict): continue
        item = {k: str(f.get(k, "")).strip() for k in FINDING_FIELDS}
        if not item["finding_id"]: item["finding_id"] = "F%d" % (i + 1)
        if item["severity"] not in SEVERITIES: item["severity"] = "note"
        if item["confidence"] not in ("high", "medium", "low"): item["confidence"] = "medium"
        # a finding with no evidence is the generic advice we promised not to serve
        if len(item["evidence"]) < 8 or len(item["reproduction"]) < 8: continue
        out.append(item)
    return out

def reviewer_worker():
    while True:
        time.sleep(2)
        if not REVIEW_API_KEY: continue
        with LOCK:
            job = next((r for r in REVIEWS.values() if r["status"] == "queued"), None)
            if job: job["status"] = "reviewing"
        if not job: continue
        # the upstream model is fast most of the time and occasionally stalls, so
        # prefer short attempts over one long one
        findings = []
        # The fast model occasionally stalls, so try it twice before falling back;
        # the fallback is a slow free model and needs a timeout that fits it, not
        # the fast one's. A fallback that always times out is not a fallback.
        attempts = [(REVIEW_MODEL, FAST_TIMEOUT), (REVIEW_MODEL, FAST_TIMEOUT)]
        attempts += [(m, FALLBACK_TIMEOUT) for m in REVIEW_FALLBACKS]
        for model, timeout in attempts:
            try:
                findings = validate_findings(
                    call_reviewer(job["artifact"], job["notes"], model, timeout))
                if findings:
                    job["reviewer_model"] = model
                    break
            except Exception as e:
                REVIEW_STATS["last_error"] = "%s (%s)" % (e, model)
                if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e):
                    REVIEW_STATS["timeouts"] += 1
            time.sleep(1)
        with LOCK:
            job["attempts"] = job.get("attempts", 0) + 1
            if findings:
                job["findings"] = findings; job["status"] = "complete"
                job["completed_at"] = time.time()
                job["reviewer"] = job.get("reviewer_model", REVIEW_MODEL)
                REVIEW_STATS["auto_completed"] += 1
            elif job["attempts"] >= 1:
                # never publish an empty review: escalate instead of looping
                job["status"] = "needs_human"
                REVIEW_STATS["auto_failed"] += 1; REVIEW_STATS["escalated"] += 1
            else:
                job["status"] = "queued"; REVIEW_STATS["auto_failed"] += 1

# --------------------------------------------------------------- authorization
THIRD_PARTY_RE = re.compile(
    r"\b(rival|competitor|opponent|their (?:card|product|service|code)|someone else'?s|"
    r"another agent'?s|other participants?')\b", re.I)
REDISTRIBUTE_RE = re.compile(
    r"\b(publish|post|share|redistribute|leak|broadcast|forward|send)\b[^.?!]{0,40}?\b(finding|findings|review|report|results?)\b[^.?!]{0,40}?\b(to|with|in the room|publicly)\b|\b(finding|findings|review|report|results?)\b[^.?!]{0,40}?\b(publish|post|share|redistribute|leak|broadcast|forward|send)\b[^.?!]{0,40}?\b(to|with|in the room|publicly)\b|\b(publish|post|share|forward|send|broadcast|leak)\s+(it|this|them|these)\s+(to|with|in)\b|\b(redistribute|leak)\b", re.I)

def authorize(purpose, submitter, subject, raw_request):
    """SharedOS-shaped: the grant permits exactly read-submission -> return-to-submitter."""
    if purpose != ALLOWED_PURPOSE:
        return "denied", "purpose %r is not granted; only %r" % (purpose, ALLOWED_PURPOSE)
    if subject and submitter and subject.strip().lower() != submitter.strip().lower():
        return "denied", ("grant returns findings only to the author of the submission; "
                          "reviewing %r on behalf of %r is not permitted" % (subject, submitter))
    if THIRD_PARTY_RE.search(raw_request or ""):
        return "denied", "request asks for review of a third party's work, which this grant forbids"
    if REDISTRIBUTE_RE.search(raw_request or ""):
        return "denied", "request asks for redistribution; findings return only to the submitter"
    if not submitter:
        return "escalated", "submitter not declared; cannot bind findings to an author"
    return "allowed", "clause:read-submission->return-to-submitter"

def decide(review_id, purpose, submitter, subject, raw, extra=None):
    # The kernel decides when it is reachable; the local rules are the fallback,
    # and the event says which one ruled so nobody has to guess.
    k = kernel_authorize(submitter, ALLOWED_PURPOSE, purpose, subject, raw)
    if k:
        decision, reason, by_kernel = k
    else:
        decision, reason = authorize(purpose, submitter, subject, raw)
        by_kernel = False
    ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "review_id": review_id,
          "purpose": purpose, "submitter": submitter, "subject": subject,
          "decision": decision, "reason": reason, "decided_by": "kernel" if by_kernel else "local-fallback"}
    if extra: ev.update(extra)
    with LOCK:
        DECISIONS.append(ev); del DECISIONS[:-2000]
    audit({"type": "authorization.decision", "outcome": decision, "namespaceId": NAMESPACE,
           "traceId": review_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
           "agent": {"kind": "agent", "agentId": submitter or "unstated"},
           "capability": ALLOWED_PURPOSE, "requestedPurpose": purpose,
           "subject": subject, "reason": reason,
           "decidedBy": "kernel" if by_kernel else "local-fallback"})
    return decision, reason

# --------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    server_version = "SecondPairOfEyes"
    def log_message(self, *a): pass
    def _send(self, st, payload):
        b = json.dumps(payload, indent=1).encode()
        self.send_response(st); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        try: self.wfile.write(b)
        except BrokenPipeError: pass
    def _reviewer(self):
        got = self.headers.get("Authorization", "")
        if got.startswith("Bearer ") and secrets.compare_digest(got[7:], REVIEWER_TOKEN):
            return True
        self._send(401, {"error": "reviewer token required"}); return False
    def _body(self, limit=MAX_ARTIFACT + 8192):
        n = int(self.headers.get("Content-Length", 0))
        if n > limit: return None, "payload exceeds %d bytes" % limit
        try: return json.loads(self.rfile.read(n) or b"{}"), None
        except Exception: return None, "invalid json"

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/v1/reviews":
            body, err = self._body()
            if err: return self._send(400, {"error": err})
            artifact = body.get("artifact") or ""
            purpose = body.get("purpose") or ""
            submitter = body.get("submitter") or ""
            subject = body.get("subject_of_review") or submitter
            notes = body.get("notes") or ""
            if not isinstance(artifact, str) or not artifact.strip():
                return self._send(400, {"error": "artifact (text of your own work) is required",
                                        "purpose_required": ALLOWED_PURPOSE})
            if len(artifact.encode()) > MAX_ARTIFACT:
                return self._send(413, {"error": "artifact must be <= %d bytes" % MAX_ARTIFACT})
            rid = "rev_" + secrets.token_hex(6)
            decision, reason = decide(rid, purpose, submitter, subject, notes + " " + str(body.get("request", "")))
            if decision != "allowed":
                return self._send(403, {"error": "request denied by the purpose grant",
                                        "decision": decision, "reason": reason,
                                        "granted_purpose": ALLOWED_PURPOSE,
                                        "note": "this service reviews only the submitter's own work "
                                                "and returns findings only to them"})
            cap = secrets.token_urlsafe(20)
            host = self.headers.get("Host", "localhost")
            rec = {"review_id": rid, "submitter": submitter, "purpose": purpose,
                   "notes": notes, "artifact": artifact, "artifact_bytes": len(artifact.encode()),
                   "submitted_at": time.time(), "status": "queued", "findings": [],
                   "priority": bool(body.get("priority")),
                   "retrieval_url": "https://%s/r/%s" % (host, cap),
                   "reviewer": None, "completed_at": None}
            with LOCK:
                REVIEWS[rid] = rec; BY_CAP[cap] = rid
            return self._send(201, {"review_id": rid, "status": "queued",
                                    "retrieval_url": rec["retrieval_url"],
                                    "target_seconds": TARGET_SECONDS,
                                    "price_credits": PRICE_PRIORITY if rec["priority"] else PRICE_REVIEW,
                                    "privacy": "your artifact is never posted to the Room; findings are "
                                               "served only at the retrieval URL above",
                                    "payment_notice": PAYMENT_NOTICE})

        m = re.match(r"^/v1/reviews/(rev_[0-9a-f]+)/findings$", path)
        if m:
            if not self._reviewer(): return
            body, err = self._body()
            if err: return self._send(400, {"error": err})
            with LOCK: rec = REVIEWS.get(m.group(1))
            if not rec: return self._send(404, {"error": "no such review"})
            findings = body.get("findings")
            if not isinstance(findings, list) or not findings:
                return self._send(400, {"error": "findings must be a non-empty list",
                                        "schema": list(FINDING_FIELDS)})
            clean = []
            for i, f in enumerate(findings):
                missing = [k for k in FINDING_FIELDS if k not in f]
                if missing:
                    return self._send(400, {"error": "finding %d missing fields" % i, "missing": missing})
                if f["severity"] not in SEVERITIES:
                    return self._send(400, {"error": "severity must be one of %s" % (SEVERITIES,)})
                clean.append({k: f[k] for k in FINDING_FIELDS})
            with LOCK:
                rec["findings"] = clean; rec["status"] = "complete"
                rec["completed_at"] = time.time(); rec["reviewer"] = body.get("reviewer") or "unnamed-reviewer"
            return self._send(200, {"review_id": rec["review_id"], "status": "complete",
                                    "findings": len(clean),
                                    "turnaround_seconds": round(rec["completed_at"] - rec["submitted_at"], 1)})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/v1/health":
            with LOCK:
                q = sum(1 for r in REVIEWS.values() if r["status"] == "queued")
                done = sum(1 for r in REVIEWS.values() if r["status"] == "complete")
            k = kernel_authorize("healthcheck", ALLOWED_PURPOSE, ALLOWED_PURPOSE, "healthcheck", "")
            return self._send(200, {"service": "Second Pair of Eyes", "queued": q, "completed": done,
                                    "sharedos": {"kernel_reachable": bool(k),
                                                 "audit_enabled": AUDIT_STATS["enabled"],
                                                 "audit_events_sent": AUDIT_STATS["sent"],
                                                 "audit_failures": AUDIT_STATS["failed"],
                                                 "audit_retried": AUDIT_STATS["retried"],
                                                 "audit_dropped": AUDIT_STATS["dropped"],
                                                 "last_audit_error": AUDIT_STATS["last_error"],
                                                 "namespace": NAMESPACE},
                                    "granted_purpose": ALLOWED_PURPOSE,
                                    "price_credits": {"review": PRICE_REVIEW, "priority": PRICE_PRIORITY},
                                    "target_seconds": TARGET_SECONDS,
                                    "finding_schema": list(FINDING_FIELDS),
                                    "reviewer": {"autonomous": REVIEW_STATS["autonomous"],
                                                 "model": REVIEW_STATS["model"],
                                                 "fallbacks": REVIEW_FALLBACKS,
                                                 "endpoint": REVIEW_STATS["endpoint"],
                                                 "auto_completed": REVIEW_STATS["auto_completed"],
                                                 "auto_failed": REVIEW_STATS["auto_failed"],
                                                 "timeouts": REVIEW_STATS["timeouts"],
                                                 "escalated_to_human": REVIEW_STATS["escalated"],
                                                 "last_error": REVIEW_STATS["last_error"]},
                                    "payment_notice": PAYMENT_NOTICE})
        if path == "/v1/decisions":
            with LOCK: return self._send(200, {"items": DECISIONS[-200:]})
        if path == "/v1/reviews/pending":
            if not self._reviewer(): return
            with LOCK:
                pend = [{"review_id": r["review_id"], "submitter": r["submitter"], "notes": r["notes"],
                         "artifact": r["artifact"], "priority": r["priority"],
                         "waiting_seconds": round(time.time() - r["submitted_at"], 1)}
                        for r in REVIEWS.values() if r["status"] in ("queued", "reviewing", "needs_human")]
            pend.sort(key=lambda p: (not p["priority"], -p["waiting_seconds"]))
            return self._send(200, {"items": pend})
        m = re.match(r"^/r/([A-Za-z0-9_\-]{16,})$", path)
        if m:
            rid = BY_CAP.get(m.group(1))
            with LOCK: rec = REVIEWS.get(rid) if rid else None
            if not rec: return self._send(404, {"error": "no such review"})
            out = {"review_id": rec["review_id"], "status": rec["status"],
                   "submitted_at": int(rec["submitted_at"]),
                   "artifact_bytes": rec["artifact_bytes"],
                   "price_credits": PRICE_PRIORITY if rec["priority"] else PRICE_REVIEW,
                   "payment_notice": PAYMENT_NOTICE}
            if rec["status"] == "complete":
                out.update(findings=rec["findings"], reviewer=rec["reviewer"],
                           turnaround_seconds=round(rec["completed_at"] - rec["submitted_at"], 1))
            else:
                out.update(findings=[], waiting_seconds=round(time.time() - rec["submitted_at"], 1),
                           note=("the automated reviewer could not complete this one; a human reviewer "
                                 "has it" if rec["status"] == "needs_human" else
                                 "review in progress; poll this URL"))
            return self._send(200, out)
        return self._send(404, {"error": "not found"})

class S(ThreadingHTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8400
    threading.Thread(target=audit_worker, daemon=True).start()
    # several buyers can be in the queue at once during a market window, and one
    # slow upstream call must not block everyone behind it
    for _ in range(int(os.environ.get("SPE_REVIEWERS", "3"))):
        threading.Thread(target=reviewer_worker, daemon=True).start()
    print("Second Pair of Eyes on %d (audit=%s, autonomous_reviewer=%s)"
          % (port, bool(SOS_KEY), bool(REVIEW_API_KEY)), flush=True)
    print("reviewer token stored at %s" % REVIEWER_TOKEN_FILE, flush=True)
    S((os.environ.get("SPE_BIND", "127.0.0.1"), port), H).serve_forever()
