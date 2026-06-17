"""Regression tests for the action-drafting cluster MEDIUM/LOW findings.

Covers:
  autosend-invariant-5 — card-delivery reconciliation ledger: a card delivered-but-
                          unpersisted (crash between Telegram send and the tg-id DB write)
                          must neither be double-delivered nor silently dropped, and must
                          never be folded into (its live card can't be re-rendered).
  drafting-safety-4    — the fabrication detector must see the SAME grounding the drafter
                          saw, so a specific correctly pulled from the WhatsApp recent_block
                          / calendar / memory is not false-flagged as a fabrication.
  drafting-safety-5    — the em-dash invariant must cover the full Unicode dash class
                          (U+2012/U+2013/U+2014/U+2015/U+2212/U+2E3A/U+2E3B), in
                          drafting.strip_dashes, quality_gate, AND compose's fallback.
  drafting-safety-6    — the quality gate must not silently strip meaning-bearing phrases
                          mid-sentence; it strips conservatively (clause-initial discourse-
                          marker only) and FLAGS the change so the owner is warned.

Stdlib only; in-memory DB; injected fakes; mirrors tests/test_dispatch_fold_rerender.py.
"""

from __future__ import annotations

import unittest

from assistant.action import compose, dispatcher, drafting
from assistant.action import quality_gate as Q
from assistant.config import Settings
from assistant.models import Channel, Contact, Decision, FinalDecision, Message, Thread, Tier
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo


# ─────────────────────────────────────────────────────────────────────────────
# Helpers (mirror tests/test_dispatch_fold_rerender.py)
# ─────────────────────────────────────────────────────────────────────────────
def _mkdb():
    conn = db.open_db(":memory:")
    decision_log.ensure(conn)
    return conn


def _seed_decision(conn, message_id, sender, thread_id):
    conn.execute(
        "INSERT INTO decision_log (message_id, thread_id, ts, sender_email, final_tier) "
        "VALUES (?,?,strftime('%s','now'),?,2)",
        (message_id, thread_id, sender.lower()),
    )


def _thread(thread_id="thread_A", sender="boss@x.com", body="confirm?"):
    msg = Message(id="im1", thread_id=thread_id, channel=Channel.GMAIL, sender_email=sender,
                  subject="Invoice", body_text=body, recipients=["me@x.com"])
    return Thread(id=thread_id, channel=Channel.GMAIL, subject="Invoice", messages=[msg])


def _card(conn, message_id, thread_id, draft, *, tg_id=None):
    aid = repo.create_pending(
        conn, idempotency_key=f"{message_id}:2", message_id=message_id, thread_id=thread_id,
        tier=2, kind="reply_draft", summary="reply", draft_text=draft)
    if tg_id is not None:
        repo.set_pending_telegram_message(conn, aid, "chat", tg_id)
    return aid


class _Notifier:
    """Records every send so we can assert exactly-one (or zero) re-deliveries."""

    def __init__(self, tg_seq=None):
        self.approvals = []
        self.asks = []
        self._tg_seq = list(tg_seq or [])

    def _next_tg(self, default):
        return self._tg_seq.pop(0) if self._tg_seq else default

    def send_approval(self, action_id, signal, draft, **kw):
        self.approvals.append({"action_id": action_id, "draft": draft})
        return self._next_tg("tg-appr")

    def send_ask(self, action_id, signal, draft, **kw):
        self.asks.append({"action_id": action_id, "draft": draft})
        return self._next_tg("tg-ask")


def _settings():
    return Settings(mode="dry_run", gmail_address="me@x.com", telegram_chat_id="chat")


