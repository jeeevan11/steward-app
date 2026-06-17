"""FastAPI backend for the local web console. Binds 127.0.0.1 ONLY.

Read endpoints use storage/read_queries (read-only). Write endpoints use
assistant.web.service, which calls the exact guarded functions the Telegram bot
uses. Dry-run is inherited from those functions — the console is never a way
around it. See docs/WEB.md for the endpoint → seam map.

Run: `python -m assistant.web.api`  (or `python run_web.py`).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time as _time
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from assistant.config import load_settings
from assistant.logging_setup import get_logger
from assistant.storage import db, decision_log
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo
from assistant.web import service

try:
    from assistant.web.miniapp_auth import validate_init_data, create_session_token, MiniAppAuth
    _miniapp_auth_available = True
except ImportError:
    _miniapp_auth_available = False

try:
    from assistant.control import state_engine as _state_engine
except ImportError:
    _state_engine = None

try:
    from assistant.memory import opportunities as _opportunities
except ImportError:
    _opportunities = None

try:
    from assistant.memory import projects as _projects
except ImportError:
    _projects = None

try:
    from assistant.memory import calendar_actions as _calendar
except ImportError:
    _calendar = None

try:
    from assistant.action import compose as _compose
except ImportError:
    _compose = None

log = get_logger("web.api")

app = FastAPI(title="Steward — Console", docs_url="/api/docs", openapi_url="/api/openapi.json")

# web-security-5: a generic error body for clients; the real exception is logged server-side
# only. Raw str(e) leaked file paths (./data/assistant.db), SQL fragments, and ISO/JSON parse
# positions to the caller. This is loopback-only today, but a generic body future-proofs the
# console against ever being bound beyond localhost or made multi-tenant.
_GENERIC_ERROR_DETAIL = "internal error"


def _safe_error(exc: Exception, where: str) -> str:
    """Log the full exception server-side and return a constant, leak-free message."""
    log.exception("unhandled error in %s", where)
    return _GENERIC_ERROR_DETAIL

# Localhost dev only — the Vite dev server proxies /api, but allow direct too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _generic_exception_handler(request, exc):  # type: ignore[no-untyped-def]
    """web-security-5: any unhandled exception is logged in full server-side and returned to
    the client as a generic 500 — never the raw str(e) (which leaked db paths / SQL / parse
    positions). HTTPExceptions are handled by FastAPI's own handler and never reach here, so
    intentional 4xx detail messages are preserved."""
    from starlette.responses import JSONResponse
    log.exception("unhandled error at %s", getattr(request, "url", ""))
    return JSONResponse({"detail": _GENERIC_ERROR_DETAIL}, status_code=500)

# Lazily-built singletons (only constructed when actually needed).
_settings = load_settings()


_CSRF_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

# Sec-Fetch-Site values a browser attaches that mean "this request did NOT come from a
# cross-site page". These are forbidden headers (JavaScript cannot set or forge them),
# so a malicious page cannot spoof them. Anything outside this set ("cross-site",
# "cross-origin") is a browser request from another site and must be blocked.
_CSRF_SAFE_FETCH_SITES = {"same-origin", "same-site", "none"}


def _origin_host(value: str):  # type: ignore[no-untyped-def]
    """Best-effort hostname of an Origin/Referer header value (None if unparseable)."""
    from urllib.parse import urlsplit
    try:
        return urlsplit(value).hostname
    except ValueError:
        return None


@app.middleware("http")
async def _console_auth(request, call_next):
    """Write protection for mutating requests (CSRF + opt-in token).

    Root cause (web-security-1): the previous guard only acted when an Origin header was
    PRESENT (``if origin:``). A state-changing browser request that arrives with NO Origin
    — e.g. a cross-site form auto-submit, a navigation-style POST, or a stripped-Origin
    request — fell straight through to /api/actions/{id}/approve, which performs a REAL
    send in live mode (that endpoint takes no body, so CORS never preflights it). Missing
    Origin was treated as trusted; it must be treated as untrusted for mutations.

    Hardened rule for mutations (additive — only ever blocks MORE, never less):
      a) Origin present and host not localhost  → reject (unchanged).
      b) Referer present and host not localhost  → reject (NEW; a cross-site browser POST
         that omits Origin still carries a foreign Referer).
      c) Sec-Fetch-Site present and not in the safe set → reject (NEW; this is the header
         a browser sends even when Origin is absent — it is how we catch the no-Origin
         cross-site POST without breaking non-browser local tools, which send none of
         these headers, nor the same-origin SPA, which sends Sec-Fetch-Site: same-origin).
      d) Opt-in token (unchanged): when CONSOLE_TOKEN is set, X-Cos-Token must match.

    Read-only endpoints (GET/HEAD/OPTIONS) are never touched. Same-origin SPA requests
    (127.0.0.1/localhost, any port) and non-browser local tools both pass."""
    from starlette.responses import JSONResponse
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and _origin_host(origin) not in _CSRF_ALLOWED_HOSTS:
            log.warning("CSRF block: cross-origin mutation %s (origin=%s)", request.url.path, origin)
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)

        # A cross-site browser POST that omits Origin still carries a foreign Referer.
        referer = request.headers.get("referer")
        if referer and _origin_host(referer) not in _CSRF_ALLOWED_HOSTS:
            log.warning("CSRF block: cross-site referer mutation %s (referer=%s)",
                        request.url.path, referer)
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)

        # Fetch-metadata: the one signal present even when Origin/Referer are absent.
        # This closes the exact hole in the finding — a no-Origin cross-site browser POST
        # arrives with Sec-Fetch-Site: cross-site and is now rejected.
        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
        if sec_fetch_site and sec_fetch_site not in _CSRF_SAFE_FETCH_SITES:
            log.warning("CSRF block: cross-site fetch mutation %s (sec-fetch-site=%s)",
                        request.url.path, sec_fetch_site)
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)

        token = getattr(_settings, "console_token", "")
        if token and request.headers.get("X-Cos-Token") != token:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)
_mail: Any = None
_notifier: Any = None
_llm: Any = None


# ── dependencies (all overridable in tests) ─────────────────────────────────
def get_settings():  # type: ignore[no-untyped-def]
    return _settings


def get_conn(settings=Depends(get_settings)):  # type: ignore[no-untyped-def]
    conn = db.connect(settings.db_path)
    db.init_db(conn)            # idempotent: ensures all core tables exist
    decision_log.ensure(conn)   # ensures the decision_log table exists for reads
    try:
        yield conn
    finally:
        conn.close()


def get_mail(settings=Depends(get_settings)):  # type: ignore[no-untyped-def]
    """A connected GmailSource — only needed for LIVE sends. In dry-run, returns
    None (execute_send never touches Gmail in dry-run)."""
    if settings.dry_run:
        return None
    global _mail
    if _mail is None:
        from assistant.ingest.gmail_source import GmailSource
        from assistant.ingest.router import MailRouter

        conn = db.open_db(settings.db_path)
        gmail = GmailSource(conn, settings)
        gmail.connect()
        sources: dict = {"gmail": gmail}
        if settings.whatsapp_enabled:
            from assistant.ingest.whatsapp_source import WhatsAppSource
            sources["whatsapp"] = WhatsAppSource(conn, settings)  # send/get_thread only
        _mail = MailRouter(sources)
    return _mail


def get_notifier(settings=Depends(get_settings)):  # type: ignore[no-untyped-def]
    global _notifier
    if _notifier is None:
        from assistant.control.notifier import Notifier
        _notifier = Notifier(settings)
    return _notifier


def get_llm(settings=Depends(get_settings)):  # type: ignore[no-untyped-def]
    global _llm
    if _llm is None:
        from assistant.llm.client import LLMClient
        from assistant.storage import metrics
        _llm = LLMClient(settings, metrics_sink=metrics.make_sink(settings.db_path))
    return _llm


# ── in-memory read cache (P6) ───────────────────────────────────────────────
# Small TTL cache so repeated read hits are O(1); any write clears it. Pipeline
# status is intentionally never cached (it's the real-time view).
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):  # type: ignore[no-untyped-def]
    now = _time.time()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    _CACHE[key] = (now + ttl, val)
    return val


def _invalidate() -> None:
    _CACHE.clear()


# ── request bodies ──────────────────────────────────────────────────────────
class EditBody(BaseModel):
    text: str


class ContactUpdateBody(BaseModel):
    flags: Optional[list[str]] = None
    importance: Optional[int] = None


class ContactSaveBody(BaseModel):
    identifier: str = ""          # WhatsApp @lid/jid or email of the sender to save
    name: str = ""                # the name to assign
    phone: str = ""               # optional: bridges an @lid to a phone number
    email: str = ""               # optional: links an email to the same person


class OwnerAboutBody(BaseModel):
    about: str = ""               # the owner's free-text self-description


class SnoozeBody(BaseModel):
    days: int = 2


class TestPipelineBody(BaseModel):
    sender: str = "someone@example.com"
    subject: str = ""
    email_text: str = ""


class FeedbackBody(BaseModel):
    correct_tier: Optional[int] = None
    thumbs: str = ""


class CommandBody(BaseModel):
    text: str = ""


class EvalBody(BaseModel):
    sender: str = "someone@example.com"
    subject: str = ""
    body: str = ""
    expected_tier: Optional[int] = None


# ── read endpoints ──────────────────────────────────────────────────────────
@app.get("/api/status")
def api_status(conn: sqlite3.Connection = Depends(get_conn), settings=Depends(get_settings)):
    return rq.get_status(conn, settings)


@app.post("/api/pause")
def api_pause(conn: sqlite3.Connection = Depends(get_conn)):
    """Pause the agent — it keeps running but acts on nothing until resumed. Works
    regardless of how the engine was launched (this is the real on/off)."""
    repo.set_paused(conn, True)
    return {"ok": True, "paused": True}


@app.post("/api/resume")
def api_resume(conn: sqlite3.Connection = Depends(get_conn)):
    repo.set_paused(conn, False)
    return {"ok": True, "paused": False}


@app.get("/api/stats")
def api_stats(conn: sqlite3.Connection = Depends(get_conn)):
    return rq.get_stats(conn)


@app.get("/api/pipeline")
def api_pipeline(conn: sqlite3.Connection = Depends(get_conn)):
    return rq.get_pipeline(conn)


@app.get("/api/notifications")
def api_notifications(conn: sqlite3.Connection = Depends(get_conn)):
    return rq.get_notifications(conn)


@app.post("/api/notifications/clear")
def api_notifications_clear(conn: sqlite3.Connection = Depends(get_conn)):
    # Non-destructive "clear for now": stamps a cursor, deletes nothing.
    return rq.clear_notifications(conn)


@app.post("/api/command")
def api_command(body: CommandBody, conn: sqlite3.Connection = Depends(get_conn),
                settings=Depends(get_settings), llm=Depends(get_llm)):
    """Talk to Steward in plain English to add a standing rule, set a contact's importance,
    pause/resume, etc. Routes through the SAME closed-set NL handler as Telegram. It never
    sends a message and never raises (worst case: 'I didn't understand that'). 'undo' is a
    no-op here since the web has no mail handle."""
    from assistant.control import commands
    reply = commands.apply_command(conn, settings, llm, None, None, (body.text or "").strip())
    return {"reply": reply}


@app.post("/api/fetch-now")
def api_fetch_now(settings=Depends(get_settings)):
    """Force an immediate fetch + process pass on BOTH channels (the 'Fetch everything'
    button). Pokes the engine's localhost control endpoint to wake its Gmail + WhatsApp
    pollers right now instead of waiting for the poll interval. Never sends anything —
    it only triggers fetch/process; drafts still require your approval."""
    import urllib.request

    port = getattr(settings, "whatsapp_relay_port", 7999)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/poll", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            return {"ok": r.status == 200, "woke": r.status == 200}
    except Exception:  # noqa: BLE001 - engine receiver may be down (e.g. WhatsApp off)
        return {"ok": False, "woke": False,
                "reason": "engine not reachable — it will still fetch on its next cycle"}


@app.post("/api/sync-contacts")
def api_sync_contacts(conn: sqlite3.Connection = Depends(get_conn)):
    """Sync WhatsApp contacts (relay cache + inbox push_names) into Steward."""
    from assistant.memory import phone_contacts
    result = phone_contacts.sync(conn)
    return {"ok": True, **result}


@app.post("/api/resolve-lid")
def api_resolve_lid(body: dict, conn: sqlite3.Connection = Depends(get_conn)):
    """Ask the relay to resolve a LID to a real phone number via WhatsApp's onWhatsApp() API."""
    import urllib.request, json as _json
    lid = (body.get("lid") or "").strip()
    if not lid:
        return {"ok": False, "error": "lid required"}
    try:
        payload = _json.dumps({"lid": lid}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:7998/resolve-lid",
            data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = _json.loads(r.read().decode())
        # If resolved, update the contacts DB
        if result.get("phone_jid") and result.get("phone_number"):
            phone_jid = result["phone_jid"]
            phone = result["phone_number"]
            # Copy name from LID entry to phone JID entry
            lid_contact = conn.execute("SELECT name, relationship, importance FROM contacts WHERE email=?", (lid,)).fetchone()
            if lid_contact and lid_contact[0]:
                conn.execute(
                    """INSERT INTO contacts (email, name, relationship, importance)
                       VALUES (?,?,?,?)
                       ON CONFLICT(email) DO UPDATE SET name=excluded.name,
                         relationship=excluded.relationship, importance=MAX(importance,excluded.importance)""",
                    (phone_jid, lid_contact[0], lid_contact[1] or "wa_contact", max(int(lid_contact[2] or 0), 5))
                )
                conn.commit()
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": _safe_error(e, "phone_contacts_sync")}


@app.get("/api/queue")
def api_queue(limit: int = 50, conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": rq.get_queue(conn, limit=limit)}


@app.get("/api/queue-summary")
def api_queue_summary(conn: sqlite3.Connection = Depends(get_conn)):
    return rq.get_queue_summary(conn)


@app.get("/api/email/{message_id}")
def api_email(message_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = rq.get_email(conn, message_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="not found")
    return detail


@app.get("/api/explanation/{message_id}")
def api_explanation(message_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 2 — the full structured 'why' for a decision (guardrails, memory/presence/
    feedback signals, model verdict, the ordered floor chain, and a human summary)."""
    from assistant.storage import explanations
    expl = explanations.get(conn, message_id)
    if expl is None:
        raise HTTPException(status_code=404, detail="no explanation recorded")
    return expl


@app.get("/api/replay/{message_id}")
def api_replay(message_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 3 — reconstruct the full reasoning path for a decision (prompt versions,
    models/params, context supplied, raw step outputs, tiering, explanation)."""
    from assistant.storage import replay
    rec = replay.reconstruct(conn, message_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="no replay record")
    return rec


@app.get("/api/calibration")
def api_calibration(conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 5 — confidence reliability curve (predicted vs actual)."""
    from assistant.storage import calibration
    return {"bins": calibration.get_curve(conn)}


@app.get("/api/trust")
def api_trust(period: str = "daily", conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 10/13 — trust & value metrics (processed/suppressed/escalated/auto-handled,
    approval + draft-acceptance rates, commitments, decisions avoided, time saved)."""
    from assistant.storage import trust_metrics
    return trust_metrics.period(conn, period)


@app.get("/api/graph/{node_id}")
def api_graph(node_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 7 — node-centric relationship view."""
    from assistant.memory import graph
    return {
        "node_id": node_id,
        "connected": graph.connected_to(conn, node_id),
        "waiting_on_me": graph.waiting_on_me(conn, node_id),
    }


@app.get("/api/decisions")
def api_decisions(conn: sqlite3.Connection = Depends(get_conn)):
    """Steward redesign: the decisions awaiting the owner, in Decision shape
    (title, one sentence, tier, draft, context) — significance, not message rows."""
    from assistant.storage import decision_log, explanations
    items = []
    for p in repo.open_pending(conn):          # tier 2/3 awaiting a human
        mid = p["message_id"]
        d = decision_log.get(conn, mid)
        expl = explanations.get(conn, mid) or {}
        subject = ((d["subject"] if d else "") or "").strip()
        sender_email = ((d["sender_email"] if d else "") or "")
        # Live name: a saved person's name wins over the frozen push-name captured at ingest,
        # so saving a contact updates this card on the next poll (no re-ingest).
        _live_sender, _ = rq._live_name(conn, sender_email or (mid if d is None else ""))
        sender = _live_sender or (((d["sender_name"] or d["sender_email"]) if d else "") or "")
        title = subject if subject and subject != "(no subject)" else (sender or "A decision")
        sentence = (expl.get("summary") or (p["summary"] if "summary" in p.keys() else "") or "").strip()
        quote = (d["snippet"] if d else "") or ""
        category = rq.CATEGORY_LABEL.get(d["category"], "Other") if d else "Other"
        # ux-trust-4: a reminder row is keyed by a thread/JID, so decision_log.get(mid) is
        # None and the card used to render empty sender + "Other" + a wrong "Email" channel
        # (the JID does not start with "wa_"). Derive the channel from the JID *shape* and
        # pull the stamped reminder provenance (sender name + a real quote) so the tier-3
        # card is verifiable instead of a blank maximally-urgent nag.
        channel = repo.channel_for_identifier(mid)
        if d is None and (p["kind"] or "") == "reminder":
            meta = repo.get_reminder_meta(conn, p["idempotency_key"]) \
                if "idempotency_key" in p.keys() else None
            if meta is not None:
                # Keep the live saved name if we resolved one; else use the reminder's stamp.
                if not _live_sender and (meta["sender_name"] or "").strip():
                    sender = meta["sender_name"].strip()
                if (meta["quote"] or "").strip():
                    quote = meta["quote"].strip()
                if (meta["channel"] or "").strip():
                    channel = meta["channel"].strip()
        items.append({
            "id": p["id"], "message_id": mid, "tier": int(p["tier"] or 2),
            "title": title, "sentence": sentence, "kind": p["kind"],
            "draft": (p["draft_text"] if "draft_text" in p.keys() else "") or "",
            "context": ((d["snippet"] if d else "") or sentence or quote),
            "quote": quote,
            "category": category,
            "channel": channel,
            "sender": sender,
            # Raw identifier + recognition, so the card can offer "Save contact" for an
            # unknown sender. For a reminder with no decision_log row, the JID-keyed
            # message_id IS the identifier.
            "sender_identifier": sender_email or (mid if d is None else ""),
            "is_saved": rq._contact_info(conn, sender_email or (mid if d is None else ""))["is_saved"],
        })
    return {"items": items}


@app.post("/api/decisions/clear")
async def api_decisions_clear(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Clear all — skip the awaiting decisions, SCOPED and RECOVERABLE (ux-trust-6).

    Root cause: the old 'Clear all' was a two-tap, tier-blind, undo-less bulk drop. It
    SKIPPED every open decision — including a tier-3 'confirm the wire today' — terminally,
    with no restore path. That is exactly the silent expensive drop the product exists to
    prevent.

    Hardened, additive behavior:
      * By default it skips ONLY non-urgent (tier < 3) decisions; tier-3 'needs you soon'
        items are KEPT unless the caller explicitly opts in with {"include_urgent": true}
        (the UI requires a distinct second confirmation that names the urgent count).
      * Every skipped row is journaled under a batch_id so the whole batch can be UNDONE
        within a grace window via /api/decisions/clear/undo.
    Returns the batch_id, how many were cleared, and how many urgent items were kept."""
    include_urgent = False
    try:
        body = await request.json()
        include_urgent = bool(body.get("include_urgent", False))
    except Exception:  # noqa: BLE001 - empty/invalid body → safe default (keep urgent)
        include_urgent = False

    import uuid as _uuid
    batch_id = _uuid.uuid4().hex
    cleared = 0
    kept_urgent = 0
    for p in list(repo.open_pending(conn)):
        tier = int(p["tier"] or 2)
        if tier >= 3 and not include_urgent:
            kept_urgent += 1
            continue
        try:
            prev_status = p["status"] if "status" in p.keys() else "PENDING"
            if service.skip(conn, p["id"]).get("ok"):
                # Journal BEFORE counting so an undo can always find what we skipped.
                repo.record_bulk_skip(conn, batch_id, int(p["id"]), prev_status or "PENDING")
                cleared += 1
        except Exception:  # noqa: BLE001
            pass
    return {"cleared": cleared, "kept_urgent": kept_urgent, "batch_id": batch_id}


@app.post("/api/decisions/clear/undo")
async def api_decisions_clear_undo(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Undo a recent 'Clear all' (ux-trust-6): restore every decision skipped under the given
    batch_id (within the grace window) back to PENDING so it re-surfaces. Recorded as a
    reversible audit event. Only rows still SKIPPED are revived — never one re-handled since."""
    batch_id = ""
    try:
        body = await request.json()
        batch_id = str(body.get("batch_id", "") or "")
    except Exception:  # noqa: BLE001
        batch_id = ""
    if not batch_id:
        return {"restored": 0, "ok": False, "reason": "no batch_id"}
    restored = repo.restore_bulk_skip(conn, batch_id)
    return {"restored": restored, "ok": True}


@app.get("/api/health")
def api_health(conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 12 — component health (heartbeat, relay, db, queue, failures)."""
    from assistant import diagnostics
    return diagnostics.health_check(conn, _settings)


@app.get("/api/diagnostics")
def api_diagnostics(conn: sqlite3.Connection = Depends(get_conn)):
    """Phase 12 — full share-safe diagnostics bundle (secrets redacted)."""
    from assistant import diagnostics
    return diagnostics.collect(conn, _settings)


@app.get("/api/brief")
def api_brief(conn: sqlite3.Connection = Depends(get_conn)):
    """GAP 4 — the structured morning brief: commitments due soon, open situations
    awaiting the owner, relationship attention, risks, and the single top priority.
    Cached in KV for 6h (regenerated when stale)."""
    from assistant.control import briefs
    return briefs.build_or_get_brief(conn)


@app.get("/api/contacts")
def api_contacts(conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": rq.list_contacts(conn)}


@app.get("/api/rules")
def api_rules(conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": rq.list_rules(conn)}


@app.get("/api/learning")
def api_learning(conn: sqlite3.Connection = Depends(get_conn)):
    """GAP 5 — the learning loop's health: event counts by type and per-day counts over
    the last 7 days. Sourced from learning_events (every human signal carries a type)."""
    return rq.learning_summary(conn)


@app.get("/api/audit")
def api_audit(conn: sqlite3.Connection = Depends(get_conn)):
    since = repo.now_epoch() - 86400  # last 24h
    return {"items": rq.list_audit(conn, since)}


@app.get("/api/wastatus")
def api_wastatus(settings=Depends(get_settings)):
    return rq.get_wastatus(settings)


# ── write endpoints (→ guarded seams in service.py) ─────────────────────────
@app.post("/api/actions/{action_id}/approve")
def api_approve(
    action_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    settings=Depends(get_settings),
    mail=Depends(get_mail),
    notifier=Depends(get_notifier),
    llm=Depends(get_llm),
):
    return service.approve(conn, mail, settings, notifier, action_id, llm=llm)


@app.post("/api/actions/{action_id}/edit")
def api_edit(action_id: int, body: EditBody, conn: sqlite3.Connection = Depends(get_conn)):
    return service.edit(conn, action_id, body.text)


@app.post("/api/actions/{action_id}/skip")
def api_skip(action_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.skip(conn, action_id)


@app.post("/api/email/{message_id}/feedback")
def api_feedback(message_id: str, body: FeedbackBody, conn: sqlite3.Connection = Depends(get_conn)):
    return service.feedback(conn, message_id, body.correct_tier, body.thumbs)


# ── eval: run the REAL brain on a synthetic email (in-memory, dry-run forced) ─
@app.post("/api/eval")
def api_eval(body: EvalBody, settings=Depends(get_settings), llm=Depends(get_llm)):
    from assistant.brain import classifier
    from assistant.brain.tiers import TierConfig, decide
    from assistant.memory import retrieval
    from assistant.models import Message, Thread

    dry = dataclasses.replace(settings, mode="dry_run")
    conn = db.open_db(":memory:")
    try:
        msg = Message(
            id="eval-1", thread_id="eval-t", sender_email=body.sender, sender_name=body.sender,
            recipients=[settings.gmail_address], subject=body.subject, body_text=body.body,
        )
        thread = Thread(id="eval-t", subject=body.subject, messages=[msg])
        contact = repo.get_or_default_contact(conn, body.sender, body.sender)
        ctx = retrieval.get_context(conn, thread, contact)
        decision = classifier.classify_thread(conn, llm, thread, ctx, prompts_dir=dry.prompts_dir)
        final = decide(thread, decision, contact, TierConfig.from_settings(dry))
        predicted = int(final.final_tier)
        result = {
            "predicted_label": rq.TIER_LABEL.get(predicted, "Working on it"),
            "predicted_tier": predicted,
            "why": decision.reasoning or final.surfaced_reason or "",
            "category": rq.CATEGORY_LABEL.get(decision.category, "Other"),
            "urgency": rq.URGENCY_LABEL.get(decision.stakes, "Worth noting"),
            "confidence": rq._confidence_phrase(decision.confidence),
        }
        if body.expected_tier is not None:
            result["expected_label"] = rq.TIER_LABEL.get(body.expected_tier, "?")
            result["match"] = predicted == body.expected_tier
        return result
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# P6 — dashboard rebuild: queue detail, commitments, voice, rules, audit,
# test-pipeline, metrics. Reads cache (30s; queue 5s); writes invalidate.
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/pipeline/status")
def api_pipeline_status(conn: sqlite3.Connection = Depends(get_conn)):
    return rq.get_pipeline(conn)  # never cached — real-time


@app.get("/api/queue/{message_id}")
def api_queue_detail(message_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = rq.get_pipeline_detail(conn, message_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="not found")
    return detail


@app.get("/api/commitments")
def api_commitments(conn: sqlite3.Connection = Depends(get_conn)):
    return rq.list_commitments(conn)


@app.post("/api/commitments/{commitment_id}/done")
def api_commitment_done(commitment_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _invalidate()
    return service.commitment_done(conn, commitment_id)


@app.post("/api/commitments/{commitment_id}/snooze")
def api_commitment_snooze(commitment_id: str, body: SnoozeBody = SnoozeBody(),
                          conn: sqlite3.Connection = Depends(get_conn)):
    _invalidate()
    return service.commitment_snooze(conn, commitment_id, body.days)


@app.get("/api/voice-profiles")
def api_voice_profiles(conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": _cached("voice", 30, lambda: rq.list_voice_profiles(conn))}


@app.post("/api/voice-profiles/rebuild")
def api_voice_rebuild(conn: sqlite3.Connection = Depends(get_conn),
                      settings=Depends(get_settings), llm=Depends(get_llm)):
    _invalidate()
    return service.rebuild_voice(conn, llm, settings)


@app.get("/api/voice-profiles/{segment}/samples")
def api_voice_samples(segment: str, limit: int = 20, offset: int = 0,
                      conn: sqlite3.Connection = Depends(get_conn)):
    # samples are tagged by sender segment at read time
    from assistant.memory import contacts as mc
    rows = repo.all_voice_samples(conn)
    matching = [r for r in rows if (mc.detect_segment(conn, r["contact_email"] or "") == segment)]
    page = matching[offset:offset + limit]
    return {"items": [{"subject": r["subject"] or "", "body": (r["body"] or "")[:400]} for r in page],
            "total": len(matching)}


@app.post("/api/contacts/{email}/update")
def api_contact_update(email: str, body: ContactUpdateBody,
                       conn: sqlite3.Connection = Depends(get_conn)):
    _invalidate()
    return service.update_contact(conn, email, flags=body.flags, importance=body.importance)


@app.post("/api/contacts/save")
def api_contact_save(body: ContactSaveBody, conn: sqlite3.Connection = Depends(get_conn)):
    """Save an unknown/unsaved sender as a real contact (name + optional phone bridge).
    Recognition-only; never sends anything."""
    ident = (body.identifier or "").strip()
    name = (body.name or "").strip()
    if not ident or not name:
        return {"ok": False, "error": "identifier and name are required"}
    res = service.save_contact(conn, ident, name, phone=(body.phone or "").strip(),
                               email=(body.email or "").strip())
    _invalidate()
    return res


@app.get("/api/settings/about")
def api_owner_about_get(conn: sqlite3.Connection = Depends(get_conn)):
    """The owner's self-description (Settings → 'About you')."""
    return service.get_owner_about(conn)


@app.post("/api/settings/about")
def api_owner_about_set(body: OwnerAboutBody, conn: sqlite3.Connection = Depends(get_conn)):
    """Save the owner's self-description. Trusted context for triage + drafting; takes
    effect on the next message, no restart. Owner-only (same localhost/owner posture as
    every other write route). Never sends anything."""
    res = service.set_owner_about(conn, body.about or "")
    _invalidate()
    return res


@app.get("/api/rules/proposed")
def api_rules_proposed(conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": rq.list_proposed_rules(conn)}


@app.post("/api/rules/{rule_id}/confirm")
def api_rule_confirm(rule_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _invalidate()
    return service.confirm_rule(conn, rule_id)


@app.post("/api/rules/{rule_id}/reject")
def api_rule_reject(rule_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _invalidate()
    return service.reject_rule(conn, rule_id)


@app.post("/api/rules/{rule_id}/delete")
def api_rule_delete(rule_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Remove a standing rule from the Active-rules list (any status)."""
    try:
        removed = repo.delete_rule(conn, int(rule_id))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad rule id"}
    conn.commit()
    _invalidate()
    return {"ok": removed}


@app.get("/api/audit-log")
def api_audit_log(start: int = 0, end: int = 0, tier: Optional[int] = None,
                  contact: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": rq.audit_filtered(conn, start=start, end=end, tier=tier, contact=contact)}


@app.get("/api/audit-log/export")
def api_audit_export(start: int = 0, end: int = 0, tier: Optional[int] = None,
                     contact: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    import csv
    import io

    rows = rq.audit_filtered(conn, start=start, end=end, tier=tier, contact=contact, limit=5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "kind", "tier", "message_id", "detail", "was_dry_run"])
    for r in rows:
        w.writerow([r["at"], r["kind"], r["tier"], r["message_id"], r["detail"], r["was_dry_run"]])
    return PlainTextResponse(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-log.csv"},
    )


@app.post("/api/test-pipeline")
def api_test_pipeline(body: TestPipelineBody, settings=Depends(get_settings), llm=Depends(get_llm)):
    """Run the FULL brain on a pasted email in an in-memory DB — ZERO side effects
    regardless of MODE. Returns every step (THINK/JUDGE/CRITIQUE/guardrails/tier/draft)."""
    from assistant.brain import classifier, guardrails
    from assistant.brain.tiers import TierConfig, decide
    from assistant.memory import retrieval
    from assistant.models import Message, Thread

    dry = dataclasses.replace(settings, mode="dry_run")
    conn = db.open_db(":memory:")
    try:
        msg = Message(id="test-1", thread_id="test-t", sender_email=body.sender,
                      sender_name=body.sender, recipients=[settings.gmail_address],
                      subject=body.subject, body_text=body.email_text)
        thread = Thread(id="test-t", subject=body.subject, messages=[msg])
        contact = repo.get_or_default_contact(conn, body.sender, body.sender)
        ctx = retrieval.get_context(conn, thread, contact)
        decision = classifier.classify_thread(conn, llm, thread, ctx, prompts_dir=dry.prompts_dir)
        final = decide(thread, decision, contact, TierConfig.from_settings(dry))
        detail = rq.get_pipeline_detail(conn, "test-1") or {}
        return {
            "note": "Test run — zero side effects.",
            "final_tier": int(final.final_tier),
            "final_label": rq.TIER_LABEL.get(int(final.final_tier), "?"),
            "base_tier": int(final.base_tier),
            "was_critical": guardrails.is_critical(thread, contact),
            "guardrail_floors": final.applied_floors,
            "surfaced_reason": final.surfaced_reason or "",
            "category": rq.CATEGORY_LABEL.get(decision.category, "Other"),
            "confidence": rq._confidence_phrase(decision.confidence),
            "reasoning": detail.get("reasoning"),
        }
    finally:
        conn.close()


_METRIC_CACHE_TTL_SECONDS = 3600  # never serve a dashboard metric older than 1h


def _metric(conn, key: str):  # type: ignore[no-untyped-def]
    """Serve the pre-aggregated snapshot only while it's FRESH (<= TTL); otherwise recompute
    live and backfill. Without the TTL the dashboard served a frozen, possibly day-stale
    snapshot as if it were current."""
    from assistant.storage import metrics
    cached = metrics.cache_get(conn, key, max_age_seconds=_METRIC_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    live = {
        "daily": lambda: rq.metrics_daily_breakdown(conn, 30),
        "accuracy": lambda: rq.metrics_accuracy(conn, 30),
        "costs": lambda: rq.metrics_costs(conn, 30),
        "response_times": lambda: rq.metrics_response_times(conn),
    }[key]()
    metrics.cache_set(conn, key, live)
    return live


@app.get("/api/metrics/daily")
def api_metrics_daily(conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": _metric(conn, "daily")}


@app.get("/api/metrics/accuracy")
def api_metrics_accuracy(conn: sqlite3.Connection = Depends(get_conn)):
    return _metric(conn, "accuracy")


@app.get("/api/metrics/costs")
def api_metrics_costs(conn: sqlite3.Connection = Depends(get_conn)):
    return {"items": _metric(conn, "costs")}


@app.get("/api/metrics/response-times")
def api_metrics_response_times(conn: sqlite3.Connection = Depends(get_conn)):
    return _metric(conn, "response_times")


@app.websocket("/ws/pipeline")
async def ws_pipeline(ws: WebSocket):
    """Push pipeline status whenever it changes (client falls back to polling on
    disconnect). Localhost only, same as the HTTP API."""
    import asyncio
    import json as _json

    await ws.accept()
    settings = get_settings()
    last = None
    try:
        while True:
            conn = db.connect(settings.db_path)
            try:
                status = rq.get_pipeline(conn)
            finally:
                conn.close()
            blob = _json.dumps(status, sort_keys=True)
            if blob != last:
                await ws.send_json(status)
                last = blob
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - never let the socket loop crash the server
        log.debug("ws/pipeline closed", exc_info=True)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


@app.post("/miniapp/auth")
async def miniapp_auth(request: Request):
    if not _miniapp_auth_available:
        raise HTTPException(status_code=503, detail="Mini App auth not configured")
    try:
        body = await request.json()
        init_data = body.get("init_data", "")
        bot_token = (
            getattr(_settings, "telegram_bot_token", "")
            or getattr(_settings, "bot_token", "")
            or getattr(_settings, "telegram_token", "")
        )
        user = validate_init_data(init_data, bot_token)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid initData")
        secret = getattr(_settings, "miniapp_secret", "") or bot_token
        token = create_session_token(str(user["user"].get("id", "unknown")), secret)
        return {"token": token, "expires_in": 3600}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_error(e, "miniapp_auth"))


@app.get("/state/snapshot")
async def state_snapshot(conn: sqlite3.Connection = Depends(get_conn)):
    if _state_engine is None:
        return {"error": "state_engine not available"}
    try:
        return _state_engine.get_state_snapshot(conn)
    except Exception as e:
        return {"error": _safe_error(e, "state_snapshot")}


@app.get("/state/waiting")
async def state_waiting(conn: sqlite3.Connection = Depends(get_conn)):
    if _state_engine is None:
        return {"waiting_on_me": [], "waiting_on_them": []}
    try:
        return {
            "waiting_on_me": _state_engine.waiting_on_me(conn),
            "waiting_on_them": _state_engine.waiting_on_them(conn),
        }
    except Exception as e:
        return {"error": _safe_error(e, "state_waiting")}


@app.post("/compose")
async def compose_message(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    settings=Depends(get_settings),
    llm=Depends(get_llm),
):
    if _compose is None:
        raise HTTPException(status_code=503, detail="Compose not available")
    try:
        body = await request.json()
        intent = body.get("intent", "")
        channel = body.get("channel", "auto")
        result = _compose.compose_and_queue(intent, channel, conn, settings, llm)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_error(e, "compose"))


@app.get("/opportunities")
async def get_opportunities(
    type: str = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if _opportunities is None:
        return []
    try:
        return _opportunities.get_opportunity_pipeline(conn, opp_type=type)
    except Exception as e:
        return {"error": _safe_error(e, "get_opportunities")}


@app.patch("/opportunities/{opp_id}/stage")
async def update_opportunity_stage(
    opp_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if _opportunities is None:
        raise HTTPException(status_code=503, detail="Not available")
    try:
        body = await request.json()
        stage = body.get("stage")
        from assistant.storage import operating_state as _os_store
        success = _os_store.update_opportunity_stage(conn, opp_id, stage)
        return {"success": success}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": _safe_error(e, "update_opportunity_stage")}


@app.get("/projects")
async def get_projects(conn: sqlite3.Connection = Depends(get_conn)):
    if _projects is None:
        return []
    try:
        return _projects.get_all_project_summaries(conn)
    except Exception as e:
        return {"error": _safe_error(e, "get_projects")}


@app.get("/calendar/freebusy")
async def calendar_freebusy(
    start: str,
    end: str,
    settings=Depends(get_settings),
):
    if _calendar is None or not getattr(settings, "calendar_enabled", False):
        return []
    try:
        from datetime import datetime
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        return _calendar.get_freebusy(start_dt, end_dt, settings)
    except Exception as e:
        return {"error": _safe_error(e, "calendar_freebusy")}


@app.post("/calendar/propose")
async def calendar_propose(
    request: Request,
    settings=Depends(get_settings),
):
    if _calendar is None:
        return []
    try:
        body = await request.json()
        slots = _calendar.propose_meeting_times(
            body.get("duration_minutes", 60),
            body.get("count", 3),
            settings,
        )
        return [{"label": s["label"]} for s in slots]
    except Exception as e:
        return {"error": _safe_error(e, "calendar_propose")}


@app.post("/calendar/event")
async def calendar_create_event(
    request: Request,
    settings=Depends(get_settings),
):
    if _calendar is None:
        raise HTTPException(status_code=503, detail="Calendar not available")
    try:
        from datetime import datetime
        body = await request.json()
        result = _calendar.create_calendar_event(
            body["summary"],
            datetime.fromisoformat(body["start"]),
            datetime.fromisoformat(body["end"]),
            body.get("attendees", []),
            settings,
        )
        return result or {"error": "Failed to create event"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_error(e, "calendar_create_event"))


def _mount_frontend() -> None:
    """Serve the built React dashboard (frontend/dist) at / when it exists, so the Mac
    app has ONE production dashboard URL (http://127.0.0.1:8000). No-op in dev (when
    only the Vite server is running) — additive and harmless if dist is absent."""
    try:
        from pathlib import Path
        from fastapi.staticfiles import StaticFiles

        dist = Path(__file__).parent / "frontend" / "dist"
        if dist.is_dir():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
            log.info("serving built dashboard from %s", dist)
    except Exception:  # noqa: BLE001 - never block the API on static serving
        log.debug("frontend mount skipped", exc_info=True)


_mount_frontend()


def _run_startup_sync() -> None:
    """Best-effort macOS Contacts sync on first start."""
    try:
        from assistant.memory import phone_contacts
        from assistant.storage import db as _db
        _settings = load_settings()
        with _db.get_connection(_settings.db_path) as _conn:
            result = phone_contacts.sync(_conn)
            log.info("startup contacts sync: %s", result)
    except Exception:  # noqa: BLE001
        log.debug("startup contacts sync failed (non-fatal)", exc_info=True)


import threading as _threading
_threading.Thread(target=_run_startup_sync, daemon=True, name="startup-contacts-sync").start()


def main() -> None:
    import uvicorn

    log.info("starting console API on http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
