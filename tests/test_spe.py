"""Acceptance tests for the five conditions frozen in Room seq 102."""
import json, time, urllib.request, urllib.error
B="http://127.0.0.1:8400"
def _reviewer_token():
    """Same precedence as the service: .env, then the generated file."""
    import os
    for p in (".env", "/opt/spe/.env"):
        if os.path.exists(p):
            for line in open(p):
                if line.strip().startswith("SPE_REVIEWER_TOKEN="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v: return v
    for p in ("spe_state/reviewer_token", "/opt/spe/spe_state/reviewer_token"):
        if os.path.exists(p): return open(p).read().strip()
    raise SystemExit("no reviewer token found")
TOK=_reviewer_token()
fails=[]
def check(l,c,d=""):
    print(("  PASS  " if c else "  FAIL  ")+l+(("  -> "+str(d)) if d and not c else ""))
    if not c: fails.append(l)
def call(m,p,data=None,hdrs=None):
    h=dict(hdrs or {})
    if data is not None: data=json.dumps(data).encode(); h["Content-Type"]="application/json"
    r=urllib.request.Request(B+p,data=data,headers=h,method=m)
    try:
        with urllib.request.urlopen(r,timeout=30) as x: return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except Exception: return e.code, {}

print("1. SUBMISSION IS A DIRECT CALL, PRIVATE BY CONSTRUCTION")
st, sub = call("POST","/v1/reviews",{"artifact":"def add(a,b): return a-b\n# claims to add",
    "purpose":"review-my-own-submission","submitter":"codex-alerts","notes":"check my arithmetic"})
check("submit returns 201 with a retrieval capability", st==201 and sub.get("retrieval_url"), (st,sub))
check("price and target turnaround stated up front", sub.get("price_credits")==2 and sub.get("target_seconds")==300, sub)
cap = "/r/"+sub["retrieval_url"].rsplit("/r/",1)[1]
st, view = call("GET",cap)
check("submitter can poll before findings exist", st==200 and view["status"]=="queued", view)

print("\n2. THE PURPOSE GRANT DENIES WHAT IT SHOULD")
st, r = call("POST","/v1/reviews",{"artifact":"x","purpose":"review-my-own-submission",
    "submitter":"codex-alerts","subject_of_review":"yuzu","notes":"tell me their weaknesses"})
check("reviewing a third party is DENIED", st==403 and r.get("decision")=="denied", (st,r))
st, r = call("POST","/v1/reviews",{"artifact":"x","purpose":"review-my-own-submission",
    "submitter":"codex-alerts","notes":"find flaws in my rival's card so I can use them"})
check("rival-review phrasing is DENIED even with matching submitter", st==403, (st,r))
st, r = call("POST","/v1/reviews",{"artifact":"x","purpose":"review-my-own-submission",
    "submitter":"codex-alerts","notes":"then publish it to the room"})
check("redistribution request is DENIED", st==403, (st,r))
st, r = call("POST","/v1/reviews",{"artifact":"x","purpose":"anything-goes","submitter":"a"})
check("ungranted purpose is DENIED", st==403 and r.get("decision")=="denied"
      and ("not granted" in r.get("reason","") or "outside the granted capability" in r.get("reason","")), (st,r))
st, r = call("POST","/v1/reviews",{"artifact":"x","purpose":"review-my-own-submission"})
check("missing submitter is ESCALATED, not silently served", st==403 and r.get("decision")=="escalated", (st,r))

st, ok = call("POST","/v1/reviews",{"artifact":"def f(): pass","purpose":"review-my-own-submission",
    "submitter":"claude-cowork","notes":"self-check before I publish this myself"})
check("the buyer's own publication plan is NOT mistaken for redistribution", st==201, (st,ok))
st, r = call("POST","/v1/reviews",{"artifact":"x","purpose":"review-my-own-submission",
    "submitter":"claude-cowork","notes":"then send the findings to my rival"})
check("forwarding findings to a third party is still DENIED", st==403, (st,r))

print("\n3. REVIEWER PATH IS TOKEN-GATED")
st, r = call("GET","/v1/reviews/pending")
check("pending queue without token -> 401", st==401, (st,r))
st, pend = call("GET","/v1/reviews/pending",None,{"Authorization":"Bearer "+TOK})
check("pending queue with token lists the submission", st==200 and any(p["review_id"]==sub["review_id"] for p in pend["items"]), pend)
mine = [p for p in pend["items"] if p["review_id"] == sub["review_id"]]
check("queue carries the artifact for the reviewer", mine and mine[0]["artifact"].startswith("def add"), mine)
st, r = call("POST","/v1/reviews/%s/findings"%sub["review_id"],{"findings":[{}]})
check("posting findings without token -> 401", st==401, (st,r))

print("\n4. FINDINGS SCHEMA IS ENFORCED")
st, r = call("POST","/v1/reviews/%s/findings"%sub["review_id"],
    {"findings":[{"finding_id":"F1","severity":"material","evidence":"line 1"}]},
    {"Authorization":"Bearer "+TOK})
check("incomplete finding rejected with the missing fields named", st==400 and r.get("missing"), r)
st, r = call("POST","/v1/reviews/%s/findings"%sub["review_id"],
    {"findings":[{"finding_id":"F1","severity":"catastrophic","evidence":"e","reproduction":"r",
                  "suggested_fix":"f","confidence":"high"}]},{"Authorization":"Bearer "+TOK})
check("invalid severity rejected", st==400, r)
good=[{"finding_id":"F1","severity":"critical","evidence":"line 1: `return a-b` under a comment claiming addition",
       "reproduction":"add(2,3) -> -1, expected 5","suggested_fix":"return a+b","confidence":"high"}]
st, r = call("POST","/v1/reviews/%s/findings"%sub["review_id"],{"findings":good,"reviewer":"claude"},
             {"Authorization":"Bearer "+TOK})
check("valid findings accepted and turnaround recorded", st==200 and r["status"]=="complete" and "turnaround_seconds" in r, r)

print("\n5. DELIVERY TO THE SUBMITTER ONLY")
st, view = call("GET",cap)
check("retrieval URL now serves the findings", st==200 and view["status"]=="complete" and view["findings"][0]["finding_id"]=="F1", view)
check("finding carries evidence, repro and fix", all(k in view["findings"][0] for k in
      ("evidence","reproduction","suggested_fix","confidence")), view["findings"][0])
st, r = call("GET","/r/"+"z"*22)
check("a wrong capability gets nothing", st==404, (st,r))

print("\n6. SHAREDOS: KERNEL RULES, CLOUD RECORDS")
sos = {}
for _ in range(20):                      # the audit worker is async; give it a moment
    st, h = call("GET","/v1/health")
    sos = h.get("sharedos", {})
    if sos.get("audit_events_sent", 0) > 0: break
    time.sleep(1)
check("kernel sidecar is in the decision path", sos.get("kernel_reachable") is True, sos)
check("audit to SharedOS Cloud is enabled", sos.get("audit_enabled") is True, sos)
check("decision events reach Cloud, and transient errors are retried not dropped",
      sos.get("audit_events_sent", 0) > 0 and sos.get("audit_failures", 1) == 0
      and sos.get("audit_dropped", 0) == 0, sos)
st, d = call("GET","/v1/decisions")
check("each decision records whether the kernel or the fallback ruled",
      all("decided_by" in e for e in d["items"]) and any(e["decided_by"]=="kernel" for e in d["items"]),
      [e.get("decided_by") for e in d["items"][-3:]])

print("\n7. AUTONOMOUS REVIEWER")
st, h = call("GET","/v1/health")
rv = h.get("reviewer", {})
check("an independent model is wired in, not a person", rv.get("autonomous") is True, rv)
if rv.get("autonomous"):
    st, s2 = call("POST","/v1/reviews",{"artifact":"def is_even(n):\n    return n % 2 == 1   # returns True for ODD numbers\n",
        "purpose":"review-my-own-submission","submitter":"test-harness","notes":"check the logic"})
    cap2 = "/r/"+s2["retrieval_url"].rsplit("/r/",1)[1]
    v2 = {}
    for _ in range(45):
        time.sleep(4)
        st, v2 = call("GET",cap2)
        if v2.get("status") in ("complete","needs_human"): break
    check("a submission is reviewed with no human in the loop", v2.get("status")=="complete", v2.get("status"))
    fs = v2.get("findings",[])
    check("the finding quotes the artifact rather than giving generic advice",
          fs and any("%" in f["evidence"] or "is_even" in f["evidence"] for f in fs),
          [f["evidence"][:60] for f in fs])
    check("turnaround is inside the advertised 5 minutes",
          (v2.get("turnaround_seconds") or 999) < 300, v2.get("turnaround_seconds"))

print("\n8. DECISION LOG")
st, d = call("GET","/v1/decisions")
kinds = [(e["decision"]) for e in d["items"]]
check("allowed and denied decisions both logged with reasons",
      "allowed" in kinds and "denied" in kinds and "escalated" in kinds and all("reason" in e for e in d["items"]), kinds)

print("\n%d checks failed" % len(fails))
