// WhatsApp relay — a DUMB pipe between WhatsApp (Baileys) and the Python brain.
//
// It does NO triage, NO drafting, NO decisions. It only:
//   * holds the WhatsApp session (paired once via QR, persisted to ./session/)
//   * forwards every incoming message as JSON to the Python receiver (/inbound)
//   * accepts /send and /read commands from Python and performs them
//   * jitters 2-8s before every send so it never looks robotic
//   * reconnects with exponential backoff, and writes ./status.json + ./relay.log
//
// Do NOT add "anti-ban" npm packages — they are malware. Jitter is the only
// mitigation. See RELAY_README.md.

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSION_DIR = path.join(__dirname, "session");
const LOG_PATH = path.join(__dirname, "relay.log");
const STATUS_PATH = path.join(__dirname, "status.json");
const OUTBOX_DIR = path.join(__dirname, "outbox");

// True only when this file is run directly (node whatsapp_relay.js), false when imported
// by a unit test. ALL runtime side effects (timers, WhatsApp connect, boot logging) are
// gated on this so the pure helpers can be exercised by `node --test` without opening a
// socket, binding a port, or scheduling intervals. Importing is otherwise inert.
const IS_MAIN_MODULE = (() => {
  try {
    if (!process.argv[1]) return false;
    return import.meta.url === new URL(`file://${process.argv[1]}`).href ||
           fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1]);
  } catch { return false; }
})();

const PY_INBOUND_PORT = parseInt(process.env.WHATSAPP_RELAY_PORT || "7999", 10); // Python listens
const SEND_PORT = parseInt(process.env.WHATSAPP_SEND_PORT || "7998", 10);        // we listen
const PY_INBOUND_URL = `http://127.0.0.1:${PY_INBOUND_PORT}/inbound`;
const PY_OUTBOUND_URL = `http://127.0.0.1:${PY_INBOUND_PORT}/outbound`;
const PY_RECEIPT_URL = `http://127.0.0.1:${PY_INBOUND_PORT}/receipt`;

// Ids of messages WE (the agent) sent via /send, so the fromMe echo of our own sends
// is NOT mistaken for the owner texting himself (would corrupt presence + style).
//
// ROOT CAUSE (ingest-whatsapp-5): this used to be an UNTIMED 500-entry FIFO Set. On a
// busy account the agent can emit >500 sends before WhatsApp finally echoes back an
// earlier one (a delayed fromMe receipt after a reconnect / app-state resync). Once the
// id had been FIFO-evicted, its echo was no longer recognized as our own send, so it was
// mis-forwarded as the OWNER texting himself — polluting presence suppression and the
// style corpus with the agent's own words. Eviction was purely by count, with no notion
// of age, so a slow echo was structurally guaranteed to be mis-ingested under load.
//
// Fix: a TTL- and size-bounded timestamped map. An id is recognized as our own send for
// up to AGENT_SEND_TTL_MS (echoes virtually always arrive within seconds, but we keep a
// generous window for reconnect-delayed receipts). We still cap the map so memory stays
// bounded, but eviction now prefers EXPIRED entries first and only falls back to oldest
// when everything is still live — so a fresh send is never silently dropped to make room.
const AGENT_SEND_TTL_MS = parseInt(process.env.RELAY_AGENT_SEND_TTL_MS || "", 10) || 6 * 60 * 60 * 1000; // 6h
const AGENT_SEND_MAX = parseInt(process.env.RELAY_AGENT_SEND_MAX || "", 10) || 5000;
const agentSentIds = new Map(); // id -> epoch_ms remembered

function pruneAgentSends(nowMs = Date.now()) {
  // Drop expired entries first (cheap, keeps the live window correct).
  for (const [id, ts] of agentSentIds) {
    if (nowMs - ts > AGENT_SEND_TTL_MS) agentSentIds.delete(id);
  }
  // Hard size cap as a backstop — evict oldest insertions (Map preserves order).
  while (agentSentIds.size > AGENT_SEND_MAX) {
    const oldest = agentSentIds.keys().next().value;
    if (oldest === undefined) break;
    agentSentIds.delete(oldest);
  }
}

function rememberAgentSend(id, nowMs = Date.now()) {
  if (!id) return;
  agentSentIds.set(id, nowMs);
  pruneAgentSends(nowMs);
}

// True if `id` is a still-live echo of one of OUR sends. Expired ids return false (the
// echo arrived later than any plausible self-echo window — treat as a real owner msg).
// On a hit we consume the id so a duplicate echo of the SAME send is not double-counted.
function consumeAgentSend(id, nowMs = Date.now()) {
  if (!id || !agentSentIds.has(id)) return false;
  const ts = agentSentIds.get(id);
  agentSentIds.delete(id);
  if (nowMs - ts > AGENT_SEND_TTL_MS) return false; // too old: not a trustworthy self-echo
  return true;
}

const logger = pino({ level: "warn" }); // Baileys' own logger (quiet)

