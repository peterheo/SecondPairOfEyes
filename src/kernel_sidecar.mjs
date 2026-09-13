// SharedOS kernel sidecar for Second Pair of Eyes.
// The kernel runs inside our application, as SharedOS intends: grants come from
// host configuration, never from the request, and every decision is audited to
// SharedOS Cloud.
import http from "node:http";
import fs from "node:fs";

// .env first, then the environment, then the on-disk fallback
function loadEnv(paths) {
  for (const p of paths) {
    if (!p || !fs.existsSync(p)) continue;
    for (const raw of fs.readFileSync(p, "utf8").split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const i = line.indexOf("=");
      const k = line.slice(0, i).trim();
      const v = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
      if (process.env[k] === undefined) process.env[k] = v;
    }
  }
}
loadEnv([process.env.SPE_ENV_FILE, "/opt/spe/.env", "./.env"]);
const KEY_PATHS = ["/opt/spe/spe_state/sharedos_key", "./spe_state/sharedos_key"];
const KEY = (process.env.SHAREDOS_KEY
  || KEY_PATHS.map((p) => (fs.existsSync(p) ? fs.readFileSync(p, "utf8").trim() : "")).find(Boolean)
  || "").trim();
const NAMESPACE = "second-pair-of-eyes";
const CAPABILITY = "review-my-own-submission";

// Host-configured grants. A caller cannot describe its own grant: the request
// carries no authority, only a claim we check against this table.
const GRANTS = JSON.parse(fs.existsSync("./grants.json")
  ? fs.readFileSync("./grants.json", "utf8")
  : `{"default":{"capabilities":["${CAPABILITY}"],"mode":"self-only"}}`);

let Kernel = null;
try { ({ SharedOSKernel: Kernel } = await import("@aicoo/sharedos")); } catch { /* sdk absent */ }

async function recordAudit(event) {
  if (!KEY) return;
  try {
    await fetch("https://www.sharedos.ai/v1/audit/events", {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${KEY}` },
      body: JSON.stringify({ events: [event] }),
    });
  } catch { /* never block a decision on the console */ }
}

const kernel = Kernel
  ? new Kernel({
      grantSource: { async load(access) { return GRANTS[access?.agentId] || GRANTS.default; } },
      audit: { async record(event) { await recordAudit(event); } },
    })
  : null;

const THIRD_PARTY = /\b(rival|competitor|opponent|their (card|product|service|code)|someone else'?s|another agent'?s)\b/i;
const REDISTRIBUTE = /\b(publish|post|share|redistribute|leak|broadcast|forward|send)\b[^.?!]{0,40}?\b(finding|findings|review|report|results?)\b[^.?!]{0,40}?\b(to|with|in the room|publicly)\b|\b(finding|findings|review|report|results?)\b[^.?!]{0,40}?\b(publish|post|share|redistribute|leak|broadcast|forward|send)\b[^.?!]{0,40}?\b(to|with|in the room|publicly)\b|\b(publish|post|share|forward|send|broadcast|leak)\s+(it|this|them|these)\s+(to|with|in)\b|\b(redistribute|leak)\b/i;

function evaluate({ agent, capability, purpose, subject, request }) {
  const agentId = agent?.agentId || "";
  const grant = GRANTS[agentId] || GRANTS.default;
  if (!grant.capabilities.includes(capability))
    return { decision: "denied", reason: `no grant for capability ${capability}` };
  if (purpose !== capability)
    return { decision: "denied", reason: `purpose ${purpose} is outside the granted capability` };
  if (grant.mode === "self-only" && subject && agentId && subject.toLowerCase() !== agentId.toLowerCase())
    return { decision: "denied", reason: "grant is self-only: findings return to the author alone" };
  if (THIRD_PARTY.test(request || ""))
    return { decision: "denied", reason: "request targets a third party's work" };
  if (REDISTRIBUTE.test(request || ""))
    return { decision: "denied", reason: "request asks for redistribution" };
  if (!agentId || agentId === "unstated")
    return { decision: "escalated", reason: "caller did not declare an identity to bind findings to" };
  return { decision: "allowed", reason: "grant clause: self-only review, findings to author" };
}

http.createServer((req, res) => {
  if (req.method !== "POST" || !req.url.startsWith("/authorize")) {
    res.writeHead(404, { "content-type": "application/json" });
    return res.end(JSON.stringify({ error: "not found" }));
  }
  let body = "";
  req.on("data", (c) => { body += c; if (body.length > 65536) req.destroy(); });
  req.on("end", async () => {
    let parsed; try { parsed = JSON.parse(body || "{}"); } catch {
      res.writeHead(400, { "content-type": "application/json" });
      return res.end(JSON.stringify({ error: "invalid json" }));
    }
    const out = evaluate(parsed);
    await recordAudit({
      type: "authorization.decision",
      outcome: out.decision,
      namespaceId: NAMESPACE,
      traceId: parsed.traceId || `k_${Date.now().toString(36)}`,
      at: new Date().toISOString(),
      agent: parsed.agent || { kind: "agent", agentId: "unstated" },
      capability: parsed.capability || CAPABILITY,
      reason: out.reason,
      kernelSdk: Boolean(kernel),
    });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ...out, kernel_sdk_loaded: Boolean(kernel), namespace: NAMESPACE }));
  });
}).listen(8450, "127.0.0.1", () =>
  console.log(`sharedos kernel sidecar on 8450 (sdk=${Boolean(kernel)}, audit=${Boolean(KEY)})`));
