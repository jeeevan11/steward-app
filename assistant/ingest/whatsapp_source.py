"""WhatsApp channel as a `MailSource` — feeds the existing brain unchanged.

Shape:
  * A dumb Node relay (relay/whatsapp_relay.js) holds the Baileys session and POSTs
    each incoming message to this process at 127.0.0.1:<relay_port>/inbound, and
    accepts /send + /read back on <send_port>.
  * `ingest_payload` is the durable intake: it persists the raw payload (so a crash
    never drops a message) and applies the hard-coded GROUP-SKIP rule before anything
    reaches the brain.
  * `WhatsAppSource` implements MailSource: `fetch_new_message_ids` drains the inbox
    (recording each in the ledger before handing it off), `get_thread` rebuilds the
    Message (transcribing voice notes best-effort), and the act/send methods map to
    WhatsApp semantics (archive = mark read; send = relay /send).

Personal JIDs (PERSONAL_JIDS) are stamped with a `"personal"` contact flag at intake
so the existing guardrail floors them to Tier 3 (never auto-handled).

Pure helpers (normalize / should_skip_group / ingest_payload) are import-safe and
unit-tested without sockets, Baileys, or the network.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from assistant.config import Settings
from assistant.ingest.base import MailSource
from assistant.llm.client import LLMError
from assistant.logging_setup import get_logger
from assistant.models import Channel, Message, Thread
from assistant.storage import db, ledger
from assistant.storage import repositories as repo
from assistant.storage import wa_messages
from assistant.storage import whatsapp_inbox as inbox

log = get_logger("ingest.whatsapp")

WA_PREFIX = "wa_"


class WhatsAppSendUnconfirmed(RuntimeError):
    """failure-recovery-5: raised by send_reply (in strict mode) when the relay accepts a
    /send with HTTP 200 but provides NO delivery confirmation. execute_send catches it like
    any send-time exception and routes the action to SEND_AMBIGUOUS — maybe-delivered, never
    auto-resent, surfaced to the owner — rather than asserting a delivery that may not have
    happened."""


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no I/O) — unit-tested
# ─────────────────────────────────────────────────────────────────────────────
def wa_id(message_id: str) -> str:
    """Ledger id for a WhatsApp message — prefixed so it can never collide with a
    Gmail id in the shared ledger."""
    return f"{WA_PREFIX}{message_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Relay HTTP auth (config-secrets-deploy-1)
# ─────────────────────────────────────────────────────────────────────────────
# ROOT CAUSE: the relay HTTP listeners (/send, /read, /send_media, and the inbound
# receiver) were reachable by ANY co-resident process on the box. Binding to
# 127.0.0.1 keeps remote hosts out, but it does NOT separate processes on the same
# machine, so any local process (or browser via a forged form post) could send as
# the owner or dump the contact directory. The documented INGEST_TOKEN was only ever
# enforced opt-in, and the Python relay client never attached it at all. Fix: a
# shared secret (INGEST_TOKEN -> Settings.ingest_token) is attached on every
# engine->relay request and verified on every relay->engine request. When the token
# is unset we cannot break the existing localhost-only deployments, so we fail OPEN
# but log a loud, repeated warning in live mode (never silently — see auth_ok).
AUTH_HEADER = "X-Cos-Token"


def relay_token(settings: Settings) -> str:
    """The shared secret used to authenticate relay<->engine HTTP. Empty when unset."""
    return (getattr(settings, "ingest_token", "") or "").strip()


def relay_auth_headers(settings: Settings) -> dict[str, str]:
    """Headers to attach to an engine->relay request. Adds the shared-secret header
    when a token is configured; an empty token yields no auth header (backward compat
    with localhost-only deployments)."""
    headers = {"Content-Type": "application/json"}
    token = relay_token(settings)
    if token:
        headers[AUTH_HEADER] = token
    return headers


def auth_ok(settings: Settings, presented: Optional[str], *, endpoint: str = "") -> bool:
    """Verify a presented token against the configured shared secret.

    - Token configured: require a constant-time match; reject otherwise.
    - Token unset + live mode: FAIL OPEN but log a loud warning every time, so an
      unauthenticated relay is never silently accepted in production (the finding
      allows fail-closed OR a loud warning; we choose the warning to avoid breaking
      already-running localhost-only deployments — additive, never weaker).
    - Token unset + dry_run: allow quietly (dev/default)."""
    expected = relay_token(settings)
    if not expected:
        # No secret configured. Never silently allow in live — shout about it.
        if not getattr(settings, "dry_run", True):
            log.warning(
                "RELAY AUTH DISABLED: INGEST_TOKEN is not set while MODE=live — the "
                "relay endpoint %r is accepting UNAUTHENTICATED localhost requests. "
                "Set INGEST_TOKEN in .env (and the relay env) to require a shared secret.",
                endpoint or "?",
            )
        return True
    if not presented:
        return False
    # Constant-time compare so a co-resident process can't time-probe the secret.
    return hmac.compare_digest(str(presented), expected)


# ingest-whatsapp-2 ROOT CAUSE: messageTimestamp (`ts`) is SENDER-controlled and stored
# unvalidated. plan_settling and get_thread sorted bursts by `ts`, so a forged/future ts
# (e.g. a 2099 epoch) made an attacker-chosen line the burst representative — the line the
# card quotes and the draft is anchored to — and could reorder the recent-conversation
# context fed to the LLM. The receive clock (`created_at`) is OUR clock and cannot be
# spoofed. Fix: when ordering, key on created_at FIRST and use a CLAMPED ts only as a
# tiebreaker, so a wildly out-of-range sender clock can never pick the representative or
# reorder context. (Storage-layer clamping at write time lives in whatsapp_inbox.put /
# wa_messages.record, which are owned by another cluster — this read-time clamp is the
# additive defense we can land here, and it fully neutralizes the representative/ordering
# attack regardless of what was stored.)
_TS_SKEW_PAST = 86400      # accept up to 1 day behind the receive clock
_TS_SKEW_FUTURE = 300      # accept up to 5 min ahead (minor real clock drift)


def _clamped_ts(ts: Any, created_at: Any) -> int:
    """Return a sender ts that is plausible relative to OUR receive clock, else fall back
    to created_at. Used only for ordering — never mutates stored data. Pure, unit-tested."""
    try:
        t = int(ts or 0)
    except (TypeError, ValueError):
        t = 0
    try:
        c = int(created_at or 0)
    except (TypeError, ValueError):
        c = 0
    if c <= 0:
        # No trustworthy receive clock to compare against — keep ts but never let a
        # negative/garbage value through.
        return max(0, t)
    if (c - _TS_SKEW_PAST) <= t <= (c + _TS_SKEW_FUTURE):
        return t
    return c  # sender clock implausible → use the receive clock for ordering


def _order_key(r: Any) -> tuple:
    """Burst/thread ordering key. created_at (receive clock) is PRIMARY so a spoofed ts
    can never choose the representative; a clamped ts then a stable message_id break ties."""
    try:
        created = int(r["created_at"] or 0)
    except (TypeError, ValueError, KeyError):
        created = 0
    return (created, _clamped_ts(r["ts"], r["created_at"]), r["message_id"])


def should_skip_group(payload: dict[str, Any], settings: Settings) -> bool:
    """Group messages are skipped UNLESS they @mention the user or contain a watch
    keyword. Hard rule, not AI. Non-group messages are never skipped here."""
    if not payload.get("is_group"):
        return False
    body = (payload.get("body") or "").lower()
    mentions = [m.lower() for m in (payload.get("mentions") or [])]
    me = (settings.wa_user_jid or "").lower()
    if me and (me in mentions or me in body):
        return False
    for kw in settings.watch_keywords:
        if kw and kw in body:
            return False
    return True


def plan_settling(
    rows: list[Any],
    now: float,
    *,
    settle: int,
    max_hold: int,
    group_settle: int,
    group_max_hold: int,
    instant_jids: Any = frozenset(),
    per_jid_settle: dict = {},
    instant_settle: int = 0,
) -> list[tuple[str, list[str]]]:
    """Pure settling/debounce planner. Given the un-processed ('new') inbox rows,
    group them by conversation (jid) and return ``[(representative_id, [member_ids])]``
    for every conversation that has SETTLED — i.e. it has been quiet for its settle
    window, OR it has been held longer than its max-hold cap (so a slow trickle is
    never starved). Conversations that are still active are omitted (held for later).

    The representative is the LATEST message in the burst (newest context wins, and the
    card's quote shows the most recent line); the members are the earlier lines that
    fold into it. Settling uses ``created_at`` (our receive clock — no sender skew);
    ordering within the burst is keyed on ``created_at`` FIRST (so a spoofed/future
    sender ``ts`` can never become the representative — ingest-whatsapp-2), with a
    clamped ``ts`` only as a tiebreaker. Groups use the far longer group windows, so we
    never surface while a group is active.

    ``instant_jids`` are VIP "always-instant" conversations. ingest-whatsapp-6 ROOT
    CAUSE: these were released with quiet=True UNCONDITIONALLY, so a VIP's multi-line
    thought that spanned a poll boundary fragmented into one card PER poll — lines 1-3
    surfaced at t=poll, then lines 4-6 (now the only 'new' rows) could not fold into the
    already-queued representative and surfaced as a SECOND disjoint card with a partial
    draft. Exactly the highest-value contacts (boss/spouse) got the worst behavior.
    Fix: instant jids use a TINY settle window (``instant_settle`` seconds, ~one poll
    interval) instead of an immediate release — so consecutive lines within that window
    collapse into one card, while latency stays near-instant (release the moment the VIP
    pauses for one short window). They keep a generous max-hold cap so a steady stream is
    never starved. instant_settle=0 preserves the legacy immediate-release behavior.

    No I/O — unit-tested without a DB. Each row only needs message_id, jid, is_group,
    ts, created_at (works on sqlite3.Row or a plain dict)."""
    by_jid: dict[str, list[Any]] = {}
    for r in rows:
        key = (r["jid"] or r["message_id"])
        by_jid.setdefault(key, []).append(r)

    released: list[tuple[str, list[str]]] = []
    for key, group in by_jid.items():
        last_seen = max(int(r["created_at"] or 0) for r in group)
        first_seen = min(int(r["created_at"] or 0) for r in group)
        if key in instant_jids:
            # ingest-whatsapp-6: VIP gets a TINY settle window (not immediate release) so a
            # multi-poll burst collapses into one card. instant_settle<=0 → legacy instant.
            if instant_settle <= 0:
                quiet, capped = True, False
            else:
                quiet = (now - last_seen) >= instant_settle
                # Never starve a VIP who keeps typing: still honor the normal max-hold cap.
                capped = (now - first_seen) >= max_hold
        else:
            is_grp = bool(group[0]["is_group"])
            window = group_settle if is_grp else per_jid_settle.get(key, settle)
            cap = group_max_hold if is_grp else max_hold
            quiet = (now - last_seen) >= window
            capped = (now - first_seen) >= cap
        if not (quiet or capped):
            continue  # still active — let it calm down
        # ingest-whatsapp-2: created_at-primary ordering (clamped ts as tiebreaker) so the
        # representative is the genuinely-latest RECEIVED line, not a forged-future one.
        ordered = sorted(group, key=_order_key)
        rep = ordered[-1]["message_id"]
        members = [r["message_id"] for r in ordered[:-1]]
        released.append((rep, members))
    return released


def _hour_in_quiet_window(hour: int, start: int, end: int) -> bool:
    """Pure: is `hour` (0-23) inside the quiet window [start, end)? Handles a window that
    wraps midnight (start > end, e.g. 22->8). start == end means the window is disabled.
    No I/O — unit-tested."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def _body_for(payload: dict[str, Any], transcript: Optional[str],
              description: Optional[str] = None) -> str:
    """GAP 8 — build the message body for a media payload.

    audio: the transcript when available, else a "could not transcribe" placeholder.
    image: the model's one-sentence description (with any caption), else "[image]".
    Anything else: the raw body."""
    mt = (payload.get("media_type") or "").lower()
    if mt == "audio":
        if transcript:
            return f"[voice note, transcribed]: {transcript}"
        existing = payload.get("body") or ""
        if existing:           # already-cached transcript or placeholder
            return existing
        return "[voice note — could not transcribe]"
    if mt == "image":
        cap = (payload.get("body") or "").strip()
        if description:
            return f"[image: {description}]" + (f" {cap}" if cap else "")
        existing = payload.get("body") or ""
        # A previously-stored description body (starts with "[image") passes through.
        if existing.startswith("[image"):
            return existing
        return f"[image]{(' ' + cap) if cap else ''}"
    return payload.get("body") or ""


