// Regression tests for the relay-js cluster of the Steward Reconstruction Program.
//
// Covers (all ADDITIVE, all verifying the fixes in relay/whatsapp_relay.js):
//   ingest-whatsapp-3      reactions/protocol/poll-votes are DROPPED; stickers/video/
//                          documents/locations/contacts/polls get their REAL media_type
//                          and an accurate placeholder body (never "[unsupported message type]").
//   ingest-whatsapp-4      owner-outbound is durable: a redacted buffer survives an
//                          engine-down window and replays (uses the same outbox machinery).
//   ingest-whatsapp-5      agentSentIds is a TTL + size bounded timestamped map; a slow
//                          self-echo past the FIFO window is still recognized within TTL,
//                          and a too-old echo is correctly treated as a real owner msg.
//   config-secrets-deploy-6  the outbox is capped by COUNT and AGE, pruned, and persists
//                          NO cleartext body (redacted at rest) — closes NO_SECRET_IN_LOGS.
//
// Run from repo root:  node --test tests/test_relay_whatsapp_relay.mjs
//
// The relay is a Node/Baileys process; the Python unittest suite cannot exercise it.
// These tests import the relay's pure helpers (gated behind IS_MAIN_MODULE so importing
// never opens a socket / binds a port / schedules timers) and assert on them directly.

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const relayPath = path.join(__dirname, "..", "relay", "whatsapp_relay.js");
const { __test__ } = await import(relayPath);

const {
  classifyMessage,
  redactForOutbox,
  pruneOutbox,
  outboxDepth,
  rememberAgentSend,
  consumeAgentSend,
  agentSentIds,
  saveToOutbox,
  AGENT_SEND_TTL_MS,
  AGENT_SEND_MAX,
  coerceNum,
  forwardReceipt,
  lastReceiptStatus,
} = __test__;

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "relay-outbox-test-"));
}
function rmrf(d) { try { fs.rmSync(d, { recursive: true, force: true }); } catch {} }

// ─────────────────────────────────────────────────────────────────────────────
// ingest-whatsapp-3 — non-text classification
// ─────────────────────────────────────────────────────────────────────────────

test("ingest-whatsapp-3: plain text and extendedText classify as real text (no media tag)", () => {
  const a = classifyMessage({ conversation: "hello" });
  assert.equal(a.drop, undefined);
  assert.equal(a.mediaType, "");
  assert.equal(a.body, "hello");

  const b = classifyMessage({ extendedTextMessage: { text: "world" } });
  assert.equal(b.mediaType, "");
  assert.equal(b.body, "world");
});

test("ingest-whatsapp-3: reactions, protocol and poll-VOTES are dropped (never a card)", () => {
  assert.equal(classifyMessage({ reactionMessage: { text: "👍" } }).drop, true);
  assert.equal(classifyMessage({ protocolMessage: { type: 0 } }).drop, true);
  assert.equal(classifyMessage({ pollUpdateMessage: {} }).drop, true);
  assert.equal(classifyMessage({ senderKeyDistributionMessage: {} }).drop, true);
  // a bare messageContextInfo-only node carries no addressable content
  assert.equal(classifyMessage({ messageContextInfo: {} }).drop, true);
});

test("ingest-whatsapp-3: media types get their REAL media_type + accurate placeholder body", () => {
  const sticker = classifyMessage({ stickerMessage: {} });
  assert.equal(sticker.mediaType, "sticker");
  assert.equal(sticker.body, "[sticker]");

  const video = classifyMessage({ videoMessage: {} });
  assert.equal(video.mediaType, "video");
  assert.equal(video.body, "[video]");

  const gif = classifyMessage({ videoMessage: { gifPlayback: true } });
  assert.equal(gif.mediaType, "video");
  assert.equal(gif.body, "[gif]");

  const doc = classifyMessage({ documentMessage: { fileName: "invoice.pdf" } });
  assert.equal(doc.mediaType, "document");
  assert.equal(doc.body, "[document: invoice.pdf]");

  const loc = classifyMessage({ locationMessage: { name: "Cafe X" } });
  assert.equal(loc.mediaType, "location");
  assert.equal(loc.body, "[location: Cafe X]");

  const contact = classifyMessage({ contactMessage: { displayName: "Rahul" } });
  assert.equal(contact.mediaType, "contact");
  assert.equal(contact.body, "[contact: Rahul]");

  const poll = classifyMessage({ pollCreationMessage: { name: "Lunch?" } });
  assert.equal(poll.mediaType, "poll");
  assert.equal(poll.body, "[poll: Lunch?]");
});

