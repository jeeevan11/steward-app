"""Orchestrator: wire every layer together and run.

Concurrency: the Telegram bot owns the asyncio loop on the MAIN thread; the Gmail
poller runs on a BACKGROUND thread. They never share a SQLite connection — each
thread opens its own (WAL allows concurrent readers + a single writer). Outbound
notifications from the poller use control.notifier (plain HTTP), so there is no
cross-thread asyncio coupling.

Heavy leaf modules (Gmail/Telegram SDKs) are imported lazily inside the functions
that need them, so `--status` and the test suite don't require them installed.
"""

from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime
from typing import Optional

from assistant.brain import classifier
from assistant.brain.tiers import TierConfig, decide as decide_tier
from assistant.config import Settings, load_settings
from assistant.models import Channel
from assistant.logging_setup import get_logger, setup_logging
from assistant.memory import contacts as memory_contacts
from assistant.memory import retrieval
from assistant.storage import db, decision_log, ledger, metrics
from assistant.storage import repositories as repo

try:
    from assistant.storage import operating_state as _os_store
except ImportError:
    _os_store = None

try:
    from assistant.memory import projects as _projects_module
except ImportError:
    _projects_module = None

try:
    from assistant.memory import opportunities as _opp_module
except ImportError:
    _opp_module = None

try:
    from assistant.control import state_engine as _state_engine_ref
except ImportError:
    _state_engine_ref = None

log = get_logger("main")

_stop = threading.Event()
# Set by the Gmail push receiver (P0a) to wake the GMAIL poller immediately; also set on
# shutdown so the poller's interruptible wait returns at once.
_wake = threading.Event()
# Set to wake the WHATSAPP poller immediately (manual "fetch now" + shutdown). Lets a UI
# button force an instant settle-drain + process instead of waiting for the poll interval.
_wa_wake = threading.Event()


def trigger_poll() -> None:
    """Wake BOTH pollers right now (manual 'fetch everything'). Idempotent + thread-safe:
    the events just short-circuit each poller's interruptible wait, so the next pass runs
    immediately. Never sends anything — it only triggers fetch + process (drafts still
    require your approval)."""
    log.info("manual fetch-now: waking Gmail + WhatsApp pollers")
    _wake.set()
    _wa_wake.set()


def _push_setup_banner(settings: Settings) -> str:
    topic = settings.gmail_pubsub_topic
    return (
        "\n".join([
            "─" * 64,
            "Gmail push (Pub/Sub) one-time setup:",
            f"  1. Create a topic + push subscription for: {topic}",
            "  2. Grant gmail-api-push@system.gserviceaccount.com Pub/Sub Publisher on it.",
            f"  3. Expose 127.0.0.1:{settings.gmail_pubsub_port} via a tunnel (cloudflared/ngrok)",
            "     and point the push subscription at <tunnel-url>/.",
            "  Until then it falls back to polling. See docs/INTELLIGENCE.md → Push.",
            "─" * 64,
        ])
    )


def _setup_push(conn, settings: Settings, mail, notifier):
    """Register the Gmail watch + start the localhost push receiver (opt-in).

    Returns the PushReceiver (or None). Prints the delivery status at startup so the
    operator can see whether they're on push (<10s) or polling. Never raises —
    any failure degrades silently to polling."""
    if not settings.gmail_pubsub_topic:
        print(f"Push notifications: disabled (polling every {settings.poll_interval_seconds}s)")
        return None
    from assistant.ingest import gmail_push

    try:
        resp = gmail_push.register_watch(mail.service, settings.gmail_pubsub_topic)
        repo.kv_set(conn, "gmail_watch_expiry_ms", str(gmail_push.watch_expiry_ms(resp)))
        receiver = gmail_push.PushReceiver(settings.gmail_pubsub_port, lambda hid: _wake.set())
        receiver.start()
        print("Push notifications: active (< 10s delivery)")
        print(_push_setup_banner(settings))
        log.info("gmail watch registered (expiry=%s)", repo.kv_get(conn, "gmail_watch_expiry_ms"))
        return receiver
    except Exception as exc:  # noqa: BLE001 - fall back to polling, surface once
        print(f"Push notifications: disabled (watch registration failed: {exc})")
        log.warning("gmail push setup failed; using polling: %s", exc)
        _safe_notify(notifier, f"⚠️ Gmail push setup failed ({exc}); falling back to polling.")
        return None