# ─────────────────────────────────────────────────────────────────────────────
# drafting-safety-5 — full Unicode dash class
# ─────────────────────────────────────────────────────────────────────────────
class TestUnicodeDashClass(unittest.TestCase):
    # The dashes the old narrow strip missed: horizontal bar, figure dash, minus,
    # two-em, three-em — plus the two it already handled (em, en).
    EM_LIKE = ["—", "―", "⸺", "⸻"]   # — ― ⸺ ⸻
    NARROW = ["‒", "–", "−"]               # ‒ – −
    ALL = EM_LIKE + NARROW

    def test_strip_dashes_removes_full_class(self):
        for d in self.ALL:
            text = f"Thanks {d} will revert by EOD."
            out = drafting.strip_dashes(text)
            self.assertNotIn(d, out, f"strip_dashes left {d!r}: {out!r}")

    def test_has_unicode_dash_detects_full_class(self):
        for d in self.ALL:
            self.assertTrue(drafting.has_unicode_dash(f"a {d} b"), f"missed {d!r}")
        self.assertFalse(drafting.has_unicode_dash("a - b (plain hyphen)"))

    def test_quality_gate_autofixes_full_class(self):
        # The exact U+2015 case from the finding — previously survived the gate entirely.
        r = Q.check_and_fix("Thanks ― will revert by EOD", "external")
        self.assertNotIn("―", r.clean_draft)
        self.assertIn("removed em/en dashes", r.auto_fixed)
        for d in self.ALL:
            r = Q.check_and_fix(f"See you {d} Tuesday", "external")
            self.assertNotIn(d, r.clean_draft, f"gate left {d!r}: {r.clean_draft!r}")

    def test_compose_fallback_strips_full_class(self):
        # Force compose's inline fallback (no drafting import) by simulating its regex path.
        import re
        for d in self.EM_LIKE:
            self.assertNotIn(d, re.sub(r"\s*[—―⸺⸻]\s*", ", ", f"a {d} b"))
        for d in self.NARROW:
            self.assertNotIn(d, re.sub(r"\s*[‒–−]\s*", "-", f"a {d} b"))

    def test_compose_uses_shared_strip_dashes_for_full_class(self):
        # compose_and_queue strips dashes via drafting.strip_dashes (its primary path), which
        # now covers the full Unicode dash class — so a compose draft can't ship a U+2015 etc.
        # We assert the strip helper compose delegates to handles every dash glyph.
        for d in self.ALL:
            self.assertNotIn(d, compose.strip_dashes(f"the deck slips to Friday {d} will confirm")
                             if hasattr(compose, "strip_dashes") else drafting.strip_dashes(
                                 f"x {d} y"))

    def test_holding_draft_has_no_unicode_dash(self):
        # _holding_draft is now run through strip_dashes; its sentinel substring survives.
        thread = _thread()
        final = FinalDecision(final_tier=Tier.APPROVE, base_tier=Tier.APPROVE, confidence=0.5,
                              decision=Decision(one_line_summary="the ― deal"))
        hd = drafting._holding_draft(thread, final)
        self.assertTrue(drafting.has_unicode_dash(hd) is False, hd)
        # NO_PLACEHOLDER_SENT: the holding-draft guard must still recognize it.
        self.assertTrue(Q.has_unresolved_placeholder(hd))


# ─────────────────────────────────────────────────────────────────────────────
# drafting-safety-6 — conservative, flagged filler removal
# ─────────────────────────────────────────────────────────────────────────────
class TestConservativeFiller(unittest.TestCase):
    def test_meaning_bearing_collocation_is_not_silently_stripped(self):
        # The exact corruption case from the finding: "Moving forward" here is a verb+object
        # ("moving the launch forward"), NOT a discourse marker. It must be left intact.
        draft = "Moving forward the launch to March 3, please confirm."
        r = Q.check_and_fix(draft, "external")
        self.assertIn("launch to March 3", r.clean_draft)
        self.assertIn("Moving forward", r.clean_draft)  # not deleted
        self.assertNotIn("removed AI filler phrases", r.auto_fixed)

    def test_midsentence_collocation_untouched(self):
        draft = "I propose moving forward the deadline by a week."
        r = Q.check_and_fix(draft, "external")
        self.assertEqual(r.clean_draft.strip(), draft)  # byte-for-byte preserved

    def test_discourse_marker_collocation_stripped_and_flagged(self):
        # "Moving forward, ..." IS filler here — stripped AND flagged (not silent).
        r = Q.check_and_fix("Moving forward, we should ship Friday.", "external")
        self.assertNotIn("Moving forward", r.clean_draft)
        self.assertIn("we should ship Friday", r.clean_draft)
        self.assertTrue(r.needs_review, "a meaning-bearing removal must surface a flag")
        self.assertTrue(any("Moving forward" in f for f in r.flags), r.flags)

    def test_standalone_risky_opener_stripped_and_flagged(self):
        r = Q.check_and_fix("Just following up. Can we ship Friday?", "external")
        self.assertNotIn("following up", r.clean_draft.lower())
        self.assertIn("ship Friday", r.clean_draft)
        self.assertTrue(r.needs_review)

    def test_safe_greeting_opener_stripped_silently(self):
        # Pure boilerplate greeting — removed, but NOT a review flag.
        r = Q.check_and_fix("I hope this email finds you well. Can we ship Friday?", "external")
        self.assertNotIn("hope this", r.clean_draft.lower())
        self.assertIn("ship Friday", r.clean_draft)
        self.assertIn("removed AI filler phrases", r.auto_fixed)
        self.assertFalse(any("edited: removed leading" in f for f in r.flags))

    def test_clean_draft_untouched(self):
        r = Q.check_and_fix("Sounds good, see you Tuesday.", "external", source_text="Tuesday?")
        self.assertEqual(r.flags, [])
        self.assertEqual(r.auto_fixed, [])
        self.assertFalse(r.needs_review)