test("ingest-whatsapp-3: NO body is ever the legacy generic '[unsupported message type]'", () => {
  // The old code mapped every non-handled type to that exact literal, which then became a
  // real inbound body the classifier drafted against. The new code never emits it.
  const samples = [
    { stickerMessage: {} },
    { videoMessage: {} },
    { documentMessage: {} },
    { locationMessage: {} },
    { contactMessage: {} },
    { pollCreationMessage: {} },
    { someBrandNewType2099: {} }, // unknown/future type
  ];
  for (const m of samples) {
    const c = classifyMessage(m);
    if (c.drop) continue;
    assert.notEqual(c.body, "[unsupported message type]");
    assert.ok(c.mediaType && c.mediaType.length > 0, `expected a media_type tag for ${Object.keys(m)[0]}`);
  }
});

test("ingest-whatsapp-3: generated placeholder bodies contain no em-dashes", () => {
  const bodies = [
    classifyMessage({ stickerMessage: {} }).body,
    classifyMessage({ videoMessage: {} }).body,
    classifyMessage({ documentMessage: { fileName: "a.pdf" } }).body,
    classifyMessage({ locationMessage: { name: "X" } }).body,
    classifyMessage({ contactMessage: { displayName: "Y" } }).body,
    classifyMessage({ pollCreationMessage: { name: "Q" } }).body,
    classifyMessage({ unknownType: {} }).body,
  ];
  for (const b of bodies) assert.ok(!b.includes("—"), `em-dash leaked into generated body: ${b}`);
});

test("ingest-whatsapp-3: unknown/future type is tagged 'unsupported' (not dropped, not drafted-to)", () => {
  const c = classifyMessage({ futureKind: {} });
  assert.equal(c.drop, undefined);
  assert.equal(c.mediaType, "unsupported");
  assert.equal(c.body, "[unsupported message]");
});

// ─────────────────────────────────────────────────────────────────────────────
// ingest-whatsapp-5 — TTL + size bounded echo dedup
// ─────────────────────────────────────────────────────────────────────────────

test("ingest-whatsapp-5: a self-echo delayed past 500 sends is STILL recognized within TTL", () => {
  agentSentIds.clear();
  const t0 = 1_000_000;
  rememberAgentSend("slow-echo", t0);
  // simulate 600 later agent sends (the old FIFO Set capped at 500 → would evict "slow-echo")
  for (let i = 0; i < 600; i++) rememberAgentSend(`later-${i}`, t0 + i + 1);
  assert.ok(agentSentIds.has("slow-echo"), "id must survive >500 subsequent sends (count cap is 5000, not 500)");
  // the delayed echo arrives a minute later — still inside TTL → recognized as our own send
  assert.equal(consumeAgentSend("slow-echo", t0 + 60_000), true);
  // and it is consumed so a duplicate echo does not double-count
  assert.equal(consumeAgentSend("slow-echo", t0 + 60_001), false);
});

test("ingest-whatsapp-5: an echo older than the TTL is treated as a real owner message", () => {
  agentSentIds.clear();
  const t0 = 2_000_000;
  rememberAgentSend("ancient", t0);
  // echo arrives AFTER the TTL window → NOT a trustworthy self-echo → owner-outbound
  assert.equal(consumeAgentSend("ancient", t0 + AGENT_SEND_TTL_MS + 1), false);
});