def normalize(payload: dict[str, Any], settings: Settings, transcript: Optional[str] = None) -> Message:
    """Raw relay payload → channel-agnostic Message."""
    jid = (payload.get("jid") or "").lower()
    sender = (payload.get("sender_jid") or jid or "").lower()
    body = _body_for(payload, transcript)
    quoted = payload.get("quoted_body")
    if quoted:
        # Mark the reply-quote as labelled CONTEXT (the earlier message being answered) so the
        # drafter doesn't echo it back — but keep the marker as DATA, not an instruction, and
        # defang+cap the sender-controlled quoted text (it reaches the drafter, which lacks the
        # classifier's untrusted-isolation). The "treat as context, don't repeat" guidance lives
        # in the TRUSTED drafting prompt, never in this untrusted body.
        from assistant.brain.classifier import _defang
        safe = _defang(str(quoted)).replace("\n", " ").strip()[:280]
        body = f"[context: replying to an earlier message that said: \"{safe}\"]\n{body}"
    return Message(
        id=wa_id(str(payload.get("messageId", ""))),
        thread_id=jid or sender,
        channel=Channel.WHATSAPP,
        sender_email=sender,                 # the JID is the contact key
        sender_name=payload.get("push_name") or sender,
        recipients=[settings.wa_user_jid] if settings.wa_user_jid else [],
        subject=(payload.get("group_name") or "") if payload.get("is_group") else "",
        body_text=body,
        snippet=body[:200],
        timestamp=float(payload.get("timestamp") or 0),
        from_me=False,
    )