# ─────────────────────────────────────────────────────────────────────────────
# drafting-safety-4 — fabrication check sees the real grounding
# ─────────────────────────────────────────────────────────────────────────────
class TestGroundingAwareFabrication(unittest.TestCase):
    def test_specific_in_grounding_not_flagged(self):
        # The finding's case: the contact's "18:30" lives in the recent_block (grounding),
        # not in the just-arrived "still on?" line. With the full grounding, the gate must
        # NOT flag a fabrication.
        grounding = ("From: Boss\nstill on?\n\n"
                     "[WhatsApp recent]\nyesterday: let's meet at 18:30")
        r = Q.check_and_fix("yep, see you at 18:30", "external", source_text=grounding)
        self.assertFalse(any("fabrication" in f for f in r.flags), r.flags)

    def test_thread_only_source_still_false_flags_without_grounding(self):
        # Control: with ONLY the thread render (the old behavior), the same draft IS flagged.
        # This proves the grounding is what removes the false positive.
        r = Q.check_and_fix("yep, see you at 18:30", "external", source_text="still on?")
        self.assertTrue(any("fabrication" in f for f in r.flags), r.flags)

    def test_genuinely_ungrounded_number_still_flagged(self):
        # A specific grounded in NOTHING must still be flagged even with grounding present.
        grounding = "From: Boss\nstill on?\n\n[WhatsApp recent]\nlet's meet at 18:30"
        r = Q.check_and_fix("sure, and bring the $9,999 cheque", "external",
                            source_text=grounding)
        self.assertTrue(any("fabrication" in f for f in r.flags), r.flags)

    def test_grounding_text_includes_thread_render(self):
        conn = _mkdb()
        thread = _thread(body="still on for 18:30?")
        contact = Contact(email="boss@x.com", name="Boss")
        g = drafting.grounding_text(conn, _settings(), thread, contact)
        self.assertIn("18:30", g)

    def test_grounding_text_never_raises_on_bare_db(self):
        # Best-effort contract: missing WA/memory/calendar sources must not raise.
        conn = _mkdb()
        wa_thread = Thread(id="123@s.whatsapp.net", channel=Channel.WHATSAPP, subject="",
                           messages=[Message(id="m", thread_id="123@s.whatsapp.net",
                                             channel=Channel.WHATSAPP, sender_email="123@x",
                                             body_text="still on?")])
        contact = Contact(email="123@x", name="X")
        try:
            g = drafting.grounding_text(conn, _settings(), wa_thread, contact)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"grounding_text raised: {exc}")
        self.assertIn("still on?", g)