test("ingest-whatsapp-5: size cap is bounded and prefers evicting by insertion order", () => {
  agentSentIds.clear();
  const t0 = 3_000_000;
  // insert well past the hard cap; map must never exceed AGENT_SEND_MAX
  for (let i = 0; i < AGENT_SEND_MAX + 50; i++) rememberAgentSend(`id-${i}`, t0 + i);
  assert.ok(agentSentIds.size <= AGENT_SEND_MAX, `size ${agentSentIds.size} must be <= ${AGENT_SEND_MAX}`);
  // the most recent send is always still present
  assert.ok(agentSentIds.has(`id-${AGENT_SEND_MAX + 49}`));
});

test("ingest-whatsapp-5: unknown ids and empty ids are not false positives", () => {
  agentSentIds.clear();
  assert.equal(consumeAgentSend("never-seen"), false);
  assert.equal(consumeAgentSend(""), false);
  assert.equal(consumeAgentSend(undefined), false);
});

// ─────────────────────────────────────────────────────────────────────────────
// config-secrets-deploy-6 — redaction + retention (closes NO_SECRET_IN_LOGS)
// ─────────────────────────────────────────────────────────────────────────────

const SENSITIVE = "Wire $40,000 to account 12345678 — meet me at 7";

test("config-secrets-deploy-6: redactForOutbox persists NO cleartext body / quoted / media", () => {
  const payload = {
    messageId: "wa_abc",
    jid: "919164536565@s.whatsapp.net",
    sender_jid: "919164536565@s.whatsapp.net",
    phone_number: "+919164536565",
    body: SENSITIVE,
    quoted_body: "earlier secret reply",
    media_b64: "AAAABBBBCCCC==",
    media_type: "image",
    is_group: false,
    timestamp: 1234,
  };
  const r = redactForOutbox(payload);
  const serialized = JSON.stringify(r);

  // the cleartext body / quoted text / media bytes must NOT appear anywhere at rest
  assert.ok(!serialized.includes(SENSITIVE), "cleartext body leaked into the redacted record");
  assert.ok(!serialized.includes("earlier secret reply"), "cleartext quoted text leaked");
  assert.ok(!serialized.includes("AAAABBBBCCCC"), "media bytes leaked");
  assert.ok(!("body" in r), "redacted record must not carry a 'body' field");
  assert.ok(!("quoted_body" in r), "redacted record must not carry 'quoted_body'");
  assert.ok(!("media_b64" in r), "redacted record must not carry 'media_b64'");

  // routing/identity needed for replay + dedup IS retained (length + hash, not content)
  assert.equal(r.messageId, "wa_abc");
  assert.equal(r.jid, payload.jid);
  assert.equal(r.body_redacted, true);
  assert.equal(r.body_len, SENSITIVE.length);
  assert.ok(/^[0-9a-f]{16}$/.test(r.body_sha256), "expected a 16-hex sha256 prefix for dedup/debug");
  assert.equal(r.has_media, true);
});

test("config-secrets-deploy-6: saveToOutbox writes a redacted file (never the cleartext body)", () => {
  const dir = tmpDir();
  try {
    saveToOutbox({
      messageId: "wa_secret1",
      jid: "x@s.whatsapp.net",
      sender_jid: "x@s.whatsapp.net",
      body: SENSITIVE,
      quoted_body: "q",
      media_b64: "",
      timestamp: 1,
    }, dir);
    const files = fs.readdirSync(dir).filter((f) => f.endsWith(".json"));
    assert.equal(files.length, 1);
    const onDisk = fs.readFileSync(path.join(dir, files[0]), "utf8");
    assert.ok(!onDisk.includes(SENSITIVE), "cleartext body was written to the outbox file on disk");
    assert.ok(onDisk.includes("body_redacted"), "expected the redacted marker on disk");
  } finally { rmrf(dir); }
});

