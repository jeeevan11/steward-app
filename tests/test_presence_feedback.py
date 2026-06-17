"""Layer 1B (presence suppression), 1E (feedback tuning), 1D (WhatsApp style).

Presence: when the owner is handling a chat himself, the agent goes silent — but ONLY
within the same safety clamps as every other lowering force (never below the guardrail
floor, never for VIP / personal / high-stakes). Feedback: repeated skips quietly lower
how loudly a sender is surfaced. Style: learned from his own sent messages."""

from __future__ import annotations

import time
import unittest

from assistant.brain.tiers import decide
from assistant.config import Settings
from assistant.control import presence
from assistant.models import Reversibility, Stakes, Tier
from assistant.storage import db
from assistant.storage import wa_messages
from tests.helpers import make_contact, make_decision, make_message, make_thread


def _low_reversible(**kw):
    base = dict(stakes=Stakes.MEDIUM, reversibility=Reversibility.REVERSIBLE,
                proposed_tier=Tier.APPROVE, confidence=0.95, needs_reply=True)
    base.update(kw)
    return make_decision(**base)


# ── Presence in the tier engine (suppress_active) ──────────────────────────────
class TestPresenceSuppression(unittest.TestCase):
    def test_active_conversation_quiets_needs_reply_to_fyi_never_silent(self):
        # CARDINAL: a needs-reply message is never silenced by presence — at most a quiet FYI
        # the owner can still see (the live Maya/Sam silencing was exactly this demotion).
        final = decide(make_thread(), _low_reversible(needs_reply=True), make_contact(),
                       suppress_active=True)
        self.assertEqual(final.final_tier, Tier.FYI)
        self.assertTrue(any("handling this conversation" in f for f in final.applied_floors))

    def test_active_conversation_can_still_silence_a_no_reply_item(self):
        # A no-reply-needed low-stakes item MAY be fully silenced when the owner is in the chat.
        final = decide(make_thread(),
                       _low_reversible(needs_reply=False, stakes=Stakes.LOW, proposed_tier=Tier.FYI),
                       make_contact(), suppress_active=True)
        self.assertEqual(final.final_tier, Tier.SILENT)

    def test_presence_cannot_drop_below_guardrail_floor(self):
        # He's in the chat, but it mentions money → still surfaces (guardrail wins).
        thread = make_thread(make_message("can you wire the payment / invoice today"))
        final = decide(thread, _low_reversible(), make_contact(), suppress_active=True)
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))

    def test_presence_never_suppresses_vip(self):
        final = decide(make_thread(), _low_reversible(), make_contact(flags={"vip"}),
                       suppress_active=True)
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))

    def test_presence_high_stakes_not_suppressed(self):
        final = decide(make_thread(), _low_reversible(stakes=Stakes.HIGH),
                       make_contact(), suppress_active=True)
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))


# ── Feedback tuning in the tier engine (deprioritized) ─────────────────────────
class TestFeedbackTuning(unittest.TestCase):
    def test_deprioritized_lowers_to_fyi_not_silent(self):
        final = decide(make_thread(), _low_reversible(needs_reply=True), make_contact(),
                       deprioritized=True)
        self.assertEqual(final.final_tier, Tier.FYI)
        self.assertTrue(any("repeatedly skipped" in f for f in final.applied_floors))

    def test_deprioritized_respects_guardrail_floor(self):
        thread = make_thread(make_message("here is the signed contract / nda"))
        final = decide(thread, _low_reversible(), make_contact(), deprioritized=True)
        self.assertGreaterEqual(int(final.final_tier), int(Tier.APPROVE))


# ── Presence module (per-conversation outbound signal) ─────────────────────────
class TestPresenceModule(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_recent_outbound_means_actively_handling(self):
        s = Settings(presence_app_focus_enabled=False, presence_outbound_cooldown_seconds=300)
        jid = "friend@s.whatsapp.net"
        wa_messages.record(self.conn, {"message_id": "wa_out_1", "jid": jid,
                                       "body": "on it", "ts": int(time.time())}, from_me=True)
        self.assertTrue(presence.is_actively_handling(self.conn, s, jid))

    def test_old_outbound_is_not_active(self):
        s = Settings(presence_app_focus_enabled=False, presence_outbound_cooldown_seconds=300)
        jid = "friend@s.whatsapp.net"
        wa_messages.record(self.conn, {"message_id": "wa_out_1", "jid": jid, "body": "old",
                                       "ts": int(time.time()) - 9999}, from_me=True)
        self.assertFalse(presence.is_actively_handling(self.conn, s, jid))

    def test_disabled_is_never_active(self):
        s = Settings(presence_suppression_enabled=False)
        self.assertFalse(presence.is_actively_handling(self.conn, s, "x@s.whatsapp.net"))


# ── WhatsApp style learning (Layer 1D) ─────────────────────────────────────────
class _StyleLLM:
    def complete_text(self, *, system_prefix="", user_prompt=""):
        return "- short, lowercase\n- no sign-off\n- uses 'haha'"


class TestWaStyle(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_builds_and_serves_style_with_enough_samples(self):
        from assistant.action import wa_style
        s = Settings(whatsapp_style_enabled=True)
        for i in range(10):
            wa_messages.record(self.conn, {"message_id": f"wa_out_{i}", "jid": f"c{i}@s.whatsapp.net",
                                           "body": f"haha sounds good {i}", "ts": int(time.time())},
                               from_me=True)
        prof = wa_style.build_wa_style(self.conn, _StyleLLM(), s)
        self.assertIn("lowercase", prof)
        prefix = wa_style.wa_style_prefix(self.conn, s)
        self.assertIn("HOW I TEXT ON WHATSAPP", prefix)

    def test_not_built_with_too_few_samples(self):
        from assistant.action import wa_style
        s = Settings(whatsapp_style_enabled=True)
        wa_messages.record(self.conn, {"message_id": "wa_out_1", "jid": "c@s.whatsapp.net",
                                       "body": "hi", "ts": int(time.time())}, from_me=True)
        self.assertEqual(wa_style.build_wa_style(self.conn, _StyleLLM(), s), "")
        self.assertEqual(wa_style.wa_style_prefix(self.conn, s), "")


if __name__ == "__main__":
    unittest.main()