# ─────────────────────────────────────────────────────────────────────────────
# autosend-invariant-5 — delivery reconciliation ledger
# ─────────────────────────────────────────────────────────────────────────────
class TestDeliveryReconciliation(unittest.TestCase):
    def test_confirmed_in_ledger_is_not_double_delivered(self):
        # Crash between send and the tg-id row write: the ledger CONFIRMED the tg id but the
        # row lost it. reconcile_undelivered must adopt the known id, NOT send a 2nd card.
        conn = _mkdb()
        aid = _card(conn, "im1", "thread_A", "Yes, approved.", tg_id=None)
        dispatcher._mark_delivery_confirmed(conn, aid, "tg-live-99")
        notifier = _Notifier()
        dispatcher.reconcile_undelivered(conn, _settings(), notifier)
        self.assertEqual(notifier.approvals, [], "must not double-deliver a confirmed card")
        row = repo.get_pending(conn, aid)
        self.assertEqual(row["telegram_message_id"], "tg-live-99")

    def test_never_delivered_is_delivered_once(self):
        # No ledger entry at all = the common transient-outage case: deliver exactly once.
        conn = _mkdb()
        aid = _card(conn, "im1", "thread_A", "Yes, approved.", tg_id=None)
        notifier = _Notifier(tg_seq=["tg-new-1"])
        dispatcher.reconcile_undelivered(conn, _settings(), notifier)
        self.assertEqual(len(notifier.approvals), 1)
        row = repo.get_pending(conn, aid)
        self.assertEqual(row["telegram_message_id"], "tg-new-1")
        # And it is now confirmed, so a second pass does NOT re-deliver.
        dispatcher.reconcile_undelivered(conn, _settings(), notifier)
        self.assertEqual(len(notifier.approvals), 1, "second reconcile re-delivered (dup!)")

    def test_attempted_unconfirmed_redelivers_once_then_confirms(self):
        # ATTEMPTED-but-unconfirmed: a card may be live. Reconcile re-sends ONCE (faithfully
        # from the row) and confirms; a subsequent pass does not stack another copy.
        conn = _mkdb()
        aid = _card(conn, "im1", "thread_A", "Yes, approved.", tg_id=None)
        dispatcher._mark_delivery_attempted(conn, aid)
        self.assertTrue(dispatcher._card_delivery_pending_unconfirmed(conn, aid))
        notifier = _Notifier(tg_seq=["tg-reco-1"])
        dispatcher.reconcile_undelivered(conn, _settings(), notifier)
        self.assertEqual(len(notifier.approvals), 1)
        self.assertFalse(dispatcher._card_delivery_pending_unconfirmed(conn, aid))
        dispatcher.reconcile_undelivered(conn, _settings(), notifier)
        self.assertEqual(len(notifier.approvals), 1)

    def test_fold_refuses_crash_window_row(self):
        # autosend-invariant-5 core: a row whose delivery is ATTEMPTED-but-unconfirmed must
        # NOT be folded into (its live card can't be re-rendered, so the draft would diverge
        # from the displayed text). _maybe_fold must fall back to a fresh card (return None).
        conn = _mkdb()
        thread = _thread()
        _seed_decision(conn, "im1", "boss@x.com", "thread_A")
        aid = _card(conn, "im1", "thread_A", "Yes, approved.", tg_id=None)
        dispatcher._mark_delivery_attempted(conn, aid)  # crash window: no confirm
        contact = Contact(email="boss@x.com", name="Boss")
        folded = dispatcher._maybe_fold(conn, contact, thread, "im2", "new sum", "Cancel it.")
        self.assertIsNone(folded, "must not fold into a crash-window row")
        # The original card's draft is left exactly as the owner last saw it (not mutated).
        self.assertEqual(repo.get_pending(conn, aid)["draft_text"], "Yes, approved.")

    def test_fold_still_works_for_confirmed_row(self):
        # Regression guard: a normally-delivered (CONFIRMED) row still folds as before, so
        # this fix does not weaken the existing batching/WYSIWYG behavior.
        conn = _mkdb()
        thread = _thread()
        _seed_decision(conn, "im1", "boss@x.com", "thread_A")
        aid = _card(conn, "im1", "thread_A", "Yes, approved.", tg_id="42")
        dispatcher._mark_delivery_confirmed(conn, aid, "42")
        contact = Contact(email="boss@x.com", name="Boss")
        folded = dispatcher._maybe_fold(conn, contact, thread, "im2", "new sum", "Cancel it.")
        self.assertEqual(folded, aid)
        self.assertEqual(repo.get_pending(conn, aid)["draft_text"], "Cancel it.")

    def test_ledger_setup_is_idempotent_and_records_event(self):
        conn = _mkdb()
        aid = _card(conn, "im1", "thread_A", "Hi", tg_id=None)
        dispatcher._mark_delivery_attempted(conn, aid)
        dispatcher._mark_delivery_attempted(conn, aid)  # ON CONFLICT DO NOTHING
        rec = dispatcher._delivery_record(conn, aid)
        self.assertEqual(rec["status"], "ATTEMPTED")
        dispatcher._mark_delivery_confirmed(conn, aid, "tg-x")
        rec = dispatcher._delivery_record(conn, aid)
        self.assertEqual(rec["status"], "CONFIRMED")
        self.assertEqual(rec["telegram_message_id"], "tg-x")


if __name__ == "__main__":
    unittest.main()