test("config-secrets-deploy-6: pruneOutbox enforces the COUNT cap, dropping oldest first", async () => {
  // The count cap is read from RELAY_OUTBOX_MAX_FILES at module load. Import a SECOND,
  // isolated instance of the relay module with a tiny cap set (ESM caches by URL, so a
  // unique query string forces a fresh module init) and assert oldest-first eviction.
  const dir = tmpDir();
  try {
    fs.mkdirSync(dir, { recursive: true });
    const now = Date.now();
    // 12 files with strictly increasing mtimes (m0 oldest ... m11 newest).
    for (let i = 0; i < 12; i++) {
      const fp = path.join(dir, `m${i}.json`);
      fs.writeFileSync(fp, JSON.stringify({ messageId: `m${i}`, body_redacted: true }));
      const t = (now - (12 - i) * 1000) / 1000;
      fs.utimesSync(fp, t, t);
    }
    const prevCap = process.env.RELAY_OUTBOX_MAX_FILES;
    process.env.RELAY_OUTBOX_MAX_FILES = "5";
    try {
      const fresh = await import(relayPath + `?cap=${Date.now()}`);
      const removed = fresh.__test__.pruneOutbox(dir, now);
      assert.equal(removed, 7, "12 files capped at 5 must remove 7");
      const remaining = fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort();
      assert.equal(remaining.length, 5);
      // the NEWEST 5 (m7..m11) survive; the OLDEST 7 (m0..m6) are gone
      for (let i = 7; i < 12; i++) assert.ok(remaining.includes(`m${i}.json`), `newest m${i} must survive`);
      for (let i = 0; i < 7; i++) assert.ok(!remaining.includes(`m${i}.json`), `oldest m${i} must be evicted`);
    } finally {
      if (prevCap === undefined) delete process.env.RELAY_OUTBOX_MAX_FILES;
      else process.env.RELAY_OUTBOX_MAX_FILES = prevCap;
    }
  } finally { rmrf(dir); }
});

test("config-secrets-deploy-6: pruneOutbox enforces the AGE cap (drops files older than the window)", () => {
  const dir = tmpDir();
  try {
    fs.mkdirSync(dir, { recursive: true });
    const now = Date.now();
    // fresh file (should survive) and an ancient file (should be pruned by age)
    const freshFp = path.join(dir, "fresh.json");
    const oldFp = path.join(dir, "old.json");
    fs.writeFileSync(freshFp, JSON.stringify({ messageId: "fresh", body_redacted: true }));
    fs.writeFileSync(oldFp, JSON.stringify({ messageId: "old", body_redacted: true }));
    // make "old" 100 days old (default age cap is 72h)
    const ancient = (now - 100 * 24 * 3600 * 1000) / 1000;
    fs.utimesSync(oldFp, ancient, ancient);

    const removed = pruneOutbox(dir, now);
    assert.ok(removed >= 1, "expected the ancient file to be pruned by the age cap");
    const remaining = fs.readdirSync(dir).filter((f) => f.endsWith(".json"));
    assert.ok(remaining.includes("fresh.json"), "fresh file must survive");
    assert.ok(!remaining.includes("old.json"), "ancient file must be pruned");
  } finally { rmrf(dir); }
});

test("config-secrets-deploy-6: outboxDepth counts only json files and is robust to a missing dir", () => {
  assert.equal(outboxDepth(path.join(os.tmpdir(), "does-not-exist-relay-xyz")), 0);
  const dir = tmpDir();
  try {
    fs.writeFileSync(path.join(dir, "a.json"), "{}");
    fs.writeFileSync(path.join(dir, "b.txt"), "x"); // non-json ignored
    assert.equal(outboxDepth(dir), 1);
  } finally { rmrf(dir); }
});

// ─────────────────────────────────────────────────────────────────────────────
// ingest-whatsapp-4 — owner-outbound durability uses the same redacted, capped path
// ─────────────────────────────────────────────────────────────────────────────