def _row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    mid = row["message_id"]
    raw_id = mid[len(WA_PREFIX):] if mid.startswith(WA_PREFIX) else mid
    return {
        "messageId": raw_id,
        "jid": row["jid"],
        "sender_jid": row["sender_jid"],
        "push_name": row["push_name"],
        "body": row["body"],
        "media_type": row["media_type"],
        "is_group": bool(row["is_group"]),
        "group_name": row["group_name"],
        "quoted_body": row["quoted_body"],
        "mentions": (row["mentions"] or "").split(",") if row["mentions"] else [],
        "timestamp": row["ts"],
    }


def _bridge_lid_to_resolved_number(conn: sqlite3.Connection, lid: str, phone_number: str) -> None:
    """When the relay hands us an @lid's real number, link the @lid to the person that already
    owns that number (address-book seed or a prior save). Attach-only: never creates a new
    person, never steals a link already pointing elsewhere, never merges two persons."""
    from assistant.storage import repositories as repo
    lid = (lid or "").strip().lower()
    digits = "".join(c for c in (phone_number or "") if c.isdigit())
    if not lid.endswith("@lid") or len(digits) < 7:
        return
    if repo.person_link_get(conn, lid):
        return                                   # @lid already resolved — nothing to do
    phone_jid = f"{digits}@s.whatsapp.net"
    pid = repo.person_link_get(conn, phone_jid)
    if not pid:                                  # try a trailing-national-digits match too
        pid = repo.person_link_by_phone_digits(conn, digits)
    if pid:
        repo.person_link_set(conn, lid, pid, confidence=1.0, source="relay_resolved")


def stamp_rule_flags(conn: sqlite3.Connection, sender_jid: str, push_name: str, settings: Settings) -> None:
    """Apply config-seeded per-contact rule flags before the brain ever sees the
    contact: personal (always Tier 3), vip (always-instant), mute (silently handled).
    Idempotent — only writes when a flag is actually missing. VIP wins over mute if a
    JID is mis-listed in both."""
    s = (sender_jid or "").lower()
    if not s:
        return
    wanted: set[str] = set()
    if s in set(settings.personal_jids):
        wanted.add("personal")
    if s in set(settings.vip_jids):
        wanted.add("vip")
    if s in set(settings.mute_jids):
        wanted.add("mute")
    if "vip" in wanted:
        wanted.discard("mute")
    if not wanted:
        return
    c = repo.get_or_default_contact(conn, s, push_name)
    if wanted - c.flags:
        c.flags |= wanted
        repo.upsert_contact(conn, c)


# Back-compat alias (the original name; some callers/tests may still reference it).
stamp_personal_flag = stamp_rule_flags


# ─────────────────────────────────────────────────────────────────────────────
# Durable intake (called by the receiver) — testable without sockets
# ─────────────────────────────────────────────────────────────────────────────
def ingest_payload(conn: sqlite3.Connection, settings: Settings, payload: dict[str, Any]) -> Optional[str]:
    """Persist a payload and apply the group-skip rule. Returns the ledger id if the
    message should be processed, or None if it was skipped (group rule)."""
    raw_id = str(payload.get("messageId") or "").strip()
    if not raw_id:
        return None
    mid = wa_id(raw_id)

    # Universal context: record EVERY inbound message to the history first — even group
    # chatter we will not surface. The agent must always know what's happening; the
    # skip/settle/suppress logic only ever affects whether it PINGS, never what it knows.
    try:
        wa_messages.record(conn, {**payload, "message_id": mid}, from_me=False)
    except Exception:  # noqa: BLE001 - context capture is best-effort
        log.debug("wa_messages inbound record failed (non-fatal)", exc_info=True)

    # Stamp personal flag now so it's set before the brain ever sees the contact.
    stamp_rule_flags(conn, (payload.get("sender_jid") or payload.get("jid") or ""),
                     payload.get("push_name", ""), settings)

    # L2: if the relay resolved this @lid's real number, bridge the @lid onto whatever person
    # already owns that number (e.g. one seeded from the address book) — so a privacy @lid
    # auto-resolves to the saved name instead of fragmenting into a new unknown person. The
    # relay-supplied number is WhatsApp's own equivalence (trusted), and we only ever ATTACH
    # to an existing person, never steal a link or merge two persons (NO_AUTO_MERGE).
    try:
        _bridge_lid_to_resolved_number(
            conn, (payload.get("sender_jid") or "").lower(), payload.get("phone_number") or "")
    except Exception:  # noqa: BLE001 - best-effort linkage, never block intake
        log.debug("lid→number bridge skipped (non-fatal)", exc_info=True)

    if should_skip_group(payload, settings):
        inbox.put(conn, mid, payload, status="skipped")
        # Atomic: record DONE in one statement so a concurrent poller can never
        # claim it during a SEEN→DONE window and accidentally process (or reply in)
        # a group it was supposed to skip.
        ledger.record_skipped(conn, mid, payload.get("jid", ""), "group_skipped")
        log.info("group message skipped (no mention/keyword): %s", mid)
        return None

    inbox.put(conn, mid, payload, status="new")
    return mid