// ── tiny logging + status ────────────────────────────────────────────────────
function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(" ")}`;
  console.log(line);
  try { fs.appendFileSync(LOG_PATH, line + "\n"); } catch {}
}

const sessionStart = Math.floor(Date.now() / 1000);
let connected = false;
let messagesToday = 0;
let messagesDayStamp = new Date().toDateString();
let lastMessageTs = 0;
// Outbox depths surfaced in status.json (config-secrets-deploy-6): a non-zero, non-
// draining depth reliably signals a broken inbound/outbound path the owner must fix.
let lastOutboxDepth = 0;
let lastOutboxOutDepth = 0;

function bumpMessageCount() {
  const today = new Date().toDateString();
  if (today !== messagesDayStamp) { messagesDayStamp = today; messagesToday = 0; }
  messagesToday += 1;
  lastMessageTs = Math.floor(Date.now() / 1000);
}

function writeStatus() {
  const status = {
    connected,
    session_age_seconds: Math.floor(Date.now() / 1000) - sessionStart,
    messages_today: messagesToday,
    last_message_ts: lastMessageTs,
    // Redacted health signal only — never any message body (NO_SECRET_IN_LOGS).
    outbox_depth: lastOutboxDepth,
    outbox_out_depth: lastOutboxOutDepth,
    updated_at: Math.floor(Date.now() / 1000),
  };
  try { fs.writeFileSync(STATUS_PATH, JSON.stringify(status, null, 2)); } catch {}
}
if (IS_MAIN_MODULE) setInterval(writeStatus, 30_000);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const jitterMs = () => 2000 + Math.floor(Math.random() * 6000); // 2–8s

// ── message extraction ───────────────────────────────────────────────────────
const groupNameCache = new Map();
// Phone-saved contact names — accumulates from contacts.upsert / chats.upsert events.
// Persisted to disk so it survives restarts and grows over time.
const CONTACT_CACHE_PATH = path.join(__dirname, "contact_cache.json");
const contactNameCache = new Map();
(function loadContactCache() {
  try {
    const raw = fs.readFileSync(CONTACT_CACHE_PATH, "utf8");
    const obj = JSON.parse(raw);
    for (const [jid, name] of Object.entries(obj)) {
      if (jid && name) contactNameCache.set(jid.toLowerCase(), name);
    }
    log(`Loaded ${contactNameCache.size} contacts from cache.`);
  } catch { /* first run or corrupt — start fresh */ }
})();

// LID → phone JID mapping: '9136083337274@lid' → '919164536565@s.whatsapp.net'
// Baileys delivers this in contacts.upsert when c.id is a phone JID and c.lid is the alias.
const LID_JID_MAP_PATH = path.join(__dirname, "lid_jid_map.json");
const lidToJidMap = new Map(); // lid (lower) → phone jid (lower)
const jidToLidMap = new Map(); // phone jid (lower) → lid (lower), for reverse lookups
(function loadLidJidMap() {
  try {
    const obj = JSON.parse(fs.readFileSync(LID_JID_MAP_PATH, "utf8"));
    for (const [lid, jid] of Object.entries(obj)) {
      if (lid && jid) { lidToJidMap.set(lid.toLowerCase(), jid.toLowerCase()); jidToLidMap.set(jid.toLowerCase(), lid.toLowerCase()); }
    }
    log(`Loaded ${lidToJidMap.size} LID→JID mappings.`);
  } catch { /* first run */ }
})();
let _saveLidMapTimer = null;
function scheduleLidMapSave() {
  if (_saveLidMapTimer) return;
  _saveLidMapTimer = setTimeout(() => {
    _saveLidMapTimer = null;
    try { fs.writeFileSync(LID_JID_MAP_PATH, JSON.stringify(Object.fromEntries(lidToJidMap), null, 2)); } catch {}
  }, 5000);
}

let _saveContactCacheTimer = null;
function scheduleContactCacheSave() {
  if (_saveContactCacheTimer) return;
  _saveContactCacheTimer = setTimeout(() => {
    _saveContactCacheTimer = null;
    try {
      const obj = Object.fromEntries(contactNameCache);
      fs.writeFileSync(CONTACT_CACHE_PATH, JSON.stringify(obj, null, 2));
    } catch (e) { log("Failed to save contact cache:", String(e)); }
  }, 5000); // debounce 5s so rapid updates don't thrash disk
}

// Resolve a JID to a phone number string (e.g. '+919164536565'), or null if not possible.
function resolvePhoneNumber(jid) {
  if (!jid) return null;
  const lower = jid.toLowerCase();
  const suffix = lower.includes("@") ? lower.split("@")[1] : "";
  const local = lower.split("@")[0];
  // Already a phone JID
  if (suffix === "s.whatsapp.net" && /^\d+$/.test(local)) return `+${local}`;
  // LID — look up in our map
  if (suffix === "lid") {
    const phoneJid = lidToJidMap.get(lower);
    if (phoneJid) {
      const phoneLocal = phoneJid.split("@")[0];
      if (/^\d+$/.test(phoneLocal)) return `+${phoneLocal}`;
    }
    return null; // unknown, number hidden by WA
  }
  return null;
}

function unwrap(message) {
  if (!message) return {};
  if (message.ephemeralMessage) return unwrap(message.ephemeralMessage.message);
  if (message.viewOnceMessage) return unwrap(message.viewOnceMessage.message);
  if (message.viewOnceMessageV2) return unwrap(message.viewOnceMessageV2.message);
  return message;
}

function quotedText(ctx) {
  const q = ctx?.quotedMessage;
  if (!q) return "";
  return q.conversation || q.extendedTextMessage?.text || q.imageMessage?.caption || "";
}

function audioFormat(mimetype) {
  if (!mimetype) return "ogg";
  if (mimetype.includes("ogg")) return "ogg";
  if (mimetype.includes("mpeg") || mimetype.includes("mp3")) return "mp3";
  if (mimetype.includes("wav")) return "wav";
  return "ogg";
}

// ROOT CAUSE (ingest-whatsapp-3): every non-text/non-image/non-audio message used to
// fall into a single `else { body = "[unsupported message type]" }`. Reactions, stickers,
// GIFs/videos, documents, polls, locations and shared contacts are among the highest-
// frequency WhatsApp events; each was forwarded to Python as a REAL inbound text body, so
// the classifier drafted an Approve/Edit/Skip reply card whose content was the literal
// string "[unsupported message type]" — constant queue noise plus a foot-gun (reflexively
// approving a reply drafted against that placeholder could send nonsense to a VIP).
//
// Fix, in two parts:
//   (1) NON-ACTIONABLE control content (reactions, protocol/system msgs, poll VOTES) is
//       dropped at the relay — classifyMessage() returns {drop:true}, buildPayload returns
//       null, and the upsert handler skips forwarding. These can never become a card.
//   (2) Real media is labelled with its ACTUAL media_type ("sticker"/"video"/"document"/
//       "location"/"contact"/"poll") and a neutral, accurate placeholder body
//       (e.g. "[sticker]", "[document: invoice.pdf]") instead of the generic string, so
//       the Python side can treat a placeholder-only body as context and not draft to it.
// No em-dashes in any of these generated bodies (they are user-facing on the card).

// Pure, side-effect-free classifier over an UNWRAPPED Baileys message object.
// Returns { drop: true } | { mediaType, body, needsDownload, audioMime }.
function classifyMessage(m) {
  if (!m || typeof m !== "object") return { drop: true };
  // ── plain text ──────────────────────────────────────────────────────────────
  if (m.conversation) return { mediaType: "", body: String(m.conversation) };
  if (m.extendedTextMessage) return { mediaType: "", body: String(m.extendedTextMessage.text || "") };
  // ── downloadable media we describe on the Python side ─────────────────────────
  if (m.imageMessage) return { mediaType: "image", body: String(m.imageMessage.caption || ""), needsDownload: true };
  if (m.audioMessage) return { mediaType: "audio", body: "", needsDownload: true, audioMime: m.audioMessage.mimetype };
  // ── non-actionable control content: NEVER a reply card ────────────────────────
  // reactionMessage (👍 on a prior msg), protocolMessage (edits/revokes/system),
  // pollUpdateMessage (a VOTE on someone else's poll), and bare receipts carry no
  // owner-addressable text. Dropping them keeps them out of the processing ledger.
  if (m.reactionMessage || m.protocolMessage || m.pollUpdateMessage ||
      m.senderKeyDistributionMessage || m.messageContextInfo && Object.keys(m).length === 1) {
    return { drop: true };
  }
  // ── other media: label with the REAL type + an accurate placeholder body ──────
  if (m.stickerMessage) return { mediaType: "sticker", body: "[sticker]" };
  if (m.videoMessage) {
    const cap = String(m.videoMessage.caption || "");
    const isGif = m.videoMessage.gifPlayback === true;
    return { mediaType: "video", body: cap || (isGif ? "[gif]" : "[video]") };
  }
  if (m.documentMessage || m.documentWithCaptionMessage) {
    const doc = m.documentMessage || m.documentWithCaptionMessage?.message?.documentMessage || {};
    const fn = String(doc.fileName || doc.title || "").trim();
    const cap = String(doc.caption || "").trim();
    const label = fn ? `[document: ${fn}]` : "[document]";
    return { mediaType: "document", body: cap ? `${label} ${cap}` : label };
  }
  if (m.locationMessage || m.liveLocationMessage) {
    const loc = m.locationMessage || m.liveLocationMessage || {};
    const name = String(loc.name || loc.address || "").trim();
    return { mediaType: "location", body: name ? `[location: ${name}]` : "[location]" };
  }
  if (m.contactMessage || m.contactsArrayMessage) {
    const c = m.contactMessage;
    const arr = m.contactsArrayMessage?.contacts;
    const name = String(c?.displayName || (arr && arr[0]?.displayName) || "").trim();
    return { mediaType: "contact", body: name ? `[contact: ${name}]` : "[contact card]" };
  }
  if (m.pollCreationMessage || m.pollCreationMessageV2 || m.pollCreationMessageV3) {
    const poll = m.pollCreationMessage || m.pollCreationMessageV2 || m.pollCreationMessageV3 || {};
    const q = String(poll.name || "").trim();
    return { mediaType: "poll", body: q ? `[poll: ${q}]` : "[poll]" };
  }
  // Unknown / future type — keep the neutral placeholder but tag a real media_type so the
  // Python side can recognize it as a non-text body and not draft a reply to the literal.
  return { mediaType: "unsupported", body: "[unsupported message]" };
}

async function buildPayload(sock, msg) {
  const m = unwrap(msg.message);
  const remoteJid = msg.key.remoteJid || "";
  const isGroup = remoteJid.endsWith("@g.us");
  const senderJid = isGroup ? (msg.key.participant || remoteJid) : remoteJid;

  const cls = classifyMessage(m);
  if (cls.drop) {
    // Non-actionable control content (reaction/protocol/poll-vote): never forward.
    log(`drop non-actionable msg ${msg.key?.id || "?"} from ${senderJid}`);
    return null;
  }

  let body = cls.body || "";
  let mediaType = cls.mediaType || "";
  let mediaB64 = "";
  let audio_format = "";
  // contextInfo lives on whichever sub-message carried it; pull it generically.
  let ctx =
    m.extendedTextMessage?.contextInfo ||
    m.imageMessage?.contextInfo ||
    m.audioMessage?.contextInfo ||
    m.videoMessage?.contextInfo ||
    m.documentMessage?.contextInfo ||
    m.stickerMessage?.contextInfo ||
    null;

  if (cls.needsDownload) {
    if (mediaType === "audio") audio_format = audioFormat(cls.audioMime);
    try {
      const buf = await downloadMediaMessage(msg, "buffer", {},
        { logger, reuploadRequest: sock.updateMediaMessage });
      mediaB64 = buf.toString("base64");
    } catch (e) {
      log(`${mediaType} download failed:`, String(e));
    }
  }

  let groupName = "";
  if (isGroup) {
    if (groupNameCache.has(remoteJid)) {
      groupName = groupNameCache.get(remoteJid);
    } else {
      try {
        const meta = await sock.groupMetadata(remoteJid);
        groupName = meta?.subject || "";
        groupNameCache.set(remoteJid, groupName);
      } catch { groupName = ""; }
    }
  }

  // Prefer phone-saved contact name over pushName (sender's self-chosen name).
  const savedName = contactNameCache.get((senderJid || "").toLowerCase()) || "";
  const displayName = savedName || msg.pushName || "";

  // Resolve the real phone number — works for both regular JIDs and LID-alias JIDs.
  const phoneNumber = resolvePhoneNumber(senderJid);

  return {
    messageId: msg.key.id,
    jid: remoteJid,
    sender_jid: senderJid,
    push_name: displayName,
    phone_number: phoneNumber,   // '+919164536565' or null for unresolved LIDs
    body,
    media_type: mediaType,
    media_b64: mediaB64,
    audio_format,
    is_group: isGroup,
    group_name: groupName,
    quoted_body: quotedText(ctx),
    mentions: (ctx?.mentionedJid || []),
    timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
  };
}

// ── durable outbox (inbound + owner-outbound) with retention + redaction ──────
//
// ROOT CAUSE (config-secrets-deploy-6 — closes NO_SECRET_IN_LOGS): the outbox used to
// persist the FULL inbound payload (message body, quoted text, base64 media, sender JID,
// phone number, group name) as cleartext JSON with NO cap, NO age limit and NO sweep. If
// INGEST_TOKEN is set on the engine but not exported for the relay, every /inbound 401s,
// every payload is buffered, and replay 401s forever — so unlinkSync never fires and the
// directory grows by ~1500 plaintext message files/day, indefinitely, entirely outside
// the engine's RETENTION_* policy. A backup / iCloud sync / `git add` / disk theft then
// leaks the whole message corpus in cleartext.
//
// ROOT CAUSE (ingest-whatsapp-4): the owner's OWN outbound (presence/style signal) had a
// single best-effort fetch with NO durability. An engine-down window during a launchd
// cycle dropped it permanently; wa_messages then had no from_me row, last_outbound_ts=0,
// and presence.is_actively_handling returned False — so the agent drafted/pinged a chat
// the owner had just answered from his phone, breaking the presence invariant.
//
// Fix: ONE bounded, redacted, durable outbox abstraction used by BOTH inbound and
// owner-outbound. Buffered payloads are REDACTED at rest (no cleartext body / quoted text
// / media bytes — only a length + sha256 prefix for dedup/debug), the directory is capped
// by COUNT and AGE (pruned on startup and on every replay tick), and the depth is surfaced
// in status.json + logged when it grows, since a non-draining outbox reliably means the
// inbound path is broken (token mismatch or engine down) and the owner must act.

const OUTBOX_OUT_DIR = path.join(__dirname, "outbox_out"); // owner-outbound (ingest-whatsapp-4)
const OUTBOX_MAX_FILES = parseInt(process.env.RELAY_OUTBOX_MAX_FILES || "", 10) || 2000;
const OUTBOX_MAX_AGE_MS = (parseInt(process.env.RELAY_OUTBOX_MAX_AGE_HOURS || "", 10) || 72) * 3600 * 1000;

// Redact a payload for AT-REST persistence. We keep only the routing/identity fields the
// engine needs to re-key the message on replay, plus a length + content hash so the buffer
// is debuggable and dedupable WITHOUT storing the cleartext body. The full body is NOT
// re-derivable from the buffer; on replay the engine still gets enough to record presence
// and dedup, and an inbound that truly needs its body would have been delivered live (the
// buffer only exists when the live POST failed). No em-dashes in stored placeholders.
function redactForOutbox(payload) {
  const p = payload || {};
  const bodyStr = typeof p.body === "string" ? p.body : "";
  const quotedStr = typeof p.quoted_body === "string" ? p.quoted_body : "";
  const hash = (s) => {
    try { return crypto.createHash("sha256").update(String(s)).digest("hex").slice(0, 16); }
    catch { return ""; }
  };
  return {
    messageId: p.messageId,
    jid: p.jid,
    sender_jid: p.sender_jid,
    phone_number: p.phone_number || null,
    media_type: p.media_type || "",
    audio_format: p.audio_format || "",
    is_group: !!p.is_group,
    from_me: !!p.from_me,
    mentions: Array.isArray(p.mentions) ? p.mentions : [],
    timestamp: p.timestamp,
    // Redacted, non-reversible content references (NO cleartext, NO media bytes):
    body_redacted: true,
    body_len: bodyStr.length,
    body_sha256: bodyStr ? hash(bodyStr) : "",
    quoted_len: quotedStr.length,
    has_media: !!p.media_b64,
    buffered_at: Math.floor(Date.now() / 1000),
  };
}

// crypto is imported below (config-secrets-deploy-1 block); hoisting is fine for these
// function bodies because they only run at call time, well after module init.

function pruneOutbox(dir, nowMs = Date.now()) {
  let entries = [];
  try {
    entries = fs.readdirSync(dir)
      .filter((f) => f.endsWith(".json"))
      .map((f) => {
        const fp = path.join(dir, f);
        let mtimeMs = nowMs;
        try { mtimeMs = fs.statSync(fp).mtimeMs; } catch {}
        return { f, fp, mtimeMs };
      });
  } catch { return 0; }
  let removed = 0;
  // 1) Age cap: drop anything older than the retention window.
  for (const e of entries) {
    if (nowMs - e.mtimeMs > OUTBOX_MAX_AGE_MS) {
      try { fs.unlinkSync(e.fp); removed++; e._gone = true; } catch {}
    }
  }
  // 2) Count cap: if still over budget, drop the OLDEST first.
  const live = entries.filter((e) => !e._gone).sort((a, b) => a.mtimeMs - b.mtimeMs);
  let over = live.length - OUTBOX_MAX_FILES;
  for (let i = 0; i < live.length && over > 0; i++) {
    try { fs.unlinkSync(live[i].fp); removed++; over--; } catch {}
  }
  if (removed > 0) log(`outbox prune (${dir.endsWith("outbox_out") ? "out" : "in"}): removed ${removed} old/over-cap files`);
  return removed;
}

function outboxDepth(dir) {
  try { return fs.readdirSync(dir).filter((f) => f.endsWith(".json")).length; } catch { return 0; }
}

let _lastInboxDepth = 0;
function saveToOutbox(payload, dir = OUTBOX_DIR) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    pruneOutbox(dir); // enforce caps BEFORE writing so we never blow the budget
    const redacted = redactForOutbox(payload);
    fs.writeFileSync(path.join(dir, `${payload.messageId}.json`), JSON.stringify(redacted));
    const depth = outboxDepth(dir);
    if (dir === OUTBOX_DIR) {
      // A growing inbound outbox reliably indicates a broken inbound path; surface it.
      if (depth > _lastInboxDepth && depth % 25 === 0) {
        log(`WARNING: inbound outbox depth=${depth} and growing — inbound delivery may be broken (token mismatch or engine down).`);
      }
      _lastInboxDepth = depth;
    }
    log(`buffered to outbox (redacted, will retry): ${payload.messageId} [depth=${depth}]`);
  } catch (e) {
    log("outbox write failed:", String(e));
  }
}

// Shared secret for relay<->engine HTTP (config-secrets-deploy-1).
//
// ROOT CAUSE: the relay's /send, /read, /send_media and contact-dump endpoints had
// NO authentication and only bound 127.0.0.1. Localhost binding keeps remote hosts
// out but does NOT isolate processes on the same box, so any co-resident process
// could POST /send to message as the owner or GET /contacts to dump the directory.
// Fix: a shared secret (INGEST_TOKEN) is attached on every relay->engine post and
// REQUIRED on every engine->relay request (verified in constant time below). When
// the token is unset we keep the legacy localhost-only behavior (fail open) but warn
// loudly in live mode so it is never silently unauthenticated in production.
import crypto from "node:crypto";

const INGEST_TOKEN = (process.env.INGEST_TOKEN || "").trim();
const RELAY_MODE = (process.env.MODE || "dry_run").trim().toLowerCase();
const RELAY_IS_LIVE = RELAY_MODE === "live";
const AUTH_HEADER = "x-cos-token"; // Node lowercases all incoming header names

if (!INGEST_TOKEN) {
  if (RELAY_IS_LIVE) {
    log(
      "RELAY AUTH DISABLED: INGEST_TOKEN is not set while MODE=live — /send, /read, " +
      "/send_media and /contacts will accept UNAUTHENTICATED localhost requests. " +
      "Set INGEST_TOKEN (same value in .env and the relay env) to require a shared secret."
    );
  } else {
    log("INGEST_TOKEN not set (dry_run) — relay auth disabled; localhost bind only.");
  }
} else {
  log("Relay auth enabled: X-Cos-Token required on /send, /read, /send_media, /contacts.");
}

function pyHeaders() {
  const h = { "Content-Type": "application/json" };
  if (INGEST_TOKEN) h["X-Cos-Token"] = INGEST_TOKEN;
  return h;
}

// Constant-time compare so a co-resident process cannot time-probe the secret.
function tokenMatches(presented) {
  if (!presented) return false;
  const a = Buffer.from(String(presented));
  const b = Buffer.from(INGEST_TOKEN);
  if (a.length !== b.length) return false;
  try { return crypto.timingSafeEqual(a, b); } catch { return false; }
}

// Returns true if the request is authorized to hit a relay command endpoint.
// Token set  -> require a matching X-Cos-Token, else reject.
// Token unset -> fail open (legacy localhost-only), warning already logged at boot.
function relayAuthOk(req) {
  if (!INGEST_TOKEN) return true;
  return tokenMatches(req.headers[AUTH_HEADER]);
}

async function postInbound(payload) {
  const res = await fetch(PY_INBOUND_URL, {
    method: "POST",
    headers: pyHeaders(),
    body: JSON.stringify(payload),
  });
  return res.ok; // gate "delivered" on a 2xx — a 4xx/5xx means Python did NOT accept it
}

async function postOutbound(payload) {
  const res = await fetch(PY_OUTBOUND_URL, {
    method: "POST",
    headers: pyHeaders(),
    body: JSON.stringify(payload),
  });
  return res.ok; // a 401 (token mismatch) / 500 means Python did NOT record the presence signal
}

// ── delivery/read receipts (messages.update + message-receipt.update) ─────────
// Baileys timestamps/status are number|Long|null — coerce before sending so a Long does
// not serialize to {low,high} junk.
function coerceNum(x) {
  if (x == null) return 0;
  if (typeof x === "number") return x;
  try { return Number(x.toNumber ? x.toNumber() : (x.low != null ? x.low : x)); }
  catch { return Number(x) || 0; }
}

// Coalesce receipt chatter: WhatsApp emits ~4 status bumps per outbound. We only forward a
// status STRICTLY GREATER than the last one already sent for that id (the DB guard makes a
// dropped lower/equal status a safe no-op anyway). Bounded so memory stays flat.
const lastReceiptStatus = new Map(); // wa msg id -> highest status already forwarded
const RECEIPT_MAP_MAX = parseInt(process.env.RELAY_RECEIPT_MAP_MAX || "", 10) || 8000;

// Receipts are pure telemetry: best-effort fire-and-forget. NOT routed through the durable
// outbox — redactForOutbox whitelists fields that EXCLUDE status, so the outbox would strip
// the one field that matters. A dropped receipt self-heals: Baileys re-emits the terminal
// messages.update on reconnect and the engine's silence sweep backfills terminal state.
async function postReceipt(payload) {
  try {
    await fetch(PY_RECEIPT_URL, { method: "POST", headers: pyHeaders(), body: JSON.stringify(payload) });
  } catch { /* best-effort; self-heals on reconnect + sweep */ }
}

function forwardReceipt(key, status, ts, participant) {
  const id = key && key.id;
  const s = coerceNum(status);
  if (!id || s <= 0) return;
  if (!key.fromMe) return; // we only track the lifecycle of OUR OWN outbound messages
  // Coalesce per (message, recipient) so a group's per-member receipts advance independently
  // while a 1:1 (participant null) collapses to one stream per message id.
  const ckey = id + ":" + (participant || "");
  const prev = lastReceiptStatus.get(ckey) || 0;
  if (s <= prev) return; // never forward a non-advancing status
  lastReceiptStatus.set(ckey, s);
  if (lastReceiptStatus.size > RECEIPT_MAP_MAX) {
    const oldest = lastReceiptStatus.keys().next().value;
    if (oldest !== undefined) lastReceiptStatus.delete(oldest);
  }
  postReceipt({
    id,
    remoteJid: (key.remoteJid || "") ,
    fromMe: !!key.fromMe,
    participant: participant || key.participant || null,
    status: s,
    ts: ts ? coerceNum(ts) : Math.floor(Date.now() / 1000),
  });
}

// Forward the OWNER's own outbound message (sent from his phone/another device) as
// pure context: it tells the agent he's handling that chat and teaches his style.
//
// ingest-whatsapp-4: this is now DURABLE. The presence signal ("the owner is handling
// this chat himself -> don't ping") is load-bearing, so on ANY failure or non-2xx we
// buffer to a SEPARATE redacted outbox (OUTBOX_OUT_DIR) that replayOutbox drains every
// 20s. Replay is safe: the Python side prefixes the message_id with wa_out_, so it is
// idempotent. We previously inspected nothing (a 401/500 was silently dropped), which is
// exactly the engine-restart window where the owner steps in manually and gets pinged.
async function forwardOutbound(payload) {
  try {
    if (await postOutbound(payload)) {
      log(`recv own-msg ${payload.jid} -> python (context) ok`);
    } else {
      log(`python rejected own-msg ${payload.messageId} (non-2xx) — buffering (presence)`);
      saveToOutbox(payload, OUTBOX_OUT_DIR);
    }
  } catch (e) {
    log("forward outbound failed (buffering presence):", String(e));
    saveToOutbox(payload, OUTBOX_OUT_DIR);
  }
}

// Forward an incoming message. On ANY failure (network down, or a non-2xx from the
// receiver) the message is buffered to a disk outbox and retried — so a down or
// restarting assistant never silently loses a WhatsApp message. Python's intake is
// idempotent (dedup on message id), so a replay of an already-processed message is safe.
async function forwardToPython(payload) {
  try {
    if (await postInbound(payload)) {
      log(`recv ${payload.is_group ? "group" : "dm"} ${payload.sender_jid} -> python ok`);
    } else {
      log(`python rejected ${payload.messageId} (non-2xx) — buffering`);
      saveToOutbox(payload);
    }
  } catch (e) {
    log("forward to python failed (buffering):", String(e));
    saveToOutbox(payload);
  }
}

// Drain ONE outbox directory via `post`. Prunes the dir first (age/count caps), then
// replays each buffered (redacted) payload; on a 2xx the file is unlinked. A buffered
// payload is redacted, so replay restores the routing/presence signal (jid, sender,
// from_me, media_type, timestamp) — enough to re-key the message and record presence —
// without ever having stored the cleartext body. Returns the post-drain depth.
async function drainOutbox(dir, post) {
  pruneOutbox(dir); // enforce retention every tick so a stuck path cannot grow unbounded
  let files = [];
  try { files = fs.readdirSync(dir).filter((f) => f.endsWith(".json")); } catch { return 0; }
  for (const f of files) {
    const p = path.join(dir, f);
    let payload;
    try { payload = JSON.parse(fs.readFileSync(p, "utf-8")); }
    catch { try { fs.unlinkSync(p); } catch {} continue; }
    try {
      if (await post(payload)) { fs.unlinkSync(p); log(`replayed ${f} -> python ok`); }
    } catch { /* assistant still down; keep the file and try again next tick */ }
  }
  return outboxDepth(dir);
}

async function replayOutbox() {
  const inDepth = await drainOutbox(OUTBOX_DIR, postInbound);
  const outDepth = await drainOutbox(OUTBOX_OUT_DIR, postOutbound); // ingest-whatsapp-4
  // Surface depth so a non-draining outbox (broken inbound path) is visible, not silent.
  lastOutboxDepth = inDepth;
  lastOutboxOutDepth = outDepth;
}
if (IS_MAIN_MODULE) {
  // Prune once at startup so an outbox that accumulated while the relay was down does not
  // linger past its retention window (config-secrets-deploy-6).
  try { fs.mkdirSync(OUTBOX_DIR, { recursive: true }); pruneOutbox(OUTBOX_DIR); } catch {}
  try { fs.mkdirSync(OUTBOX_OUT_DIR, { recursive: true }); pruneOutbox(OUTBOX_OUT_DIR); } catch {}
  setInterval(replayOutbox, 20_000);
}

// ── outbound HTTP server (Python -> relay) ───────────────────────────────────
function startSendServer(getSock) {
  const lastKeys = startSendServer._lastKeys || (startSendServer._lastKeys = new Map());
  const server = http.createServer((req, res) => {
    // config-secrets-deploy-1: authenticate EVERY relay command/dump request. Localhost
    // bind alone does not isolate co-resident processes, so a shared secret gates /send,
    // /read, /send_media, /contacts and /resolve-lid. Reject unauthenticated with 401.
    if (!relayAuthOk(req)) {
      log(`unauthorized ${req.method} ${req.url} (bad/missing X-Cos-Token)`);
      res.writeHead(401, { "Content-Type": "application/json" });
      res.end('{"ok":false,"error":"unauthorized"}');
      return;
    }
    // GET /contacts — dump the full contact directory + LID→JID mappings
    if (req.method === "GET" && req.url === "/contacts") {
      const out = [];
      for (const [jid, name] of contactNameCache.entries()) {
        if (name && name.trim()) {
          const phoneNumber = resolvePhoneNumber(jid);
          out.push({ jid, name: name.trim(), phone_number: phoneNumber });
        }
      }
      const lidMap = Object.fromEntries(lidToJidMap);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ contacts: out, lid_jid_map: lidMap }));
      return;
    }
    // GET /resolve-lid?lid=9136083337274@lid — resolve a LID to phone number + name
    if (req.method === "GET" && req.url?.startsWith("/resolve-lid")) {
      const params = new URL(req.url, "http://x").searchParams;
      const lid = (params.get("lid") || "").toLowerCase();
      const phoneJid = lid ? lidToJidMap.get(lid) : null;
      const phoneNumber = lid ? resolvePhoneNumber(lid) : null;
      const name = lid ? (contactNameCache.get(lid) || (phoneJid ? contactNameCache.get(phoneJid) : null)) : null;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ lid, phone_jid: phoneJid || null, phone_number: phoneNumber, name: name || null }));
      return;
    }
    if (req.method !== "POST") { res.writeHead(404); res.end(); return; }
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", async () => {
      let body;
      try { body = JSON.parse(raw || "{}"); } catch { res.writeHead(400); res.end('{"ok":false}'); return; }
      const sock = getSock();
      if (!sock) { res.writeHead(503); res.end('{"ok":false,"error":"not connected"}'); return; }
      try {
        if (req.url === "/send") {
          await sleep(jitterMs()); // human-like delay
          const sent = await sock.sendMessage(body.jid, { text: body.text || "" });
          rememberAgentSend(sent?.key?.id); // so the fromMe echo isn't read as the owner
          log(`send -> ${body.jid} (${(body.text || "").length} chars)`);
          // Return the real WhatsApp message id so the engine can attach delivery/read
          // receipts to the agent's send (its row id is wa_<this id>). Without it, an
          // approved reply has no row for messages.update receipts to land on.
          res.writeHead(200); res.end(JSON.stringify({ ok: true, message_id: sent?.key?.id || "" }));
        } else if (req.url === "/read") {
          const key = lastKeys.get(body.jid);
          if (key) { try { await sock.readMessages([key]); } catch {} }
          log(`read -> ${body.jid}`);
          res.writeHead(200); res.end('{"ok":true}');
        } else if (req.url === "/send_media") {
          if (!body.jid || !body.url || !body.media_type) {
            res.writeHead(400); res.end('{"success":false,"error":"jid, url, and media_type are required"}'); return;
          }
          function downloadBuffer(url) {
            return new Promise((resolve, reject) => {
              const lib = url.startsWith('https') ? require('https') : require('http');
              lib.get(url, r => {
                const chunks = [];
                r.on('data', c => chunks.push(c));
                r.on('end', () => resolve(Buffer.concat(chunks)));
                r.on('error', reject);
              }).on('error', reject);
            });
          }
          try {
            const buffer = await downloadBuffer(body.url);
            let content;
            if (body.media_type === 'image') {
              content = { image: buffer, caption: body.caption || '' };
            } else if (body.media_type === 'video') {
              content = { video: buffer, caption: body.caption || '' };
            } else if (body.media_type === 'audio') {
              content = { audio: buffer, mimetype: 'audio/mp4', ptt: false };
            } else if (body.media_type === 'document') {
              content = { document: buffer, mimetype: 'application/octet-stream', fileName: body.filename || 'file' };
            } else {
              res.writeHead(400); res.end('{"success":false,"error":"unsupported media_type"}'); return;
            }
            await sock.sendMessage(body.jid, content);
            log(`send_media (${body.media_type}) -> ${body.jid}`);
            res.writeHead(200); res.end('{"success":true}');
          } catch (e) {
            log("send_media failed:", String(e));
            res.writeHead(500); res.end(JSON.stringify({ success: false, error: e.message }));
          }
        } else if (req.url === "/resolve-lid") {
          // Try sock.onWhatsApp(lid) to discover real phone JID from a LID.
          // WhatsApp may or may not respond depending on version/privacy settings.
          const lid = (body.lid || "").toLowerCase();
          if (!lid || !lid.includes("@lid")) {
            res.writeHead(400); res.end('{"ok":false,"error":"lid param required"}'); return;
          }
          try {
            const results = await sock.onWhatsApp(lid);
            if (results && results.length > 0) {
              const r = results[0];
              const phoneJid = (r.jid || "").toLowerCase();
              const phoneLocal = phoneJid.split("@")[0];
              const phoneNumber = /^\d+$/.test(phoneLocal) ? `+${phoneLocal}` : null;
              if (phoneJid && phoneJid !== lid) {
                lidToJidMap.set(lid, phoneJid);
                jidToLidMap.set(phoneJid, lid);
                scheduleLidMapSave();
                // Also cache name under both keys
                const name = contactNameCache.get(lid) || contactNameCache.get(phoneJid);
                if (name) { contactNameCache.set(phoneJid, name); scheduleContactCacheSave(); }
                log(`resolve-lid: ${lid} → ${phoneJid} (${phoneNumber})`);
              }
              res.writeHead(200); res.end(JSON.stringify({ ok: true, lid, phone_jid: phoneJid, phone_number: phoneNumber }));
            } else {
              res.writeHead(200); res.end(JSON.stringify({ ok: false, lid, phone_jid: null, phone_number: null, reason: "not found via onWhatsApp" }));
            }
          } catch (e) {
            log("resolve-lid failed:", String(e));
            res.writeHead(200); res.end(JSON.stringify({ ok: false, lid, phone_jid: null, phone_number: null, reason: String(e) }));
          }
        } else {
          res.writeHead(404); res.end('{"ok":false}');
        }
      } catch (e) {
        log("send/read failed:", String(e));
        res.writeHead(500); res.end('{"ok":false}');
      }
    });
  });
  server.listen(SEND_PORT, "127.0.0.1", () =>
    log(`relay send server on 127.0.0.1:${SEND_PORT} (/send, /read)`));
  return lastKeys;
}

// ── main connect loop with exponential backoff ───────────────────────────────
let sock = null;
let backoff = 1000;

async function connect() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  let version;
  try { ({ version } = await fetchLatestBaileysVersion()); } catch { version = undefined; }

  sock = makeWASocket({ version, auth: state, logger, printQRInTerminal: false });
  const lastKeys = startSendServer.__started ? startSendServer._lastKeys : startSendServer(() => sock);
  startSendServer.__started = true;

  sock.ev.on("creds.update", saveCreds);

  // Populate contactNameCache with phone-saved names from all contact/chat events.
  // c.name  = what Jatin saved this person as in his phone
  // c.notify = what they set as their WhatsApp push_name
  // We prefer c.name (phone-book) over c.notify (their own choice).
  function cacheContact(c) {
    // Prefer phone-book name (c.name) over WhatsApp push_name (c.notify).
    const name = (c.name || "").trim() || (c.notify || "").trim() || (c.verifiedName || "").trim();
    if (c.id && name) {
      contactNameCache.set(c.id.toLowerCase(), name);
      scheduleContactCacheSave();
    }
    // Build LID ↔ phone JID map.
    // Case A: c.id is a phone JID and c.lid is the linked alias.
    if (c.id && c.lid) {
      const phoneJid = c.id.toLowerCase();
      const lid = c.lid.toLowerCase();
      lidToJidMap.set(lid, phoneJid);
      jidToLidMap.set(phoneJid, lid);
      // Also cache name under the LID so LID-sourced messages get the right name.
      if (name) contactNameCache.set(lid, name);
      scheduleLidMapSave();
    }
    // Case B: c.id is itself a LID (ends @lid) — store name under it too.
    if (c.id && c.id.toLowerCase().endsWith("@lid") && name) {
      contactNameCache.set(c.id.toLowerCase(), name);
      scheduleContactCacheSave();
    }
  }

  sock.ev.on("contacts.upsert", (contacts) => {
    contacts.forEach(cacheContact);
    // Log first contact's full fields so we can see if c.lid is exposed
    if (contacts.length > 0) {
      const sample = contacts[0];
      const keys = Object.keys(sample).filter(k => sample[k]);
      log(`contacts.upsert sample fields: ${keys.join(", ")} (${contacts.length} total)`);
    }
  });
  sock.ev.on("contacts.update", (updates) => { updates.forEach(cacheContact); });

  // POST /resolve-lid → try to resolve a LID to a real phone JID via sock.onWhatsApp
  // This is exposed as an HTTP endpoint so Python can call it on demand.

  // chats.upsert fires on connection with recent chats — each carries the contact name.
  sock.ev.on("chats.upsert", (chats) => {
    let added = 0;
    for (const chat of chats) {
      const name = (chat.name || "").trim();
      if (chat.id && name) {
        if (!contactNameCache.has(chat.id.toLowerCase())) {
          contactNameCache.set(chat.id.toLowerCase(), name);
          added++;
        }
      }
    }
    if (added > 0) scheduleContactCacheSave();
    log(`chats.upsert: ${chats.length} chats, +${added} new names — total ${contactNameCache.size}.`);
  });

  sock.ev.on("chats.update", (chats) => {
    let added = 0;
    for (const chat of chats) {
      const name = (chat.name || "").trim();
      if (chat.id && name && !contactNameCache.has(chat.id.toLowerCase())) {
        contactNameCache.set(chat.id.toLowerCase(), name);
        added++;
      }
    }
    if (added > 0) { scheduleContactCacheSave(); log(`chats.update +${added} — total ${contactNameCache.size}.`); }
  });

  sock.ev.on("connection.update", (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      log("Scan this QR in WhatsApp → Linked devices → Link a device:");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      connected = true; backoff = 1000;
      log("WhatsApp connected.");
      writeStatus();
      // Fetch full contact directory from WA app state so contactNameCache fills up.
      setTimeout(async () => {
        try {
          await sock.resyncAppState(["critical_block", "critical_unblock_low", "regular_high", "regular_low", "regular"], false);
          log(`Contact sync done — ${contactNameCache.size} contacts cached.`);
        } catch (e) {
          log("Contact app-state sync failed (non-fatal):", String(e));
        }
      }, 3000);
    } else if (connection === "close") {
      connected = false; writeStatus();
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        log("Logged out. Delete relay/session/ and re-pair (see RELAY_README.md). Exiting.");
        process.exit(1);
      }
      const wait = Math.min(backoff, 60_000);
      log(`Connection closed (code ${code}). Reconnecting in ${wait}ms…`);
      setTimeout(connect, wait);
      backoff = Math.min(backoff * 2, 60_000);
    }
  });

  // Delivery/read lifecycle of OUR outbound (owner-phone + agent-approved sends). Pure
  // telemetry: forwarded to /receipt, never a card. update.status is WebMessageInfo.Status
  // (1=PENDING 2=SERVER_ACK 3=DELIVERY_ACK 4=READ 5=PLAYED).
  sock.ev.on("messages.update", (updates) => {
    for (const u of updates || []) {
      try {
        const st = u && u.update ? u.update.status : undefined;
        if (st == null || !u.key || !u.key.id) continue;
        forwardReceipt(u.key, st, undefined, null);
      } catch (e) { log("messages.update handling error:", String(e)); }
    }
  });

  // Per-recipient read receipts — essential for groups (context only; the engine's
  // "not coming back" nudge stays 1:1). Derive status from which timestamp is present.
  sock.ev.on("message-receipt.update", (receipts) => {
    for (const r of receipts || []) {
      try {
        const rc = r && r.receipt;
        if (!rc || !r.key || !r.key.id) continue;
        const status = rc.readTimestamp ? 4 : (rc.playedTimestamp ? 5 : (rc.receiptTimestamp ? 3 : 0));
        if (status <= 0) continue;
        const ts = rc.readTimestamp || rc.playedTimestamp || rc.receiptTimestamp;
        forwardReceipt(r.key, status, ts, rc.userJid || null);
      } catch (e) { log("message-receipt.update handling error:", String(e)); }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        if (!msg.message) continue;
        if (msg.key.remoteJid === "status@broadcast") continue;
        if (msg.key.fromMe) {
          // The owner's own message. Skip the echo of the agent's own sends; forward a
          // genuine self-sent message as CONTEXT only (presence + style), never processed.
          const id = msg.key.id;
          if (consumeAgentSend(id)) continue; // still-live echo of our own /send — drop
          const payload = await buildPayload(sock, msg);
          if (!payload) continue; // non-actionable (reaction/protocol) — skip entirely
          payload.from_me = true;
          await forwardOutbound(payload);
          continue;
        }
        // remember a key per chat so /read can mark it read
        startSendServer._lastKeys?.set(msg.key.remoteJid, msg.key);
        const payload = await buildPayload(sock, msg);
        if (!payload) continue; // non-actionable (reaction/protocol/poll-vote) — never a card
        bumpMessageCount(); // only count real, forwardable inbound messages
        await forwardToPython(payload);
      } catch (e) {
        log("message handling error:", String(e));
      }
    }
  });
}

if (IS_MAIN_MODULE) {
  log(`Relay starting. Python /inbound = ${PY_INBOUND_URL}; listening for /send on ${SEND_PORT}.`);
  writeStatus();
  connect().catch((e) => { log("fatal:", String(e)); process.exit(1); });
}

// ── test-only exports ─────────────────────────────────────────────────────────
// Pure / near-pure helpers exported so `node --test` can verify the regression fixes
// (ingest-whatsapp-3/4/5, config-secrets-deploy-6) WITHOUT booting the relay. This block
// is additive and has no effect on the running relay (importing is inert per
// IS_MAIN_MODULE). Internal state maps (agentSentIds) are exposed read-only for assertions.
export const __test__ = {
  classifyMessage,
  redactForOutbox,
  pruneOutbox,
  outboxDepth,
  rememberAgentSend,
  consumeAgentSend,
  pruneAgentSends,
  agentSentIds,
  saveToOutbox,
  OUTBOX_DIR,
  OUTBOX_OUT_DIR,
  AGENT_SEND_TTL_MS,
  AGENT_SEND_MAX,
  coerceNum,
  forwardReceipt,
  lastReceiptStatus,
};