test("ingest-whatsapp-4: a buffered owner-outbound record is redacted and preserves from_me + jid", () => {
  // The presence signal only needs routing/identity (jid, sender, from_me, timestamp) to
  // restore last_outbound_ts on replay — never the cleartext body. Assert that's exactly
  // what the durable buffer carries.
  const r = redactForOutbox({
    messageId: "wa_out_123",
    jid: "client@s.whatsapp.net",
    sender_jid: "client@s.whatsapp.net",
    body: "On it, sending now",
    from_me: true,
    timestamp: 9999,
  });
  assert.equal(r.from_me, true);
  assert.equal(r.jid, "client@s.whatsapp.net");
  assert.equal(r.timestamp, 9999);
  assert.ok(!("body" in r), "owner-outbound body must not be persisted in cleartext");
  assert.equal(r.body_redacted, true);
});

test("ingest-whatsapp-4: owner-outbound buffers to a SEPARATE dir from inbound", () => {
  // The two paths must not collide; saveToOutbox honors the dir argument.
  const inDir = tmpDir();
  const outDir = tmpDir();
  try {
    saveToOutbox({ messageId: "in1", jid: "a@s.whatsapp.net", body: "hi", timestamp: 1 }, inDir);
    saveToOutbox({ messageId: "out1", jid: "b@s.whatsapp.net", body: "yo", from_me: true, timestamp: 2 }, outDir);
    assert.equal(outboxDepth(inDir), 1);
    assert.equal(outboxDepth(outDir), 1);
    const outRec = JSON.parse(fs.readFileSync(path.join(outDir, "out1.json"), "utf8"));
    assert.equal(outRec.from_me, true);
    assert.equal(outRec.body_redacted, true);
  } finally { rmrf(inDir); rmrf(outDir); }
});

// ─────────────────────────────────────────────────────────────────────────────
// message-lifecycle — receipt forwarding (messages.update / message-receipt.update)
// ─────────────────────────────────────────────────────────────────────────────

test("lifecycle: coerceNum handles number, Long-like {low}, and null", () => {
  assert.equal(coerceNum(4), 4);
  assert.equal(coerceNum({ toNumber: () => 3 }), 3);
  assert.equal(coerceNum({ low: 2 }), 2);
  assert.equal(coerceNum(null), 0);
});

test("lifecycle: forwardReceipt only tracks OUR (fromMe) messages and coalesces forward-only", () => {
  lastReceiptStatus.clear();
  // inbound (fromMe false) is never tracked
  forwardReceipt({ id: "in1", fromMe: false }, 4, undefined, null);
  assert.equal(lastReceiptStatus.has("in1:"), false);
  // our message advances 2 -> 3 -> 4, but a stale 3 is dropped (coalesced)
  forwardReceipt({ id: "m1", fromMe: true, remoteJid: "x@s.whatsapp.net" }, 2, undefined, null);
  forwardReceipt({ id: "m1", fromMe: true, remoteJid: "x@s.whatsapp.net" }, 4, undefined, null);
  forwardReceipt({ id: "m1", fromMe: true, remoteJid: "x@s.whatsapp.net" }, 3, undefined, null);
  assert.equal(lastReceiptStatus.get("m1:"), 4); // highest wins; never regresses
});

test("lifecycle: group per-recipient receipts advance independently", () => {
  lastReceiptStatus.clear();
  forwardReceipt({ id: "g1", fromMe: true, remoteJid: "123@g.us" }, 4, undefined, "a@s.whatsapp.net");
  forwardReceipt({ id: "g1", fromMe: true, remoteJid: "123@g.us" }, 3, undefined, "b@s.whatsapp.net");
  assert.equal(lastReceiptStatus.get("g1:a@s.whatsapp.net"), 4);
  assert.equal(lastReceiptStatus.get("g1:b@s.whatsapp.net"), 3); // not dropped by a's higher status
});