def ingest_receipt(conn: sqlite3.Connection, payload: dict[str, Any]) -> bool:
    """Apply a delivery/read receipt for one of OUR outbound WhatsApp messages.

    Pure inbound telemetry — it updates the delivery lifecycle (status/delivered_at/read_at)
    on an EXISTING wa_messages row and NOTHING else. It never enters the inbox/ledger, never
    creates a card, and never sends — so a receipt physically cannot violate NO_AUTO_SEND.
    Best-effort; a malformed receipt or one for an unknown message is a harmless no-op.
    """
    raw_id = str(payload.get("id") or payload.get("messageId") or "").strip()
    if not raw_id:
        return False
    try:
        status = int(payload.get("status"))
    except (TypeError, ValueError):
        return False
    ts = payload.get("ts") or payload.get("timestamp")
    return wa_messages.apply_receipt(
        conn,
        raw_id=raw_id,
        status=status,
        ts=int(ts) if ts else None,
        remote_jid=(payload.get("remoteJid") or payload.get("jid") or ""),
        participant=(payload.get("participant") or ""),
    )


def ingest_outbound(conn: sqlite3.Connection, payload: dict[str, Any],
                    settings: Optional[Settings] = None) -> None:
    """Record a message the OWNER sent himself (from his phone or another device) as context +
    presence + style signal, AND — the cross-surface fix — clear any pending card for that
    chat, because he has already replied to it elsewhere."""
    raw_id = str(payload.get("messageId") or "").strip()
    jid = (payload.get("jid") or "").strip()
    if not raw_id or not jid:
        return
    # The relay's fromMe echo of one of STEWARD's OWN approved sends can slip past its 6h
    # agentSentIds TTL (a reconnect-delayed app-state resync) and arrive here. That is NOT the
    # owner texting himself: send_reply already recorded it as `wa_<id>` (is_agent=1). Re-recording
    # it as `wa_out_<id>` (is_agent=0) would pollute presence + style and falsely cross-surface-
    # close cards. Recognize it by the existing agent row and drop it entirely.
    if wa_messages.is_agent_send(conn, raw_id):
        log.debug("ingest_outbound: skipping delayed echo of agent send %s", raw_id)
        return
    mid = "wa_out_" + raw_id
    try:
        wa_messages.record(conn, {**payload, "message_id": mid}, from_me=True)
    except Exception:  # noqa: BLE001 - best-effort context
        log.debug("wa_messages outbound record failed (non-fatal)", exc_info=True)

    # Cross-surface resolution: the owner replied to this chat himself, so any open card for it
    # is already handled — close it (HANDLED_ELSEWHERE) so he can never approve a stale card and
    # send a SECOND reply. 1:1 only (a group post must not clear a group card). Only PENDING
    # rows are touched, so a reply Steward itself just sent (SENDING/SENT) is never affected.
    if jid.endswith("@g.us"):
        return
    try:
        closed = repo.resolve_handled_elsewhere(conn, jid)
        # A tier-3 ASK is deliberately NOT auto-closed — you messaging the chat is not the same
        # as making the decision it's waiting on (the 'Sam $250k' incident). Keep it visible.
        kept_asks = repo.open_asks_for_thread(conn, jid)
    except Exception:  # noqa: BLE001 - never let queue cleanup break ingest
        log.debug("handled-elsewhere resolution failed (non-fatal)", exc_info=True)
        return
    if not closed and not kept_asks:
        return
    try:
        repo.record_event(conn, type="handled_elsewhere",
                          detail={"jid": jid, "count": len(closed), "kept_asks": len(kept_asks)})
    except Exception:  # noqa: BLE001
        pass
    log.info("cross-surface: owner replied elsewhere to %s — closed %d, kept %d ask(s)",
             jid, len(closed), len(kept_asks))
    # Best-effort: tell the owner once (never blocks ingest). If a decision was kept open,
    # say so explicitly — something that needs him is never silently auto-handled.
    if settings is not None:
        try:
            who = payload.get("name") or payload.get("pushName") or "this chat"
            from assistant.control.notifier import Notifier
            parts = []
            if closed:
                n = len(closed)
                parts.append(f"cleared {n} item{'s' if n != 1 else ''} from your queue")
            if kept_asks:
                topic = (kept_asks[0]["summary"] or "").split(":", 1)[-1].strip()[:80]
                k = len(kept_asks)
                parts.append(
                    f"kept {k} thing{'s' if k != 1 else ''} that still need your decision open"
                    + (f" (e.g. “{topic}”)" if topic else ""))
            if parts:
                Notifier(settings).send_text(f"You replied to {who} yourself — " + "; ".join(parts) + ".")
        except Exception:  # noqa: BLE001
            log.debug("handled-elsewhere notify failed (non-fatal)", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP receiver (relay → Python). Stdlib only; a thread per request, each with its
# own connection (WAL-safe).
# ─────────────────────────────────────────────────────────────────────────────
class _InboundHandler(BaseHTTPRequestHandler):
    # config-secrets-deploy-4: a forced-poll debounce window (seconds). Rapid repeated
    # /poll hits coalesce into one wake instead of one fetch+process cycle per request,
    # so even an authenticated caller (the console's "Fetch now" button held down, or a
    # buggy loop) can't drive continuous engine churn. Read from env with a safe default;
    # 0 disables. Class-level so it is shared across the thread-per-request handlers.
    _POLL_DEBOUNCE_S = float(os.environ.get("WA_POLL_DEBOUNCE_S", "2") or 0)
    _last_poll_at = 0.0
    _poll_lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        # Manual "fetch everything now": wake BOTH pollers immediately. This handler runs
        # IN the engine process, so it sets the same wake events the pollers wait on. Used
        # by the web console's "Fetch now" button.
        #
        # config-secrets-deploy-4 ROOT CAUSE: /poll returned BEFORE the INGEST_TOKEN check,
        # so once the owner set INGEST_TOKEN believing the receiver was gated, any local
        # process — or a drive-by web page issuing fetch('http://127.0.0.1:7999/poll',
        # {method:'POST'}) (a simple cross-origin request, no preflight) — could force
        # unbounded Gmail+WhatsApp fetch passes (ignoring poll_interval) and, whenever
        # messages were pending, real OpenRouter spend. A local-origin denial-of-wallet.
        # Fix: gate /poll behind the SAME auth as /inbound, reject cross-site requests, and
        # debounce so rapid calls coalesce into one pass.
        if self.path == "/poll":
            settings: Settings = self.server.cos_settings  # type: ignore[attr-defined]
            # 1) Same token gate as the inbound path.
            if not auth_ok(settings, self.headers.get(AUTH_HEADER), endpoint="/poll"):
                self._reply(401, {"ok": False, "error": "unauthorized"})
                return
            # 2) CSRF defense independent of the token: reject a request the browser
            #    labels cross-site. Sec-Fetch-Site is set by browsers and cannot be forged
            #    by page JS; 'cross-site'/'same-site' means a page other than our console
            #    triggered it. Non-browser callers (curl, the console's fetch with no such
            #    header, or 'same-origin'/'none') are allowed.
            sfs = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if sfs in ("cross-site", "same-site"):
                log.warning("rejected cross-site /poll (Sec-Fetch-Site=%s)", sfs)
                self._reply(403, {"ok": False, "error": "cross-site forbidden"})
                return
            # 3) Debounce: coalesce rapid forced polls into one wake.
            now = time.time()
            with _InboundHandler._poll_lock:
                if (self._POLL_DEBOUNCE_S > 0 and
                        (now - _InboundHandler._last_poll_at) < self._POLL_DEBOUNCE_S):
                    self._reply(200, {"ok": True, "woke": False, "debounced": True})
                    return
                _InboundHandler._last_poll_at = now
            try:
                from assistant import main as _engine
                _engine.trigger_poll()
                self._reply(200, {"ok": True, "woke": True})
            except Exception:  # noqa: BLE001
                self._reply(200, {"ok": False, "woke": False})
            return
        if self.path not in ("/inbound", "/outbound", "/receipt"):
            self._reply(404, {"ok": False, "error": "not found"})
            return
        # config-secrets-deploy-1: verify the relay's shared secret on every inbound
        # post. When INGEST_TOKEN is set, the relay MUST present a matching X-Cos-Token
        # or the request is rejected 401 (a co-resident process can't impersonate the
        # relay). When unset, auth_ok fails open but logs loudly in live mode.
        settings: Settings = self.server.cos_settings  # type: ignore[attr-defined]
        if not auth_ok(settings, self.headers.get(AUTH_HEADER), endpoint=self.path):
            self._reply(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._reply(400, {"ok": False, "error": "bad json"})
            return
        conn = db.open_db(self.server.cos_db_path)      # type: ignore[attr-defined]
        try:
            if self.path == "/outbound":
                # The owner's own message — context/presence/style only, never processed.
                ingest_outbound(conn, payload, settings)
                self._reply(200, {"ok": True, "recorded": True})
                return
            if self.path == "/receipt":
                # A delivery/read receipt — status-only telemetry on an existing row. It can
                # never enter the inbox/ledger or become a card (NO_AUTO_SEND holds by design).
                bumped = ingest_receipt(conn, payload)
                self._reply(200, {"ok": True, "bumped": bumped})
                return
            mid = ingest_payload(conn, settings, payload)
        except Exception:  # noqa: BLE001
            log.exception("ingest failed")
            self._reply(500, {"ok": False})
            return
        finally:
            conn.close()
        self._reply(200, {"ok": True, "queued": mid is not None})

    def _reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: N802 - silence default stderr logging
        return


# ─────────────────────────────────────────────────────────────────────────────
# The MailSource implementation
# ─────────────────────────────────────────────────────────────────────────────
class WhatsAppSource(MailSource):
    def __init__(self, conn: sqlite3.Connection, settings: Settings, llm: Any = None):
        self.conn = conn
        self.settings = settings
        self.llm = llm
        self._server: Optional[ThreadingHTTPServer] = None

    # -- lifecycle ------------------------------------------------------------
    def connect(self) -> None:
        inbox.ensure(self.conn)
        if not self.settings.wa_user_jid:
            log.warning(
                "WA_USER_JID is not set — group @mention detection is disabled; group "
                "messages will only be surfaced when they contain a WATCH_KEYWORD."
            )
        self._start_receiver()

    def _start_receiver(self) -> None:
        if self._server is not None:
            return
        server = ThreadingHTTPServer(("127.0.0.1", self.settings.whatsapp_relay_port), _InboundHandler)
        server.cos_settings = self.settings        # type: ignore[attr-defined]
        server.cos_db_path = self.settings.db_path  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True, name="wa-receiver").start()
        self._server = server
        log.info("WhatsApp receiver listening on 127.0.0.1:%s/inbound",
                 self.settings.whatsapp_relay_port)

    # -- read -----------------------------------------------------------------
    def _queue_one(self, row: sqlite3.Row, turn_id: str = "") -> str:
        """Ledger + inbox bookkeeping to hand one message to the poller. Record in the
        ledger BEFORE marking the inbox row queued — durable ownership first, so a
        crash can never drop the message. The ledger (via ledger.claim's atomic
        compare-and-set) is the AUTHORITATIVE guard against double-processing; the
        inbox status is just an advisory filter so we don't re-hand the same row every
        pass."""
        mid = row["message_id"]
        ledger.mark_seen(self.conn, mid, row["jid"] or "")
        inbox.mark_queued(self.conn, mid)
        # Fix 7: stamp turn_id so every message in a settled burst shares its anchor.
        if turn_id:
            try:
                self.conn.execute(
                    "UPDATE processed_messages SET turn_id=? WHERE message_id=?",
                    (turn_id, mid),
                )
            except Exception:  # noqa: BLE001
                pass
        return mid

    def _fetch_all_new(self) -> list[str]:
        """Legacy immediate path: every new message handed off at once (no settling)."""
        return [self._queue_one(row) for row in inbox.list_new(self.conn)]

    # ingest-whatsapp-6: optional tiny settle window for VIP 'instant' jids so a burst
    # spanning a poll boundary can fold into one card instead of fragmenting per poll.
    # DEFAULT IS 0 (legacy immediate release) ON PURPOSE: instant-VIP is a deliberate
    # "tell me now" guarantee (an investor/partner ping must not wait), and a cross-poll
    # burst can only be coalesced by DELAYING the first line — the wrong trade for a VIP.
    # Owners who prefer fold-over-latency set WHATSAPP_VIP_INSTANT_SETTLE_SECONDS>0; the
    # coalescing planner (plan_settling) honors it fully and is regression-tested.
    def _instant_settle_seconds(self) -> int:
        try:
            env = os.environ.get("WHATSAPP_VIP_INSTANT_SETTLE_SECONDS")
            if env not in (None, ""):
                return max(0, int(env))
        except (TypeError, ValueError):
            pass
        return 0  # instant-VIP preserved by default; coalescing is opt-in

    def _vip_instant_jids(self, rows: list[sqlite3.Row]) -> set[str]:
        """The grouping keys (jids) of VIP 'always-instant' 1:1 conversations among the
        pending rows — these skip the settling delay. Groups are never instant (a group
        is not a person). Best-effort: a contact-lookup miss just means 'not VIP'."""
        instant: set[str] = set()
        seen: dict[str, bool] = {}
        for r in rows:
            if r["is_group"]:
                continue
            key = r["jid"] or r["message_id"]
            if key in seen:
                continue
            try:
                c = repo.get_contact(self.conn, (r["jid"] or "").lower())
                is_vip = bool(c and c.is_vip(self.settings.vip_importance_threshold))
            except Exception:  # noqa: BLE001 - VIP lookup must never block fetching
                is_vip = False
            seen[key] = is_vip
            if is_vip:
                instant.add(key)
        return instant

    def _per_jid_settle(self, rows: list[sqlite3.Row]) -> dict:
        """Fix 3: build a per-JID settle window (seconds) from each contact's observed
        avg_response_seconds. Capped between 30s and whatsapp_settle_max_hold_seconds so
        a fast-replier gets a tighter window and a slow one stays under the hold cap.
        JIDs with no contact record fall back to the global setting (not included)."""
        result: dict = {}
        seen: set = set()
        max_hold = getattr(self.settings, "whatsapp_settle_max_hold_seconds", 300)
        for r in rows:
            if r["is_group"]:
                continue
            key = r["jid"] or r["message_id"]
            if key in seen:
                continue
            seen.add(key)
            try:
                c = repo.get_contact(self.conn, (r["jid"] or "").lower())
                avg = c.avg_response_seconds if c else None
                if avg and avg > 0:
                    result[key] = int(max(30, min(avg, max_hold)))
            except Exception:  # noqa: BLE001
                pass
        return result

    def fetch_new_message_ids(self) -> list[str]:
        """Hand the poller only CONVERSATIONS THAT HAVE SETTLED. A burst of line-by-line
        messages is held until it goes quiet, then collapsed to a single representative
        (its earlier lines folded into it) so the brain sees the whole burst and the
        owner gets one card instead of a ping per line. Fail-safe: any error in the
        settling planner falls back to the legacy immediate path — never drops a
        message."""
        if not getattr(self.settings, "whatsapp_settle_enabled", True):
            return self._fetch_all_new()
        try:
            rows = inbox.list_new(self.conn)
            if not rows:
                return []
            plan = plan_settling(
                rows,
                time.time(),
                settle=self.settings.whatsapp_settle_seconds,
                max_hold=self.settings.whatsapp_settle_max_hold_seconds,
                group_settle=self.settings.whatsapp_group_settle_seconds,
                group_max_hold=self.settings.whatsapp_group_max_hold_seconds,
                instant_jids=self._vip_instant_jids(rows),
                per_jid_settle=self._per_jid_settle(rows),
                instant_settle=self._instant_settle_seconds(),
            )
            row_by_id = {r["message_id"]: r for r in rows}
            ids: list[str] = []
            for rep, members in plan:
                for m in members:
                    inbox.mark_folded(self.conn, m, rep)
                ids.append(self._queue_one(row_by_id[rep], turn_id=rep))
            return ids
        except Exception:  # noqa: BLE001 - settling must never block ingestion
            log.warning("WhatsApp settling failed; falling back to immediate fetch",
                        exc_info=True)
            return self._fetch_all_new()

    def _materialize(self, row: sqlite3.Row) -> Message:
        """Transcribe a voice note / describe an image once (lazily, cached) and normalize
        one inbox row into a Message. Shared by single- and multi-message threads.

        GAP 8: audio → transcript (falls back to a 'could not transcribe' placeholder),
        image → one-sentence description (falls back to '[image]'). The media_b64 is
        cleared by inbox.set_body once processed so it isn't reprocessed or retained."""
        media_type = (row["media_type"] or "").lower()
        opaque = False  # Fix 5: True when media arrived but couldn't be processed
        if media_type == "audio" and row["media_b64"] and self.llm is not None:
            transcript = None
            try:
                transcript = self.llm.transcribe_audio(
                    row["media_b64"], row["audio_format"] or "ogg"
                )
            except LLMError as exc:
                log.warning("voice transcription failed (%s); using placeholder", exc)
                opaque = True
            body = _body_for(_row_to_payload(row), transcript)
            inbox.set_body(self.conn, row["message_id"], body)
            row = inbox.get(self.conn, row["message_id"])
        elif media_type == "image" and row["media_b64"] and self.llm is not None:
            description = None
            try:
                description = self.llm.describe_image(row["media_b64"])
            except Exception as exc:  # noqa: BLE001 - vision best-effort
                log.warning("image description failed (%s); using placeholder", exc)
                opaque = True
            body = _body_for(_row_to_payload(row), None, description=description)
            inbox.set_body(self.conn, row["message_id"], body)
            row = inbox.get(self.conn, row["message_id"])
        # Fix 5: mark opaque in inbox + register an attachment node in the graph so
        # un-processable media is visible rather than silently becoming a placeholder.
        if opaque:
            try:
                self.conn.execute(
                    "UPDATE whatsapp_inbox SET opaque=1 WHERE message_id=?",
                    (row["message_id"],),
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                from assistant.memory import graph
                node_id = f"att_{row['message_id']}"
                jid = row["sender_jid"] or row["jid"] or ""
                graph.upsert_node(self.conn, node_id, type="attachment",
                                  name=f"{media_type} from {jid}",
                                  attrs={"media_type": media_type, "jid": jid,
                                         "opaque": True, "message_id": row["message_id"]})
            except Exception:  # noqa: BLE001 - graph is additive
                pass
        stamp_rule_flags(self.conn, row["sender_jid"] or row["jid"] or "",
                         row["push_name"] or "", self.settings)
        return normalize(_row_to_payload(row), self.settings, transcript=None)

    def get_thread(self, message_id: str) -> Thread:
        row = inbox.get(self.conn, message_id)
        if row is None:
            return Thread(id=message_id, channel=Channel.WHATSAPP, messages=[])

        # Reassemble the full settled burst: every line folded into this representative,
        # plus the representative itself, in the sender's own order. (No folded members
        # → a single-message thread, exactly as before.)
        rows: list[sqlite3.Row] = []
        try:
            rows = inbox.folded_members(self.conn, message_id)
        except Exception:  # noqa: BLE001 - a missing burst never blocks the message
            log.debug("folded_members lookup failed (non-fatal)", exc_info=True)
        rows.append(row)
        # ingest-whatsapp-2: order the reassembled burst by receive clock first (clamped ts
        # as tiebreaker) so a spoofed sender ts can't reorder what the drafter/owner sees.
        rows.sort(key=_order_key)

        messages = [self._materialize(r) for r in rows]
        subject = messages[-1].subject if messages else ""
        return Thread(id=row["jid"] or (messages[-1].thread_id if messages else message_id),
                      channel=Channel.WHATSAPP, subject=subject, messages=messages)

    # -- act (Tier 0/1: mark read, not archive) ------------------------------
    def archive(self, message_id: str) -> dict:
        jid = self._jid_for(message_id)
        self._relay("/read", {"jid": jid})
        return {"op": "wa_read", "message_id": message_id, "jid": jid}

    def apply_label(self, message_id: str, label: str) -> dict:
        # WhatsApp has no labels — treat as "mark read".
        jid = self._jid_for(message_id)
        self._relay("/read", {"jid": jid})
        return {"op": "wa_read", "message_id": message_id, "jid": jid, "label": label}

    def undo(self, undo_data: dict) -> None:
        # Marking-read can't be un-done on WhatsApp; nothing to reverse.
        log.info("undo is a no-op for WhatsApp action: %s", undo_data.get("op"))

    # -- send -----------------------------------------------------------------
    # failure-recovery-5 ROOT CAUSE: send_reply returned the terminal "wa_sent" id on a
    # BARE HTTP-200 from the relay, NEVER reading the body. During a relay reconnect,
    # `await sock.sendMessage(...)` resolves on local encrypt/enqueue WITHOUT a server ack,
    # so the relay returns 200 {"ok":true} even though WhatsApp never delivered the message.
    # execute_send then recorded terminal SENT — and because the stuck-send reaper only
    # watches SENDING and undelivered_pending only watches PENDING, the lost reply was never
    # re-queued and no alert fired: a silent communication failure on the channel most tied
    # to VIP relationships. Fix (the email SEND_AMBIGUOUS spirit — do not claim delivered
    # without evidence): parse the relay body, PREFER a real delivery id when the relay
    # provides one, and otherwise SURFACE the uncertainty (record an event, optionally route
    # to SEND_AMBIGUOUS via a raised error) instead of silently asserting delivery.
    #
    # Strict mode (WHATSAPP_REQUIRE_DELIVERY_CONFIRMATION=1) raises on an unconfirmed send so
    # execute_send's existing handler marks it SEND_AMBIGUOUS (maybe-delivered, surfaced to
    # the owner, NEVER auto-resent). Default is OFF so we do not flip every send on the
    # current bare-ok relay to ambiguous (which would be noisy) — but even with it off we
    # ALWAYS record a wa_send_unconfirmed event so the gap is observable, and we ALWAYS use a
    # real relay-supplied delivery id when present. Set the flag once the relay is upgraded
    # to return delivery confirmation.
    _CONFIRM_FIELDS = ("message_id", "messageId", "id", "key")

    def _delivery_id_from(self, body: Optional[dict]) -> str:
        """Extract a real delivery id from the relay's /send JSON, or '' if none present.
        A non-empty id is positive evidence the message was accepted with a server key."""
        if not isinstance(body, dict):
            return ""
        for f in self._CONFIRM_FIELDS:
            v = body.get(f)
            if isinstance(v, dict):  # e.g. {"key": {"id": "..."}}
                v = v.get("id") or v.get("Id") or ""
            if v:
                return str(v)
        return ""

    @staticmethod
    def _require_send_confirmation() -> bool:
        return (os.environ.get("WHATSAPP_REQUIRE_DELIVERY_CONFIRMATION", "") or "").strip() \
            in ("1", "true", "True", "yes", "on")

    def send_reply(self, *, thread_id: str, to: list[str], cc: list[str],
                   subject: str, body: str, in_reply_to_gmail_id: str) -> str:
        # thread_id is the chat JID; the rest of the email-shaped params don't apply.
        status, resp_body = self._relay_with_body("/send", {"jid": thread_id, "text": body})

        delivery_id = self._delivery_id_from(resp_body)
        if delivery_id:
            # Record the reply as conversation state (is_agent=1) so the lifecycle sweep sees
            # the chat as answered (no false "you haven't replied") and a later messages.update
            # receipt for wa_<id> has a row to attach delivered/read to. is_agent keeps it out
            # of presence/style. Best-effort — never break the send return path.
            try:
                wa_messages.record(
                    self.conn,
                    {"message_id": f"wa_{delivery_id}", "jid": thread_id, "body": body,
                     "is_group": (thread_id or "").endswith("@g.us")},
                    from_me=True, is_agent=True,
                )
            except Exception:  # noqa: BLE001
                log.debug("wa_messages agent-send record failed (non-fatal)", exc_info=True)
            # IMPORTANT: delivery_id is a CLIENT-minted Baileys key (resolved on local
            # encrypt/enqueue, BEFORE any server ACK) — it lets receipts attach to the row, but
            # it is NOT proof of delivery. So we STILL record the unconfirmed-send event for
            # observability (failure-recovery-5), and the is_agent stuck-send sweep + the eventual
            # messages.update receipt (status>=SERVER_ACK) are what actually confirm delivery. In
            # strict mode, surface ambiguity rather than asserting delivery on a mere key.
            try:
                repo.record_event(
                    self.conn, type="wa_send_unconfirmed", message_id=in_reply_to_gmail_id or "",
                    contact_email=(thread_id or ""),
                    detail={"jid": thread_id, "status": status, "delivery_id": delivery_id,
                            "client_key_only": True, "strict": self._require_send_confirmation()},
                )
            except Exception:  # noqa: BLE001 - observability must never break the send path
                log.debug("wa_send_unconfirmed (client-key) event failed (non-fatal)", exc_info=True)
            if self._require_send_confirmation():
                raise WhatsAppSendUnconfirmed(
                    f"WhatsApp send to {thread_id} returned only a client key (no server "
                    f"delivery ack) — marking ambiguous rather than asserting delivery")
            return f"wa_{delivery_id}"

        # No delivery evidence in the body (current relay returns a bare {"ok":true}). The
        # send is UNCONFIRMED. Record it so an undelivered reply is never silently lost.
        try:
            repo.record_event(
                self.conn, type="wa_send_unconfirmed", message_id=in_reply_to_gmail_id or "",
                contact_email=(thread_id or ""),
                detail={"jid": thread_id, "status": status,
                        "body": (resp_body if isinstance(resp_body, dict) else {}),
                        "strict": self._require_send_confirmation()},
            )
        except Exception:  # noqa: BLE001 - observability must never break the send path
            log.debug("wa_send_unconfirmed event record failed (non-fatal)", exc_info=True)
        log.warning("WhatsApp send to %s returned no delivery confirmation (status=%s) — "
                    "treating as UNCONFIRMED", thread_id, status)

        if self._require_send_confirmation():
            # Strict: surface uncertainty. Raising routes execute_send to SEND_AMBIGUOUS
            # (maybe-delivered → never auto-resent, owner is alerted). This is the email
            # AMBIGUOUS spirit: do not claim delivered without evidence.
            raise WhatsAppSendUnconfirmed(
                f"WhatsApp send to {thread_id} unconfirmed (relay HTTP {status}, no "
                f"delivery receipt) — marking ambiguous rather than asserting delivery")

        # Lenient default: preserve existing terminal-SENT behavior on the current relay,
        # but the recorded event above keeps the gap visible to the owner/dashboard.
        return "wa_sent"

    def _relay_with_body(self, path: str, payload: dict) -> tuple[int, Optional[dict]]:
        """Like _relay but also returns the parsed JSON response body (or None). Used by
        send_reply to read delivery confirmation. A non-2xx still raises (urlopen raises),
        so execute_send routes a hard failure to SEND_FAILED/AMBIGUOUS exactly as before."""
        url = f"http://127.0.0.1:{self.settings.whatsapp_send_port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=relay_auth_headers(self.settings), method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # raises on non-2xx
            raw = b""
            try:
                raw = resp.read()
            except Exception:  # noqa: BLE001
                raw = b""
            parsed: Optional[dict] = None
            if raw:
                try:
                    obj = json.loads(raw.decode("utf-8"))
                    parsed = obj if isinstance(obj, dict) else None
                except Exception:  # noqa: BLE001 - a non-JSON body is just 'no evidence'
                    parsed = None
            return resp.status, parsed

    def send_media(self, jid: str, media_type: str, url: str, caption: str = '', filename: str = '') -> bool:
        """Send a media message via the WhatsApp relay. media_type: image|video|audio|document"""
        if self.settings.dry_run:
            print(f"[dry_run] would send {media_type} to {jid}")
            return True
        try:
            import urllib.request, urllib.error, json as _json
            payload = json.dumps({"jid": jid, "media_type": media_type, "url": url,
                                  "caption": caption, "filename": filename}).encode()
            relay_send_url = f"http://127.0.0.1:{self.settings.whatsapp_send_port}/send_media"
            # config-secrets-deploy-1: attach the shared secret so the relay can reject
            # unauthenticated /send_media (else a local process could send media as owner).
            req = urllib.request.Request(relay_send_url, data=payload,
                                         headers=relay_auth_headers(self.settings), method='POST')
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = _json.loads(resp.read())
                return result.get('success', False)
        except Exception as e:
            print(f"[whatsapp] send_media error: {e}")
            return False

    # -- relay client ---------------------------------------------------------
    def _jid_for(self, message_id: str) -> str:
        row = inbox.get(self.conn, message_id)
        return (row["jid"] if row else "") or ""

    def _in_read_quiet_hours(self) -> bool:
        """Fix 2: True if NOW (local tz) is inside the read-receipt quiet window. Best-
        effort — any error returns False (fail toward normal behavior)."""
        if not getattr(self.settings, "read_receipt_quiet_hours_enabled", True):
            return False
        try:
            from datetime import datetime
            try:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo(self.settings.timezone))
            except Exception:  # noqa: BLE001 - bad/missing tz → local wall clock
                now = datetime.now()
            return _hour_in_quiet_window(
                now.hour,
                int(getattr(self.settings, "read_receipt_quiet_start_hour", 22)),
                int(getattr(self.settings, "read_receipt_quiet_end_hour", 8)),
            )
        except Exception:  # noqa: BLE001
            return False

    def _relay(self, path: str, payload: dict) -> int:
        # Fix 2: suppress read receipts (blue ticks) during quiet hours. ONLY /read is
        # gated — outbound /send is never affected (sends still require human approval).
        if path == "/read" and self._in_read_quiet_hours():
            log.info("read receipt suppressed (quiet hours): %s", payload.get("jid"))
            return 0
        url = f"http://127.0.0.1:{self.settings.whatsapp_send_port}{path}"
        data = json.dumps(payload).encode("utf-8")
        # config-secrets-deploy-1: attach the shared secret so the relay can reject
        # unauthenticated /send and /read. relay_auth_headers includes Content-Type.
        req = urllib.request.Request(
            url, data=data, headers=relay_auth_headers(self.settings), method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # raises on failure
            return resp.status
