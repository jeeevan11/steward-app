// Tiny fetch helpers. All requests go to /api (Vite proxies to 127.0.0.1:8000).

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export const api = {
  // header / overview
  status: () => get("/api/status"),
  stats: () => get("/api/stats"),
  pipeline: () => get("/api/pipeline"),
  pipelineStatus: () => get("/api/pipeline/status"),
  // recent activity feed + non-destructive "clear for now"
  notifications: () => get("/api/notifications"),
  notificationsClear: () => post("/api/notifications/clear"),
  // force an immediate fetch+process pass on email + WhatsApp (the "Fetch everything" button)
  fetchNow: () => post("/api/fetch-now"),
  // talk to Steward in plain English (add a rule, set importance, pause…)
  command: (text) => post("/api/command", { text }),
  // queue + detail
  queue: () => get("/api/queue"),
  queueSummary: () => get("/api/queue-summary"),
  email: (id) => get(`/api/email/${encodeURIComponent(id)}`),
  queueDetail: (id) => get(`/api/queue/${encodeURIComponent(id)}`),
  // actions
  approve: (id) => post(`/api/actions/${id}/approve`),
  edit: (id, text) => post(`/api/actions/${id}/edit`, { text }),
  skip: (id) => post(`/api/actions/${id}/skip`),
  feedback: (msgId, correct_tier, thumbs) =>
    post(`/api/email/${encodeURIComponent(msgId)}/feedback`, { correct_tier, thumbs }),
  // commitments (P4)
  commitments: () => get("/api/commitments"),
  commitmentDone: (id) => post(`/api/commitments/${id}/done`),
  commitmentSnooze: (id, days = 2) => post(`/api/commitments/${id}/snooze`, { days }),
  // voice profiles (P5a)
  voiceProfiles: () => get("/api/voice-profiles"),
  voiceRebuild: () => post("/api/voice-profiles/rebuild"),
  voiceSamples: (seg) => get(`/api/voice-profiles/${encodeURIComponent(seg)}/samples`),
  // contacts + rules
  contacts: () => get("/api/contacts"),
  contactUpdate: (email, body) => post(`/api/contacts/${encodeURIComponent(email)}/update`, body),
  syncContacts: () => post("/api/sync-contacts", {}),
  rules: () => get("/api/rules"),
  rulesProposed: () => get("/api/rules/proposed"),
  ruleConfirm: (id) => post(`/api/rules/${id}/confirm`),
  ruleReject: (id) => post(`/api/rules/${id}/reject`),
  // audit
  audit: () => get("/api/audit"),
  auditLog: (q = {}) => {
    const p = new URLSearchParams(q).toString();
    return get(`/api/audit-log${p ? "?" + p : ""}`);
  },
  auditExportUrl: (q = {}) => {
    const p = new URLSearchParams(q).toString();
    return `/api/audit-log/export${p ? "?" + p : ""}`;
  },
  // test the brain (P6) — zero side effects
  testPipeline: (payload) => post("/api/test-pipeline", payload),
  evalBrain: (payload) => post("/api/eval", payload),
  // metrics (P6)
  metricsDaily: () => get("/api/metrics/daily"),
  metricsAccuracy: () => get("/api/metrics/accuracy"),
  metricsCosts: () => get("/api/metrics/costs"),
  metricsResponseTimes: () => get("/api/metrics/response-times"),
  wastatus: () => get("/api/wastatus"),
};

// WebSocket for live pipeline status; caller provides onStatus. Returns a close fn.
// Falls back silently (the caller keeps polling) if the socket can't connect.
export function openPipelineSocket(onStatus) {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/pipeline`);
    ws.onmessage = (e) => {
      try { onStatus(JSON.parse(e.data)); } catch (_) {}
    };
    return () => { try { ws.close(); } catch (_) {} };
  } catch (_) {
    return () => {};
  }
}

export function timeAgo(epoch) {
  if (!epoch) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