def _maybe_renew_watch(conn, settings: Settings, mail) -> None:
    """Re-register the Gmail watch when it's near expiry (watches last ~7 days)."""
    if not settings.gmail_pubsub_topic:
        return
    from assistant.ingest import gmail_push

    try:
        expiry = int(repo.kv_get(conn, "gmail_watch_expiry_ms") or 0)
        now_ms = repo.now_epoch() * 1000
        if gmail_push.should_renew(expiry, now_ms):
            resp = gmail_push.register_watch(mail.service, settings.gmail_pubsub_topic)
            repo.kv_set(conn, "gmail_watch_expiry_ms", str(gmail_push.watch_expiry_ms(resp)))
            log.info("gmail watch renewed (expiry=%s)", repo.kv_get(conn, "gmail_watch_expiry_ms"))
    except Exception:  # noqa: BLE001 - renewal is best-effort; polling still covers us
        log.warning("gmail watch renewal failed (non-fatal)", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Core processing
# ─────────────────────────────────────────────────────────────────────────────
def _feedback_deprioritized(conn, settings: Settings, contact) -> bool:
    """Layer 1E: has the owner repeatedly skipped this sender with ~no approvals? If so,
    surface them more quietly (the tier engine clamps this to the guardrail floor and
    never applies it to VIP/personal/high-stakes). Pure read of learning_events."""
    if not getattr(settings, "feedback_tuning_enabled", True):
        return False
    if not contact or not contact.email or contact.is_vip(settings.vip_importance_threshold):
        return False
    from assistant.storage import repositories as repo

    threshold = int(getattr(settings, "feedback_skip_threshold", 3))
    skips = repo.count_events(conn, type="skip", contact_email=contact.email)
    if skips < threshold:
        return False
    approves = repo.count_events(conn, type="approve", contact_email=contact.email)
    edits = repo.count_events(conn, type="edit", contact_email=contact.email)
    return skips > (approves + edits)  # clear net-negative signal


# failure-recovery-4 — idempotency guard for the post-dispatch side effects.
# ROOT CAUSE: process_one commits dispatch (card delivered) and only LATER calls
# ledger.complete. The best-effort side effects between/after those two steps
# (retrieval.record_episode, distill, commitments.capture_from_inbound, project
# auto-tag, opportunity detection) are NOT idempotent. If the engine is OOM-killed
# after dispatch but before complete, ledger.recover_stale flips the row back to
# SEEN, the poller re-claims it, create_pending dedups the card (no second send —
# the exactly-once invariant holds), but the side effects RE-RUN: a duplicate
# 'surfaced' episode is appended (evicting a real older episode under the cap) and
# duplicate DISTILL / opportunity / project-tag / commitment-extract LLM calls fire
# (untracked spend + possible duplicate rows).
# FIX: stamp a per-message_id marker the FIRST time the side effects run and skip
# them on any replay where the marker already exists. The marker lives in its own
# table created here (shared schema files are off-limits to this agent); the row is
# written on the SAME connection inside the same transaction as the side effects, so
# a crash before commit leaves no marker and the work is retried exactly once.
def _ensure_side_effects_table(conn) -> None:
    """Create the idempotency-marker table if absent. Safe to call repeatedly."""
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS process_side_effects ("
            "  message_id TEXT PRIMARY KEY,"
            "  done_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))"
            ")"
        )
    except Exception:  # noqa: BLE001 - never break processing on a marker-table issue
        log.debug("side-effects marker table ensure failed (non-fatal)", exc_info=True)


def _side_effects_already_done(conn, message_id: str) -> bool:
    """True iff the post-dispatch side effects for this message already ran (a replay).
    Fail-open to False so a marker-table problem degrades to the prior (re-run) behavior
    rather than silently skipping side effects on a first, legitimate run."""
    if not message_id:
        return False
    try:
        _ensure_side_effects_table(conn)
        row = conn.execute(
            "SELECT 1 FROM process_side_effects WHERE message_id=?", (message_id,)
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _mark_side_effects_done(conn, message_id: str) -> None:
    """Stamp the per-message marker (BEFORE the side effects run, under autocommit) so a
    later replay skips the non-idempotent side effects. INSERT OR IGNORE keeps it a no-op
    if a concurrent run already stamped it."""
    if not message_id:
        return
    try:
        _ensure_side_effects_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO process_side_effects (message_id) VALUES (?)",
            (message_id,),
        )
    except Exception:  # noqa: BLE001
        log.debug("side-effects marker write failed (non-fatal)", exc_info=True)


def process_one(conn, settings: Settings, mail, llm, notifier, message_id: str) -> None:
    """Run the full pipeline for a single claimed message. Fail-safe: any error is
    surfaced to the human and the ledger row is marked FAILED (never silent)."""
    from assistant.action import dispatcher

    thread = mail.get_thread(message_id)
    conn.execute(
        "UPDATE processed_messages SET thread_id=? WHERE message_id=?",
        (thread.id, message_id),
    )
    inbound = thread.latest_inbound or thread.latest
    if inbound is None:
        ledger.complete(conn, message_id, tier=0, category="other", confidence=1.0,
                        dry_run=settings.dry_run)
        return

    # Never process mail the principal sent to themselves (their other inboxes).
    if inbound.sender_email and inbound.sender_email.lower() in settings.self_addresses:
        log.info("skipping message from self address: %s", inbound.sender_email)
        ledger.complete(conn, message_id, tier=0, category="self_skipped",
                        confidence=1.0, dry_run=settings.dry_run)
        return

    contact = memory_contacts.resolve_sender(conn, inbound)
    memory_contacts.observe_thread(conn, thread, settings.gmail_address)

    # Only draft when THEY had the last word. If the latest message on the thread is the
    # owner's own (they already replied — in Gmail, on their phone, anywhere), the ball is
    # in the other party's court; there is nothing here to reply to. Drafting now would
    # fabricate a reply to a conversation that has already moved on (the "Chinese supplier"
    # bug: owner sent 'Let's stop and call them', yet a stale draft kept sitting in the
    # queue). We've already observed the thread for voice/memory above; close any open card
    # on it (cross-surface resolution) and complete without surfacing — no LLM call, no card.
    latest = thread.latest
    if latest is not None and getattr(latest, "from_me", False):
        try:
            closed = repo.resolve_handled_elsewhere(conn, thread.id)
        except Exception:  # noqa: BLE001 — cleanup must never break completion
            closed = []
            log.debug("owner-replied cleanup failed (non-fatal)", exc_info=True)
        if closed and notifier is not None:
            who = (contact.name if contact else "") or inbound.sender_name or inbound.sender_email \
                or "that thread"
            n = len(closed)
            try:
                notifier.send_text(
                    f"You replied to {who} yourself — cleared {n} item"
                    f"{'s' if n != 1 else ''} from your queue.")
            except Exception:  # noqa: BLE001
                log.debug("owner-replied notify failed (non-fatal)", exc_info=True)
        log.info("owner replied last on thread %s — no draft (ball in their court)", thread.id)
        ledger.complete(conn, message_id, tier=0, category="owner_replied",
                        confidence=1.0, dry_run=settings.dry_run)
        return

    # Memory Part A: resolve this sender to a cross-channel PERSON and, if a weak
    # match turns up, ask Jatin once to confirm. Strictly best-effort — any failure
    # here must NOT affect classification; the brain falls through to the thread alone.
    person_id = ""
    if settings.memory_enabled:
        try:
            from assistant.memory import identity
            res = identity.resolve(conn, inbound)
            person_id = res.person_id or ""
            if (res.suggestion and settings.link_suggestions_enabled
                    and notifier is not None):
                try:
                    notifier.send_link_suggestion(res.suggestion)
                except Exception:  # noqa: BLE001 - surfacing is best-effort
                    log.warning("link suggestion notify failed", exc_info=True)
        except Exception:  # noqa: BLE001 - memory is additive; degrade to thread-only
            log.warning("person resolution failed (non-fatal); continuing on thread alone",
                        exc_info=True)

    context = retrieval.get_context(conn, thread, contact)
    # P4a: fold one line of calendar context into the classifier prompt (opt-in;
    # empty + harmless when CALENDAR_ENABLED is off or the calendar is unreachable).
    try:
        from assistant.memory import calendar_context
        context.calendar_note = calendar_context.prompt_note(settings)
    except Exception:  # noqa: BLE001 - calendar is never allowed to break processing
        pass

    # Memory Part C: load this person's relationship record into the prompt (so the
    # brain reads the new message in light of what it already knows) and compute the
    # deterministic nudge signals for the tier engine. Best-effort — any failure here
    # falls through to thread-only classification (memory never breaks the decision).
    mem_signals = None
    if settings.memory_enabled and person_id:
        try:
            from assistant.memory import distill as distill_mod
            mem = distill_mod.load_memory(conn, person_id)
            context.person_id = person_id
            # memory-knowledge-2: pass conn+person_id so governance gates the READ path
            # (decayed/low-confidence facts are demoted out of the trusted block).
            context.memory_block = retrieval.build_memory_block(
                mem, cap=distill_mod.MEMORY_CHAR_CAP, conn=conn, person_id=person_id)
            mem_signals = retrieval.memory_signals(conn, person_id, thread, contact, settings, mem=mem)
        except Exception:  # noqa: BLE001 - degrade to thread-only
            log.warning("memory load failed (non-fatal); classifying on thread alone", exc_info=True)
            mem_signals = None

    # Fix 4: enrich context with graph signals — who is waiting on this person,
    # and who connects them to the owner. Best-effort; never breaks classification.
    if settings.memory_enabled and person_id:
        try:
            from assistant.memory import graph as _graph
            waiting = _graph.waiting_on_me(conn, person_id)
            connected = _graph.neighbors(conn, person_id, edge_type="knows", direction="both")
            parts: list[str] = []
            if waiting:
                names = ", ".join(n.get("name") or n.get("id") for n in waiting[:5])
                parts.append(f"Waiting on them: {names}")
            if connected:
                names = ", ".join(n.get("name") or n.get("id") for n in connected[:5]
                                  if (n.get("id") or "") != "owner")
                if names:
                    parts.append(f"Connected to: {names}")
            if parts:
                context.graph_block = "Graph: " + "; ".join(parts)
        except Exception:  # noqa: BLE001 - graph context is additive
            pass

    # Layer 1C: fold the recent rolling conversation (last N days, this chat) into the
    # prompt so the brain reasons over the relationship, not one orphan line. WhatsApp
    # only (email already carries its thread). Best-effort; never breaks classification.
    if thread.channel == Channel.WHATSAPP:
        try:
            from assistant.ingest import wa_context
            context.recent_conversation = wa_context.recent_block(
                conn, thread.id, days=settings.whatsapp_context_days,
                me_jid=settings.wa_user_jid)
        except Exception:  # noqa: BLE001 - context is additive
            pass

    # Layer 1B (presence) + 1E (feedback): compute the LOWERING signals. Presence =
    # the owner is handling this conversation himself right now. Deprioritized = he's
    # repeatedly skipped this sender. Both only ever lower (and never below the
    # guardrail floor / never for VIP/personal/high-stakes). Context is unaffected.
    suppress_active = False
    deprioritized = False
    if thread.channel == Channel.WHATSAPP:
        try:
            from assistant.control import presence
            suppress_active = presence.is_actively_handling(conn, settings, thread.id)
        except Exception:  # noqa: BLE001
            suppress_active = False
    try:
        deprioritized = _feedback_deprioritized(conn, settings, contact)
    except Exception:  # noqa: BLE001
        deprioritized = False

    decision = classifier.classify_thread(
        conn, llm, thread, context, prompts_dir=settings.prompts_dir
    )
    # Close the feedback loop: map the model's RAW confidence to its empirically-observed
    # accuracy (calibration.calibrated, recomputed nightly from real approve/skip/edit
    # outcomes). Previously the nightly calibration was computed but never consumed. Safe:
    # calibrated() falls back to the raw value until a confidence bucket has data, and the
    # guardrails still floor money/legal/investor/VIP regardless of confidence.
    try:
        from assistant.storage import calibration as _calibration
        import dataclasses as _dc
        cal_conf = _calibration.calibrated(conn, decision.confidence)
        if cal_conf != decision.confidence:
            decision = _dc.replace(decision, confidence=cal_conf)
    except Exception:  # noqa: BLE001 - calibration is best-effort, never blocks classification
        log.debug("confidence calibration skipped (non-fatal)", exc_info=True)
    final = decide_tier(thread, decision, contact, TierConfig.from_settings(settings),
                        memory=mem_signals, suppress_active=suppress_active,
                        deprioritized=deprioritized)

    log.info(
        "msg %s → tier %s (base %s, conf %.2f, cat %s)%s",
        message_id, int(final.final_tier), int(final.base_tier),
        final.confidence, decision.category,
        f" [surfaced: {final.surfaced_reason}]" if final.surfaced_reason else "",
    )

    # Additive: persist the full decision so the local web console can explain it in
    # plain English and compute the "nearly filed but looked important" stat. This is
    # the ONLY web-driven change to the core; it is best-effort and never raises.
    decision_log.record(conn, message=inbound, thread=thread, decision=decision,
                        final=final, dry_run=settings.dry_run)

    # Phase 2: capture a full, structured explanation of WHY this decision was made
    # (guardrails, memory/presence/feedback signals, model verdict, the full floor chain
    # + a human one-liner). Best-effort; joins decision_log/llm_calls by message_id.
    try:
        from assistant.storage import explanations
        explanations.record(conn, explanations.build(
            message_id, thread, contact, decision, final,
            memory=mem_signals, suppress_active=suppress_active, deprioritized=deprioritized))
    except Exception:  # noqa: BLE001 - explainability must never break the pipeline
        log.debug("explanation capture failed (non-fatal)", exc_info=True)

    # Phase 3: capture the replay inputs (prompt versions, models/params, context supplied)
    # so this decision can be fully reconstructed later. Best-effort.
    try:
        from assistant.storage import replay
        replay.capture(conn, settings, message_id,
                       context_supplied=context.render_for_prompt(),
                       thread_snapshot=thread.render_for_prompt())
    except Exception:  # noqa: BLE001
        log.debug("replay capture failed (non-fatal)", exc_info=True)

    dispatcher.dispatch(conn, settings, mail, llm, notifier, thread, contact, final, message_id)

    # failure-recovery-4: detect a crash-replay (the card was already dispatched and the
    # non-idempotent side effects already ran on a prior, crashed attempt). On a replay
    # we skip those side effects so the same item is not 'surfaced' twice into episodic
    # memory and the distill/opportunity/project/commitment LLM calls do not re-fire.
    #
    # The DB connection is autocommit (db.open_db sets isolation_level=None), so we stamp
    # the marker BEFORE running the side effects: the stamp commits immediately, so a
    # crash ANYWHERE inside the side-effects block still leaves the marker present and a
    # replay skips them all — guaranteeing each side effect runs at most once across
    # crash/recover/re-claim. The crash window the audit describes (after dispatch, before
    # ledger.complete) is therefore covered even though complete runs partway through.
    _replayed = _side_effects_already_done(conn, message_id)
    if _replayed:
        log.info("replay detected for %s — skipping non-idempotent side effects", message_id)
        try:
            repo.record_event(conn, type="side_effects_replay_skipped", message_id=message_id)
        except Exception:  # noqa: BLE001 - observability is best-effort
            pass
    else:
        _mark_side_effects_done(conn, message_id)

    # Memory Part C: log that we surfaced this to the person's episodic memory, so a
    # later message can see "I already showed this and Jatin skipped it" and not nag.
    if (not _replayed and settings.memory_enabled and person_id
            and int(final.final_tier) >= 2):  # APPROVE/ASK
        try:
            retrieval.record_episode(conn, person_id, action="surfaced",
                                     tier=int(final.final_tier), thread_id=thread.id)
        except Exception:  # noqa: BLE001
            pass

    ledger.complete(
        conn, message_id,
        tier=int(final.final_tier), category=decision.category,
        confidence=decision.confidence, dry_run=settings.dry_run,
    )

    # Memory Part B: distill the relationship AFTER the card is sent (post-card, so it
    # never delays the notification) and the message is marked done. Skip pure noise.
    # Best-effort — memory is additive and can never break processing.
    # GROUP GUARD (privacy): never distill a GROUP message into the poster's private 1:1
    # relationship memory — a group thread mixes many people, and the sender's distilled
    # facts/commitments would be polluted with group context (and later surface in 1:1
    # drafts). Only learn 1:1 memory from genuine 1:1 threads. (WhatsApp group jids end @g.us.)
    _is_group = (thread.channel == Channel.WHATSAPP and (thread.id or "").endswith("@g.us"))
    if not _replayed and settings.memory_enabled and person_id and not _is_group:
        try:
            from assistant.memory import distill as distill_mod
            if decision.category not in distill_mod.NOISE_CATEGORIES:
                distill_mod.distill(conn, llm, settings, person_id, thread)
                # Phase 8: capture commitments the OTHER party made to you ("I'll send it
                # Friday") from this inbound thread. Post-card, best-effort, non-noise only.
                from assistant.memory import commitments as commitments_mod
                commitments_mod.capture_from_inbound(conn, llm, settings, thread)
        except Exception:  # noqa: BLE001
            log.warning("distill failed (non-fatal)", exc_info=True)

    # Relationship graph: record the sender as a person node and link owner→sender so
    # graph-backed features actually have data (was never wired — graph stayed empty).
    if settings.memory_enabled and person_id:
        try:
            from assistant.memory import graph
            graph.upsert_node(conn, "owner", type="owner",
                              name=(settings.gmail_address or "owner"))
            graph.upsert_node(conn, person_id, type="person",
                              name=(contact.name or inbound.sender_email or ""))
            graph.add_edge(conn, "owner", person_id, "knows")
        except Exception:  # noqa: BLE001 - graph is additive; never break processing
            log.debug("graph update failed (non-fatal)", exc_info=True)

    # ── Operating State updates (best-effort, never crash the pipeline) ──
    try:
        if _os_store is not None:
            _tier_int = int(final.final_tier)
            if _tier_int == 3:
                _thread_status = 'awaiting_me'
            elif _tier_int in (1, 2):
                _thread_status = 'awaiting_them'
            else:
                _thread_status = 'quiet'
            _thread_id = thread.id
            _channel = str(thread.channel)
            _subject = thread.subject or ''
            _person_id = (contact.email if contact else '') or ''
            if _thread_id:
                _os_store.upsert_thread(conn, _thread_id, _channel, _thread_status,
                                        person_id=_person_id, subject=_subject)
    except Exception as _e:
        print(f"[operating_state] update failed: {_e}")

    # ── Project auto-tagging (best-effort) ──
    # Perf: only tag SURFACED items (tier >= 2). Running an LLM call on every tier-0/1
    # message (incl. noise/social/spam already filed) was ~1 wasted call per message.
    # failure-recovery-4: skip on replay so we don't re-fire the tag LLM call.
    try:
        if (not _replayed and _projects_module is not None
                and getattr(settings, 'project_tagging_enabled', True)
                and int(final.final_tier) >= 2):
            _snippet = ''
            try:
                _snippet = thread.messages[-1].body_text[:300] if thread.messages else ''
            except Exception:
                pass
            _projects_module.auto_tag_thread(
                thread.id, thread.subject or '',
                (contact.name if contact else '') or '',
                str(getattr(decision, 'category', '')),
                _snippet, conn, settings, llm)
    except Exception as _e:
        print(f"[projects] tagging failed: {_e}")

    # ── Opportunity detection (best-effort) ──
    # Perf: surfaced items only (tier >= 2) — same waste as project tagging above.
    # failure-recovery-4: skip on replay so we don't re-fire the opportunity LLM call
    # or write a duplicate opportunity row.
    try:
        if (not _replayed and _opp_module is not None
                and getattr(settings, 'opportunity_detection_enabled', True)
                and int(final.final_tier) >= 2):
            _sender_email = (contact.email if contact else '') or ''
            _sender_name = (contact.name if contact else '') or ''
            _category = str(getattr(decision, 'category', ''))
            _tier_str = str(int(final.final_tier))
            _snippet = ''
            try:
                _snippet = thread.messages[-1].body_text[:400] if thread.messages else ''
            except Exception:
                pass
            _opp_module.detect_opportunity(
                thread.id, thread.subject or '', _sender_name, _sender_email,
                _category, _tier_str, _snippet, conn, settings, llm)
    except Exception as _e:
        print(f"[opportunities] detection failed: {_e}")


def _is_wa(mid: str) -> bool:
    return mid.startswith("wa_")


def _not_wa(mid: str) -> bool:
    return not mid.startswith("wa_")


def poll_and_process(conn, settings: Settings, mail, llm, notifier, *, owns=None, do_redeliver=True) -> None:
    """One poll pass: detect new messages, then drain pending ledger work.

    ``owns`` (optional) lets a channel-specific poller claim only its own ids from
    the shared ledger (Gmail excludes ``wa_*``; the WhatsApp poller takes only
    ``wa_*``). Default None = claim everything (preserves the single-source behavior
    the existing tests rely on). ``do_redeliver`` is run by the primary poller only,
    so two pollers don't double-send undelivered cards.
    """
    if repo.is_paused(conn):
        log.info("paused — skipping poll")
        return

    try:
        new_ids = mail.fetch_new_message_ids()
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the loop
        log.exception("fetch_new_message_ids failed")
        _safe_notify(notifier, f"⚠️ Couldn't check for new messages: {exc}")
        return

    for mid in new_ids:
        ledger.mark_seen(conn, mid)  # dedup gate — exactly once

    for row in ledger.list_pending(conn):
        mid = row["message_id"]
        if owns is not None and not owns(mid):
            continue  # belongs to another channel's poller
        if not ledger.claim(conn, mid):
            continue
        try:
            process_one(conn, settings, mail, llm, notifier, mid)
        except Exception as exc:  # noqa: BLE001 — fail safe: surface + mark FAILED
            log.exception("processing failed for %s", mid)
            _safe_notify(notifier, f"⚠️ I hit an error on a message and stopped on it: {exc}")
            ledger.fail(conn, mid, str(exc))

    # Re-deliver any cards whose Telegram delivery previously failed, so a transient
    # outage can never silently swallow something that needs your eyes.
    if do_redeliver:
        redeliver_undelivered(conn, settings, notifier)


def redeliver_undelivered(conn, settings: Settings, notifier) -> None:
    """Re-send pending cards that were queued but never delivered to Telegram."""
    for row in repo.undelivered_pending(conn):
        try:
            if row["kind"] == "reminder":
                # A proactive reminder is an INFORMATIONAL nudge — deliver it as plain text,
                # never an Approve/Edit/Skip card. It has no draft, and its message_id is a
                # raw chat id (a WhatsApp JID), so surfacing it as a send-able card mis-routes
                # to Gmail and errors on approve (the live SEND_FAILED). Only mark delivered
                # once Telegram actually accepted it (else retry next poll).
                tg = notifier.send_text(row["summary"] or "Reminder")
                if tg:
                    repo.set_pending_telegram_message(conn, row["id"], settings.telegram_chat_id, str(tg))
                    log.info("delivered reminder #%s as FYI (no send card)", row["id"])
                continue
            if row["kind"] == "ask":
                tg = notifier.send_ask(row["id"], row["summary"] or "", row["draft_text"] or "")
            else:
                tg = notifier.send_approval(row["id"], row["summary"] or "", row["draft_text"] or "")
            if tg:
                repo.set_pending_telegram_message(conn, row["id"], settings.telegram_chat_id, tg)
                log.info("re-delivered undelivered pending #%s", row["id"])
        except Exception:  # noqa: BLE001
            log.warning("re-delivery of pending #%s failed", row["id"], exc_info=True)


def _safe_notify(notifier, text: str) -> None:
    try:
        notifier.error(text)
    except Exception:  # noqa: BLE001
        log.error("could not deliver notification: %s", text)


# ─────────────────────────────────────────────────────────────────────────────
# Gmail cross-surface: close cards the owner already answered IN Gmail.
# ─────────────────────────────────────────────────────────────────────────────
# `fetch_new_message_ids` is INBOX-only, so when the owner replies straight from
# Gmail (or any mail client) their Sent message never re-enters the pipeline — the
# drafted card just sits there, stale, while the thread has actually moved on. A
# system without a sense of time is a passive device; this gives the poller a
# heartbeat to reconcile the world it can't see pushed to it. Each pass re-reads
# the FULL thread for every open card (get_thread pulls the owner's Sent messages
# too) and, when the latest message in the thread is the owner's own, closes the
# card HANDLED_ELSEWHERE — a CLOSE, never a send, so a stale draft can never be
# approved into a duplicate reply. Throttled (a thread fetch per card is a network
# call) and fully best-effort: it must never raise into the poll loop.
_RECONCILE_INTERVAL_SECONDS = 120
_last_owner_reconcile = 0.0

# Card kinds that correspond to a real Gmail thread. Reminders carry a chat-id as
# their message_id (not a Gmail message id), so get_thread can't fetch them.
_GMAIL_CARD_KINDS = {"reply_draft", "ask", "surface", "fyi"}


def _thread_other_party(thread) -> str:
    """A human label for whoever Steward was drafting to on this thread."""
    m = thread.latest_inbound or thread.latest
    if m is None:
        return "that thread"
    return (getattr(m, "sender_name", "") or getattr(m, "sender_email", "")
            or thread.subject or "that thread")


def _reconcile_owner_replies(conn, settings: Settings, mail, notifier) -> None:
    """Close any open card whose thread the owner has already replied to in Gmail."""
    global _last_owner_reconcile
    now = time.time()
    if now - _last_owner_reconcile < _RECONCILE_INTERVAL_SECONDS:
        return
    _last_owner_reconcile = now
    try:
        rows = repo.open_pending(conn)
    except Exception:  # noqa: BLE001
        log.debug("reconcile: open_pending failed", exc_info=True)
        return

    seen_threads: set[str] = set()
    for row in rows:
        # Only untouched cards. APPROVED/EDITED means the owner is mid-action via
        # Steward — never yank a card out from under an in-progress approval.
        if (row["status"] or "") != "PENDING":
            continue
        mid = (row["message_id"] or "")
        if not mid or mid.startswith("wa_"):
            continue  # WhatsApp does its own cross-surface via ingest_outbound
        if (row["kind"] or "") not in _GMAIL_CARD_KINDS:
            continue  # reminders carry a chat-id, not a Gmail message id
        tid = (row["thread_id"] or "").strip()
        if not tid:
            # No thread to resolve by. A self-authored follow-up (e.g. a commitment
            # "Draft follow-up" card from telegram_bot.py) carries no thread_id; it is
            # not a reply awaiting the owner, and resolve_handled_elsewhere keys on
            # thread_id — fetching it would waste a get_thread call and the fallback key
            # (thread.id) would mis-close OTHER cards on that thread, not this one.
            continue
        if tid in seen_threads:
            continue  # one thread fetch already covered this card's siblings
        try:
            thread = mail.get_thread(mid)
        except Exception:  # noqa: BLE001 — one bad fetch must not stop the sweep
            log.debug("reconcile: get_thread(%s) failed", mid, exc_info=True)
            continue
        key = tid  # guaranteed non-empty (empty thread_ids skipped above)
        seen_threads.add(key)
        latest = thread.latest
        if latest is None or not getattr(latest, "from_me", False):
            continue  # they haven't replied themselves — leave the card live
        try:
            closed = repo.resolve_handled_elsewhere(conn, key)
        except Exception:  # noqa: BLE001
            log.debug("reconcile: resolve_handled_elsewhere(%s) failed", key, exc_info=True)
            continue
        if not closed:
            continue
        try:
            repo.record_event(conn, type="handled_elsewhere",
                              detail={"thread_id": key, "count": len(closed), "channel": "gmail"})
        except Exception:  # noqa: BLE001
            pass
        conn.commit()
        who = _thread_other_party(thread)
        n = len(closed)
        log.info("reconcile: owner replied in Gmail to %s — closed %d card(s)", who, n)
        try:
            notifier.send_text(
                f"You replied to {who} yourself — cleared {n} item{'s' if n != 1 else ''} "
                "from your queue.")
        except Exception:  # noqa: BLE001
            log.debug("reconcile notify failed (non-fatal)", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Briefs (scheduled, once per day per kind)
# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-6 — catch-up window for hour-gated daily jobs.
# ROOT CAUSE: every scheduled job used exact-hour equality (`now.hour != hour`). A
# laptop asleep across the single target hour (e.g. lid closed 07:50-09:10 over the
# 08:00 brief) never sees `now.hour == 8`, the per-day stamp is never written, and
# that day's brief/sweep/state-update is silently skipped with no log and no retry.
# FIX: gate on a >=-with-upper-bound window keyed off the existing per-day kv stamp:
# fire once on the FIRST poll at-or-after the target hour (so a late wake still runs
# the job exactly once that day), stay silent before the hour, and no-op on repeat
# polls via the stamp. An upper bound (_CATCHUP_WINDOW_HOURS) prevents a very-late
# wake from firing an off-time job at, say, 23:00 — important for the evening brief
# so a midnight wake never blasts an "evening" brief in the dead of night.
_CATCHUP_WINDOW_HOURS = 6


def _due_today(now, target_hour: int, *, window_hours: int = _CATCHUP_WINDOW_HOURS) -> bool:
    """True when a once-daily job whose nominal time is `target_hour` should run on
    THIS poll: we are at or after the target hour but still within the catch-up window
    (target_hour <= now.hour < target_hour + window_hours). Replaces brittle exact-hour
    equality so a sleep spanning the target hour still fires the job once that day
    (the caller's per-day kv stamp provides the once-per-day dedup)."""
    try:
        h = int(now.hour)
        t = int(target_hour)
    except Exception:  # noqa: BLE001
        return False
    return t <= h < (t + int(window_hours))


def maybe_send_briefs(conn, settings: Settings, llm, notifier) -> None:
    from assistant.control import briefs

    try:
        now = datetime.now(_tz(settings))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    for kind, hour in (("morning", settings.morning_brief_hour),
                       ("evening", settings.evening_brief_hour)):
        # control-state-presence-6: catch-up window instead of exact-hour equality so a
        # wake AFTER the brief hour still delivers that day's brief exactly once.
        if not _due_today(now, hour):
            continue
        key = f"last_brief_{kind}"
        if repo.kv_get(conn, key) == today:
            continue
        try:
            text = briefs.generate_brief(conn, settings, llm, kind)
            # Silence beats noise: never send an empty scheduled brief. Mark it done
            # for today so we don't retry every minute of the brief hour.
            if text and text.strip() != briefs.EMPTY_BRIEF:
                notifier.send_text(text)
                # Observability: a "(catch-up)" tag makes a late-wake recovery visible
                # in the log rather than silent (control-state-presence-6).
                _late = "" if int(now.hour) == int(hour) else " (catch-up)"
                log.info("sent %s brief%s", kind, _late)
            else:
                log.info("%s brief empty — not sending (quiet window)", kind)
            repo.kv_set(conn, key, today)
        except Exception:  # noqa: BLE001
            log.exception("brief (%s) failed", kind)


def _tz(settings: Settings):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return None


def maybe_rebuild_voice(conn, settings: Settings, llm) -> None:
    """Weekly (Sunday 7pm local) rebuild of the per-segment voice profiles (P5a).
    Runs at most once per ISO week; never raises into the poller."""
    try:
        now = datetime.now(_tz(settings))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    if now.weekday() != 6 or now.hour != 19:  # Sunday, 19:00
        return
    week_key = now.strftime("%Y-W%W")
    if repo.kv_get(conn, "last_voice_rebuild") == week_key:
        return
    try:
        from assistant.action import voice

        rebuilt = voice.build_segment_profiles(conn, llm, settings)
        # Layer 1D: also refresh his learned WhatsApp texting style from his own sends.
        try:
            from assistant.action import wa_style
            wa_style.build_wa_style(conn, llm, settings)
        except Exception:  # noqa: BLE001
            log.warning("wa_style rebuild failed (non-fatal)", exc_info=True)
        repo.kv_set(conn, "last_voice_rebuild", week_key)
        log.info("weekly voice rebuild: %s", rebuilt)
    except Exception:  # noqa: BLE001
        log.exception("voice rebuild failed")


def maybe_aggregate_metrics(conn, settings: Settings) -> None:
    """Once-a-day pre-aggregation of dashboard metrics so the /metrics endpoints stay O(1).
    Runs the FIRST time the poller ticks each day (catch-up) — not pinned to 23:00, which
    silently skipped the refresh whenever the Mac was asleep at that exact hour, leaving the
    dashboard on a day-stale snapshot. The read-side TTL (api._metric) keeps it fresh between
    runs; this guarantees at least one fresh snapshot per day. Never raises into the poller."""
    try:
        now = datetime.now(_tz(settings))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if repo.kv_get(conn, "last_metrics_aggregate") == today:
        return
    try:
        metrics.populate(conn)
        repo.kv_set(conn, "last_metrics_aggregate", today)
        log.info("metrics pre-aggregated for the dashboard")
    except Exception:  # noqa: BLE001
        log.exception("metrics aggregation failed")


def _vip_emails(conn, settings: Settings) -> set[str]:
    """Emails treated as VIP (importance >= threshold OR a vip flag) — used to apply
    the tighter staleness thresholds in the commitment check."""
    out: set[str] = set()
    try:
        for row in conn.execute(
            "SELECT email FROM contacts WHERE importance >= ? OR flags LIKE '%vip%'",
            (settings.vip_importance_threshold,),
        ):
            if row["email"]:
                out.add(row["email"].lower())
    except Exception:  # noqa: BLE001
        pass
    return out


def maybe_surface_commitments(conn, settings: Settings, llm, notifier) -> None:
    """Daily (commitment_check_hour) sweep: due/stale commitments + stalled threads.
    Runs at most once per day; never raises into the poller."""
    try:
        now = datetime.now(_tz(settings))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    # control-state-presence-6: catch-up window (was exact-hour equality, so a sleep
    # spanning commitment_check_hour silently skipped the day's nudge).
    if not _due_today(now, settings.commitment_check_hour):
        return
    today = now.strftime("%Y-%m-%d")
    if repo.kv_get(conn, "last_commitment_check") == today:
        return
    try:
        from assistant.memory import commitments

        vips = _vip_emails(conn, settings)
        due = commitments.due_commitments(conn, now=now, vip_emails=vips)
        for c in due:
            line = (
                f"📋 Commitment check\nYou promised: {c['commitment_text']}\n"
                f"To: {c['contact_email'] or 'someone'}"
            )
            if c["due_date"]:
                line += f"\nDue: {c['due_date']}"
            notifier.send_commitment(c["id"], line)
        stale = commitments.stale_threads(conn, now=now, vip_emails=vips)
        for s in stale:
            notifier.send_text(
                f"⏰ {s['email']} hasn't heard back in {s['days']} days — "
                f"last topic: {s['subject'] or '(no subject)'}"
            )
        repo.kv_set(conn, "last_commitment_check", today)
        log.info("commitment check: %d due, %d stale", len(due), len(stale))
    except Exception:  # noqa: BLE001
        log.exception("commitment check failed")


# ─────────────────────────────────────────────────────────────────────────────
# Runners
# ─────────────────────────────────────────────────────────────────────────────
def _build_mail(conn, settings: Settings, notifier=None):
    from assistant.ingest.gmail_source import GmailSource

    mail = GmailSource(conn, settings)
    # NO_SILENT_LOSS: surface Gmail history-gap resyncs to the owner (finding
    # `ingest-email-1`). Best-effort — a missing notifier just means log+metric only.
    if notifier is not None:
        mail.on_coverage_gap = lambda text: _safe_notify(notifier, text)
    mail.connect()
    return mail


def _poller_loop(settings: Settings, llm, notifier) -> None:
    """Background thread: its own DB connection + Gmail client. When EMAIL_ENABLED is
    off, runs without Gmail (no creds needed) but still keeps the generic periodic jobs
    + heartbeat alive so the Mac app and WhatsApp side keep working."""
    conn = db.open_db(settings.db_path)
    ledger.recover_stale(conn)
    mail = None
    if settings.email_enabled:
        try:
            mail = _build_mail(conn, settings, notifier)
        except Exception:  # noqa: BLE001
            log.exception("poller could not connect to Gmail; running without it")
            _safe_notify(notifier, "⚠️ I couldn't connect to Gmail — email polling is off.")
            mail = None

    receiver = _setup_push(conn, settings, mail, notifier) if mail is not None else None
    log.info("poller started (interval %ss, mode=%s, email=%s)",
             settings.poll_interval_seconds, settings.mode, settings.email_enabled and mail is not None)
    while not _stop.is_set():
        try:
            if mail is not None:
                _maybe_renew_watch(conn, settings, mail)
                # Gmail poller owns everything EXCEPT wa_* ids (those are the WhatsApp
                # poller's), and is the one that runs card re-delivery.
                poll_and_process(conn, settings, mail, llm, notifier, owns=_not_wa, do_redeliver=True)
                # Gmail cross-surface: clear cards the owner already answered directly in Gmail.
                _reconcile_owner_replies(conn, settings, mail, notifier)
            # control-state-presence-5: briefs must NOT be gated behind `if mail`. A
            # WhatsApp-only deploy (EMAIL_ENABLED=false) sets mail=None, so the old
            # placement under the `if mail` block meant such users silently never got
            # morning/evening briefs. briefs.generate_brief is channel-agnostic (no mail
            # dependency), so the scheduler runs every iteration — mirroring the Phase 11
            # fix that moved commitments out of the same block. Card re-delivery stays
            # email-specific above; only the brief scheduler is decoupled.
            maybe_send_briefs(conn, settings, llm, notifier)
            # Channel-agnostic jobs run regardless of email (Phase 11: commitments were
            # wrongly gated under `if mail`, so a WhatsApp-only user never got nudges).
            maybe_surface_commitments(conn, settings, llm, notifier)
            maybe_run_proactive(conn, settings, notifier)
            maybe_rebuild_voice(conn, settings, llm)
            maybe_aggregate_metrics(conn, settings)
            maybe_calibrate(conn, settings)
            maybe_nightly_sync(conn, settings)
            maybe_decay_memory(conn, settings)
            maybe_prune(conn, settings)
            maybe_daily_state_update(conn, settings)
            _check_relay_health(conn, settings, notifier)
            _reap_stuck_sends(conn, settings, notifier)
            _write_heartbeat(conn, settings)
        except Exception:  # noqa: BLE001
            log.exception("poller iteration error")
        # Sleep until the next interval OR until a push wakes us (whichever first).
        # _wake is also set on shutdown so this returns promptly.
        _wake.wait(timeout=settings.poll_interval_seconds)
        _wake.clear()
    if receiver is not None:
        receiver.stop()
    conn.close()
    log.info("poller stopped")


def maybe_run_proactive(conn, settings: Settings, notifier) -> None:
    """Phase 9: once-daily curated proactive digest. run_sweep self-dedups + is hour-gated."""
    try:
        from assistant.control import proactive
        proactive.run_sweep(conn, settings, notifier)
    except Exception:  # noqa: BLE001 - proactive must never break the poller
        log.debug("proactive sweep failed (non-fatal)", exc_info=True)


def maybe_daily_state_update(conn, settings: Settings) -> None:
    """Daily (state_update_hour, default 7am local) maintenance of the operating state
    tables: ensure schema exists and create risk rows for overdue commitments.
    Runs at most once per day; never raises into the poller."""
    try:
        now = datetime.now(_tz(settings))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    # control-state-presence-6: catch-up window (was exact-hour equality, so a sleep
    # spanning state_update_hour silently skipped the day's risk-row refresh).
    if not _due_today(now, getattr(settings, 'state_update_hour', 7)):
        return
    today = now.strftime("%Y-%m-%d")
    if repo.kv_get(conn, "last_state_update") == today:
        return
    try:
        if _os_store is not None:
            _os_store.ensure_tables(conn)
        # Derive risk records from overdue items
        if _state_engine_ref is not None and _os_store is not None:
            overdue = _state_engine_ref.overdue_commitments(conn)
            for item in overdue:
                desc = item.get('description', '') or str(item)[:100]
                _os_store.create_risk(conn, type='overdue_commitment',
                                      description=desc, severity='high')
        repo.kv_set(conn, "last_state_update", today)
        log.info("daily state update complete (%s overdue items processed)",
                 len(_state_engine_ref.overdue_commitments(conn)) if _state_engine_ref else 0)
    except Exception as e:
        print(f"[state_update] failed: {e}")


def maybe_calibrate(conn, settings: Settings) -> None:
    """Phase 5: once per day, recompute confidence calibration from decisions + outcomes."""
    try:
        from datetime import datetime
        day_key = datetime.now().strftime("%Y-%m-%d")
        if repo.kv_get(conn, "last_calibration") == day_key:
            return
        from assistant.storage import calibration
        calibration.compute(conn)
        repo.kv_set(conn, "last_calibration", day_key)
    except Exception:  # noqa: BLE001
        log.debug("calibration failed (non-fatal)", exc_info=True)


def maybe_nightly_sync(conn, settings: Settings) -> None:
    """Fix 2: once per day, sync all contacts from the persons table into the graph
    so graph-backed features reflect the full contact roster, not only contacts seen
    today. Piggybacks on the calibration day-key (same 24h cadence, different kv key)."""
    try:
        from datetime import datetime
        day_key = datetime.now().strftime("%Y-%m-%d")
        if repo.kv_get(conn, "last_graph_sync") == day_key:
            return
        from assistant.memory import graph
        counts = graph.sync_from_persons(conn)
        repo.kv_set(conn, "last_graph_sync", day_key)
        log.info("nightly graph sync: %s", counts)
    except Exception:  # noqa: BLE001
        log.debug("nightly graph sync failed (non-fatal)", exc_info=True)


def maybe_decay_memory(conn, settings: Settings) -> None:
    """Phase 6: once per day, decay unreinforced memory confidence so weak facts fade.
    Only adjusts memory confidence (never a guardrail floor). Best-effort."""
    if not getattr(settings, "memory_governance_enabled", True):
        return
    try:
        from datetime import datetime
        day_key = datetime.now().strftime("%Y-%m-%d")
        if repo.kv_get(conn, "last_memory_decay") == day_key:
            return
        from assistant.memory import governance
        governance.decay(conn)
        repo.kv_set(conn, "last_memory_decay", day_key)
    except Exception:  # noqa: BLE001
        log.debug("memory decay failed (non-fatal)", exc_info=True)


def maybe_prune(conn, settings: Settings) -> None:
    """Phase 11: once per day, trim unbounded high-volume tables. Best-effort."""
    try:
        from datetime import datetime
        day_key = datetime.now().strftime("%Y-%m-%d")
        if repo.kv_get(conn, "last_prune") == day_key:
            return
        from assistant.storage import retention
        retention.prune(conn, settings)
        repo.kv_set(conn, "last_prune", day_key)
    except Exception:  # noqa: BLE001 - retention never breaks the poller
        log.debug("prune failed (non-fatal)", exc_info=True)


def _check_relay_health(conn, settings: Settings, notifier) -> None:
    """Phase 11: if WhatsApp is on, alert ONCE when the relay goes stale/disconnected, and
    once when it recovers. A dead/logged-out relay was previously invisible. Deduped via kv."""
    if not (settings.whatsapp_enabled and getattr(settings, "relay_health_alert_enabled", True)):
        return
    try:
        import json as _json
        from pathlib import Path as _Path

        healthy = False
        p = _Path(settings.relay_status_path)
        if p.exists():
            st = _json.loads(p.read_text(encoding="utf-8"))
            age = time.time() - float(st.get("updated_at") or 0)
            healthy = bool(st.get("connected")) and age <= settings.relay_stale_alert_seconds
        already = repo.kv_get_bool(conn, "relay_unhealthy_alerted", False)
        if not healthy and not already:
            _safe_notify(notifier, "⚠️ WhatsApp relay looks down (disconnected or stale). "
                                   "Incoming WhatsApp may be paused until it reconnects.")
            repo.kv_set_bool(conn, "relay_unhealthy_alerted", True)
        elif healthy and already:
            _safe_notify(notifier, "✅ WhatsApp relay reconnected.")
            repo.kv_set_bool(conn, "relay_unhealthy_alerted", False)
    except Exception:  # noqa: BLE001
        log.debug("relay health check failed (non-fatal)", exc_info=True)


def _reap_stuck_sends(conn, settings: Settings, notifier) -> None:
    """Phase 11: flag pending actions wedged in SENDING (crash mid-send) so they're not
    silently lost. Moves them to the terminal SEND_STUCK state (NEVER re-sent → cannot
    double-send) and alerts the human to verify. Best-effort."""
    try:
        cutoff = int(time.time()) - int(getattr(settings, "stuck_send_minutes", 30)) * 60
        stuck = repo.stuck_sending(conn, cutoff)
        flagged = [r for r in stuck if repo.mark_send_stuck(conn, r["id"])]
        if flagged:
            lines = "; ".join(f"#{r['id']} {(r['summary'] or '')[:40]}" for r in flagged[:5])
            _safe_notify(notifier, f"⚠️ {len(flagged)} reply(ies) may not have completed sending "
                                   f"— please verify: {lines}")
    except Exception:  # noqa: BLE001
        log.debug("stuck-send reaper failed (non-fatal)", exc_info=True)


def _write_heartbeat(conn, settings: Settings) -> None:
    """Write a tiny status file for the Mac menu-bar app (and any external watcher).
    Best-effort — never raises into the poller."""
    try:
        import json as _json
        from pathlib import Path as _Path

        status = {
            "mode": settings.mode,
            "dry_run": settings.dry_run,
            "paused": repo.is_paused(conn),
            "email_enabled": bool(settings.email_enabled),
            "whatsapp_enabled": bool(settings.whatsapp_enabled),
            "pending": len(repo.open_pending(conn)),
            "last_24h": ledger.counts_since(conn, repo.now_epoch() - 86400),
            "heartbeat_ts": repo.now_epoch(),
        }
        p = _Path(settings.db_path).parent / "status.json"
        p.write_text(_json.dumps(status), encoding="utf-8")
    except Exception:  # noqa: BLE001 - heartbeat is best-effort
        pass


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp message-lifecycle "silence sweep" — "not going" / "not coming back".
# ─────────────────────────────────────────────────────────────────────────────
def _short_jid(jid: str) -> str:
    """A readable fallback name from a JID (the phone-number local-part)."""
    s = (jid or "").split("@", 1)[0].strip()
    return s or "a contact"


def _silence_seen(conn, bucket: str, jid: str, message_id: str) -> bool:
    """Per-(bucket, jid) dedup: True if we have ALREADY surfaced this exact situation, so a
    standing silence is announced once — not every sweep. Re-fires only when the anchoring
    message changes (a new latest message), which is the situation genuinely changing."""
    key = f"wa_silence:{bucket}:{jid}"
    if repo.kv_get(conn, key) == (message_id or ""):
        return True
    repo.kv_set(conn, key, message_id or "")
    return False


def maybe_surface_wa_silence(conn, settings: Settings, notifier) -> None:
    """Surface 'not going' (stuck undelivered) and 'not coming back' (delivered/read but
    unanswered, either direction) WhatsApp situations as INFORMATIONAL FYIs.

    Read-only + pause-gated: emits ONLY repo.record_event + notifier.send_text — never a
    card, never a send, so NO_AUTO_SEND holds. 1:1 only (groups are context-only). Throttled,
    quiet-hours-aware, mute-aware, and deduped per (jid, bucket). Never raises into the poller.
    """
    if not getattr(settings, "whatsapp_silence_sweep_enabled", True):
        return
    # A paused agent emits nothing. This job runs in the poller loop OUTSIDE poll_and_process
    # (whose is_paused gate doesn't cover it), so it must self-gate.
    if repo.is_paused(conn):
        return
    now = int(time.time())
    interval = max(60, int(getattr(settings, "wa_silence_sweep_interval_secs", 1800)))
    try:
        last = int(repo.kv_get(conn, "last_wa_silence_sweep") or 0)
    except (TypeError, ValueError):
        last = 0
    if now - last < interval:
        return
    repo.kv_set(conn, "last_wa_silence_sweep", str(now))

    # Don't surface unanswered nudges in the dead of night — reuse the read-receipt window.
    if getattr(settings, "read_receipt_quiet_hours_enabled", True):
        try:
            from assistant.ingest.whatsapp_source import _hour_in_quiet_window
            hour = datetime.now(_tz(settings)).hour
            if _hour_in_quiet_window(
                int(hour),
                int(getattr(settings, "read_receipt_quiet_start_hour", 22)),
                int(getattr(settings, "read_receipt_quiet_end_hour", 8)),
            ):
                return
        except Exception:  # noqa: BLE001 - never let quiet-hours logic break the sweep
            pass

    mute = {j.lower() for j in (getattr(settings, "mute_jids", []) or [])}
    from assistant.storage import wa_messages

    def _name(row) -> str:
        # Resolve the OTHER party's name from their inbound push_name — NEVER from an
        # owner-authored row's push_name (that's the owner's own name, e.g. "Jatin").
        try:
            jid = row["jid"]
            return wa_messages.contact_name(conn, jid) or _short_jid(jid)
        except Exception:  # noqa: BLE001
            return "a contact"

    # One FYI per chat per pass: a chat can match more than one bucket (e.g. an old stuck send
    # plus a newer inbound) — surface the highest-priority once rather than two messages at once.
    fired_jids: set[str] = set()
    max_age = int(getattr(settings, "wa_silence_max_age_secs", 345600))
    fired = 0
    try:
        # 1) NOT GOING — your message never reached WhatsApp's servers.
        for r in wa_messages.stuck_outbound(
                conn, stuck_secs=int(getattr(settings, "wa_stuck_secs", 120)),
                now=now, max_age_secs=max_age):
            if (r["jid"].lower() in mute or r["jid"] in fired_jids
                    or _silence_seen(conn, "not_going", r["jid"], r["message_id"])):
                continue
            fired_jids.add(r["jid"])
            notifier.send_text(
                f"⚠️ Your WhatsApp message to {_name(r)} hasn't been delivered "
                f"(their phone may be off or unreachable).")
            repo.record_event(conn, type="wa_not_going",
                              detail={"jid": r["jid"], "message_id": r["message_id"]})
            fired += 1

        # 2) AGENT SEND STUCK — a reply Steward sent on your behalf may not have delivered.
        # Safety net for the is_agent=1 path (the SEND_STUCK reaper only watches SENDING and
        # the owner-send sweep excludes is_agent), so an approved reply that never left the
        # device is surfaced rather than silently assumed delivered.
        for r in wa_messages.stuck_agent_send(
                conn, stuck_secs=int(getattr(settings, "wa_stuck_secs", 120)),
                now=now, max_age_secs=max_age):
            if (r["jid"].lower() in mute or r["jid"] in fired_jids
                    or _silence_seen(conn, "agent_stuck", r["jid"], r["message_id"])):
                continue
            fired_jids.add(r["jid"])
            notifier.send_text(
                f"⚠️ The reply I sent to {_name(r)} on your behalf may not have been "
                f"delivered — you may want to check.")
            repo.record_event(conn, type="wa_agent_send_stuck",
                              detail={"jid": r["jid"], "message_id": r["message_id"]})
            fired += 1

        # 3) NOT COMING BACK — they delivered/read it but haven't replied to YOU.
        for r in wa_messages.unanswered_outbound(
                conn, min_age_secs=int(getattr(settings, "wa_unanswered_out_secs", 21600)),
                now=now, max_age_secs=max_age):
            if (r["jid"].lower() in mute or r["jid"] in fired_jids
                    or _silence_seen(conn, "unanswered_out", r["jid"], r["message_id"])):
                continue
            fired_jids.add(r["jid"])
            seen = "read it" if r["read_at"] else "received it"
            notifier.send_text(
                f"⏳ {_name(r)} {seen} but hasn't replied to your last message yet.")
            repo.record_event(conn, type="wa_unanswered",
                              detail={"jid": r["jid"], "dir": "out", "message_id": r["message_id"]})
            fired += 1

        # 4) NOT COMING BACK — THEY are waiting on you.
        for r in wa_messages.unanswered_inbound(
                conn, min_age_secs=int(getattr(settings, "wa_unanswered_in_secs", 10800)),
                now=now, max_age_secs=max_age):
            if (r["jid"].lower() in mute or r["jid"] in fired_jids
                    or _silence_seen(conn, "unanswered_in", r["jid"], r["message_id"])):
                continue
            fired_jids.add(r["jid"])
            notifier.send_text(f"💬 You haven't replied to {_name(r)} yet.")
            repo.record_event(conn, type="wa_unanswered",
                              detail={"jid": r["jid"], "dir": "in", "message_id": r["message_id"]})
            fired += 1
    except Exception:  # noqa: BLE001 - the sweep is additive; never break the poller
        log.debug("wa silence sweep error (non-fatal)", exc_info=True)
        return
    if fired:
        log.info("wa silence sweep surfaced %d FYI(s)", fired)


def _whatsapp_poller_loop(settings: Settings, llm, notifier) -> None:
    """Background thread for the WhatsApp channel (gated on WHATSAPP_ENABLED). Its
    own DB connection + WhatsAppSource (which also starts the relay receiver). Claims
    only wa_* ids from the shared ledger."""
    if not settings.whatsapp_enabled:
        return
    conn = db.open_db(settings.db_path)
    ledger.recover_stale(conn)
    try:
        from assistant.ingest.whatsapp_source import WhatsAppSource

        source = WhatsAppSource(conn, settings, llm)
        source.connect()  # starts the localhost receiver for the relay
    except Exception:  # noqa: BLE001
        log.exception("WhatsApp source could not start; thread exiting")
        _safe_notify(notifier, "⚠️ WhatsApp relay receiver failed to start — WhatsApp is off.")
        conn.close()
        return

    log.info("WhatsApp poller started (interval %ss)", settings.poll_interval_seconds)
    while not _stop.is_set():
        try:
            poll_and_process(conn, settings, source, llm, notifier, owns=_is_wa, do_redeliver=False)
            # Message-lifecycle: surface "not going" / "not coming back" as FYIs (pure context).
            maybe_surface_wa_silence(conn, settings, notifier)
        except Exception:  # noqa: BLE001
            log.exception("whatsapp poller iteration error")
        # Interruptible wait: a manual "fetch now" (_wa_wake) or shutdown returns at once.
        _wa_wake.wait(timeout=settings.poll_interval_seconds)
        _wa_wake.clear()
    conn.close()
    log.info("WhatsApp poller stopped")


def _acquire_singleton_lock(settings: Settings):
    """Prevent a SECOND engine from starting (two Telegram pollers → 409 'bot goes dead').
    Writes a PID lock beside the DB. Returns a path str if we acquired it, None if another
    LIVE engine already owns it (caller should refuse to start), or "" if lock IO failed
    (fail-open: proceed without a lock). A stale lock held by a dead PID is reclaimed."""
    import os
    from pathlib import Path
    try:
        lock_path = Path(settings.db_path).parent / "engine.pid"
        if lock_path.exists():
            try:
                other = int((lock_path.read_text(encoding="utf-8").strip() or "0"))
            except (ValueError, OSError):
                other = 0
            if other and other != os.getpid():
                try:
                    os.kill(other, 0)   # signal 0 = liveness probe, sends nothing
                    return None          # another live engine owns the lock
                except OSError:
                    pass                 # stale (dead PID) → reclaim below
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
        return str(lock_path)
    except Exception:  # noqa: BLE001 - never let lock IO block startup
        return ""


def _release_singleton_lock(lock_path) -> None:
    import os
    from pathlib import Path
    try:
        if lock_path and Path(lock_path).exists():
            if int(Path(lock_path).read_text(encoding="utf-8").strip() or "0") == os.getpid():
                Path(lock_path).unlink()
    except Exception:  # noqa: BLE001
        pass


def _poller_watchdog(threads: dict, notifier) -> None:
    """Alert ONCE if a background poller thread dies unexpectedly — otherwise mail/WhatsApp
    processing would stop silently while the bot keeps answering."""
    alerted: set = set()
    while not _stop.is_set():
        for name, th in threads.items():
            if th is not None and not th.is_alive() and name not in alerted:
                alerted.add(name)
                _safe_notify(notifier, f"⚠️ Steward's {name} stopped unexpectedly — that "
                                       f"channel's processing has halted. Please restart Steward.")
        _stop.wait(30)


def run_full(settings: Settings, *, onboard: bool = False) -> int:
    from assistant.control import telegram_bot
    from assistant.control.notifier import Notifier
    from assistant.llm.client import LLMClient

    conn = db.open_db(settings.db_path)  # main-thread connection (for the bot)
    ledger.recover_stale(conn)
    llm = LLMClient(settings, metrics_sink=metrics.make_sink(settings.db_path))
    notifier = Notifier(settings)

    # Single-instance guard: a second engine would start a second Telegram poller and
    # Telegram would 409 one of them dead (the "Steward turns off" glitch). Refuse to
    # start a duplicate; the existing one keeps running.
    _lock = _acquire_singleton_lock(settings)
    if _lock is None:
        msg = ("Another Steward engine is already running (data/engine.pid) — not "
               "starting a second (it would 409-kill the Telegram bot).")
        log.error(msg)
        print(msg)
        _safe_notify(notifier, "⚠️ Tried to start a second Steward, but one is already "
                               "running. Keeping the existing one.")
        conn.close()
        return 1

    if onboard:
        from assistant.onboarding import bootstrap
        try:
            mail = _build_mail(conn, settings)
            summary = bootstrap.run_onboarding(conn, settings, mail, llm, notifier)
            log.info("onboarding: %s", summary)
            notifier.send_text(f"👋 Onboarding done.\n{summary}")
        except Exception:  # noqa: BLE001
            log.exception("onboarding failed (continuing anyway)")

    # Start the poller thread(s), then run the Telegram bot on the main thread.
    t = threading.Thread(target=_poller_loop, args=(settings, llm, notifier), daemon=True)
    t.start()
    wa_thread = None
    if settings.whatsapp_enabled:
        wa_thread = threading.Thread(
            target=_whatsapp_poller_loop, args=(settings, llm, notifier), daemon=True
        )
        wa_thread.start()

    # Watchdog: alert if a poller thread dies, so mail/WhatsApp never halts silently.
    threading.Thread(
        target=_poller_watchdog,
        args=({"email poller": t, "WhatsApp poller": wa_thread}, notifier),
        daemon=True, name="watchdog",
    ).start()

    # The control surface (Telegram/approve/undo) acts on items from BOTH channels,
    # so it gets a MailRouter that routes each action to its channel by message id.
    try:
        from assistant.ingest.router import MailRouter

        sources: dict = {}
        if settings.email_enabled:
            try:
                sources["gmail"] = _build_mail(conn, settings)
            except Exception:  # noqa: BLE001 - control surface still works for WhatsApp
                log.warning("control-surface Gmail unavailable (email off / no creds)")
        if settings.whatsapp_enabled:
            from assistant.ingest.whatsapp_source import WhatsAppSource

            # Control-side WhatsApp source: used only for send/get_thread/mark-read on
            # this thread's connection. Do NOT call connect() — the receiver is owned
            # by the WhatsApp poller thread.
            sources["whatsapp"] = WhatsAppSource(conn, settings, llm)
        mail_main = MailRouter(sources)
    except Exception:  # noqa: BLE001
        log.exception("could not connect mail for the control surface")
        mail_main = None

    channels = " + ".join(
        c for c, on in (("Gmail", settings.email_enabled),
                        ("WhatsApp", settings.whatsapp_enabled)) if on
    ) or "no channels"
    notifier.send_text(
        f"🟢 Assistant online — {channels} — mode: {settings.mode.upper()}"
        + ("  (dry-run: nothing will be changed or sent)" if settings.dry_run else "")
    )
    try:
        telegram_bot.run_bot(conn, settings, llm, mail_main, notifier)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - the engine is going down; tell Jatin first
        log.exception("engine crashed in the bot loop")
        _safe_notify(notifier, f"🔴 Steward crashed and is stopping: {exc}. Please restart it.")
        raise
    finally:
        _stop.set()
        _wake.set()      # unblock the Gmail poller's interruptible wait immediately
        _wa_wake.set()   # and the WhatsApp poller's
        t.join(timeout=5)
        if wa_thread is not None:
            wa_thread.join(timeout=5)
        _release_singleton_lock(_lock)
        conn.close()
    return 0


def run_once(settings: Settings) -> int:
    from assistant.control.notifier import Notifier
    from assistant.llm.client import LLMClient

    conn = db.open_db(settings.db_path)
    ledger.recover_stale(conn)
    llm = LLMClient(settings, metrics_sink=metrics.make_sink(settings.db_path))
    notifier = Notifier(settings)
    mail = _build_mail(conn, settings)
    poll_and_process(conn, settings, mail, llm, notifier)
    maybe_send_briefs(conn, settings, llm, notifier)
    conn.close()
    return 0


def print_status(settings: Settings) -> int:
    conn = db.open_db(settings.db_path)
    paused = repo.is_paused(conn)
    pending = repo.open_pending(conn)
    hist = repo.get_last_history_id(conn)
    counts = ledger.counts_since(conn, repo.now_epoch() - 86400)
    print(f"mode:          {settings.mode}  (dry_run={settings.dry_run})")
    print(f"paused:        {paused}")
    print(f"gmail history: {hist or '(not yet synced)'}")
    print(f"last 24h:      {counts}")
    print(f"pending items: {len(pending)}")
    for row in pending[:20]:
        print(f"  [{row['id']}] tier {row['tier']} {row['kind']}: {row['summary']}")
    conn.close()
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local-first email chief of staff (Phase 1: Gmail)")
    parser.add_argument("--onboard", action="store_true", help="run onboarding, then start")
    parser.add_argument("--once", action="store_true", help="one poll/process pass, then exit")
    parser.add_argument("--status", action="store_true", help="print status and exit")
    parser.add_argument("--replay", metavar="MESSAGE_ID",
                        help="reconstruct the full reasoning path for a decision, then exit")
    parser.add_argument("--doctor", action="store_true",
                        help="print a health check, then exit")
    parser.add_argument("--export-diagnostics", nargs="?", const="", metavar="OUT",
                        help="write a share-safe diagnostics bundle, then exit")
    args = parser.parse_args(argv)

    settings = load_settings()
    settings.ensure_dirs()
    setup_logging(settings.log_path)

    if args.status:
        return print_status(settings)

    if args.doctor:
        from assistant import diagnostics
        conn = db.open_db(settings.db_path)
        print(diagnostics.format_health(diagnostics.health_check(conn, settings)))
        conn.close()
        return 0

    if args.export_diagnostics is not None:
        from assistant import diagnostics
        conn = db.open_db(settings.db_path)
        path = diagnostics.export(conn, settings, out_path=(args.export_diagnostics or None))
        print(path)
        conn.close()
        return 0

    if args.replay:
        from assistant.storage import replay
        conn = db.open_db(settings.db_path)
        print(replay.render(replay.reconstruct(conn, args.replay) or {}))
        conn.close()
        return 0

    missing = settings.missing_required()
    if missing:
        print("Missing required configuration (edit your .env):")
        for m in missing:
            print(f"  - {m}")
        print("\nSee README.md → Setup. (You can still run `--status` and the test suite.)")
        return 1

    log.info("starting assistant in %s mode", settings.mode)
    if args.once:
        return run_once(settings)
    return run_full(settings, onboard=args.onboard)
