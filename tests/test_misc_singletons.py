"""Regression tests for the misc-singletons cluster (MEDIUM/LOW findings).

Covers, additively and stdlib-only:
  * ingest-whatsapp-7   — list_new per-jid fairness (one flooding group can no longer
                          starve other conversations out of the settling window).
  * control-state-presence-7 — group presence suppression no longer keys on the bare
                          group JID, so one owner group post can't silence every
                          owner-mentioning group message for the cooldown window.
  * approval-telegram-5 — the pending-edit state is namespaced per action and bound to
                          the card's Telegram message id, so tapping Edit on a second
                          card cannot redirect the owner's text to the wrong action.
  * config-secrets-deploy-2 — launchd KeepAlive is conditional ({Crashed: true}) so a
                          clean config-error exit does not respawn forever.
  * classifier-brain-5  — VERIFY-ONLY: the confident-spam early return already applies
                          the investor-firm-domain / legal-attachment structural floors
                          (closed by the sibling classifier-brain-1 fix). Asserted here
                          so a future regression re-opening the bypass is caught.

Stdlib only; open_db(":memory:"); fake/plain-dict rows; no network, no live engine.
"""

from __future__ import annotations

import os
import time
import unittest

from assistant.storage import db
from assistant.storage import whatsapp_inbox as inbox


# ─────────────────────────────────────────────────────────────────────────────
# ingest-whatsapp-7 — list_new per-jid fairness
# ─────────────────────────────────────────────────────────────────────────────
class TestListNewFairness(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        inbox.ensure(self.conn)
        # Make the per-jid cap deterministic for the assertions below.
        self._prev_cap = os.environ.get("WHATSAPP_INBOX_PER_JID_CAP")
        os.environ["WHATSAPP_INBOX_PER_JID_CAP"] = "5"

    def tearDown(self):
        if self._prev_cap is None:
            os.environ.pop("WHATSAPP_INBOX_PER_JID_CAP", None)
        else:
            os.environ["WHATSAPP_INBOX_PER_JID_CAP"] = self._prev_cap
        self.conn.close()

    def _put(self, mid, jid, created_at, *, is_group=False):
        inbox.put(
            self.conn,
            mid,
            {
                "jid": jid,
                "sender_jid": jid,
                "body": "hi",
                "is_group": is_group,
                "ts": created_at,
            },
        )
        # put() defaults created_at to now; force a deterministic receive clock so the
        # flood is unambiguously *older* than the late 1:1.
        self.conn.execute(
            "UPDATE whatsapp_inbox SET created_at=? WHERE message_id=?", (created_at, mid)
        )

    def test_flood_does_not_starve_other_conversations(self):
        base = 1_000_000
        # A flooding group: 300 owner-mentioning lines, all OLDER than everything else.
        for i in range(300):
            self._put(f"wa_flood_{i}", "flood@g.us", base + i, is_group=True)
        # A genuinely-urgent 1:1 that arrives AFTER the whole flood (highest created_at).
        late_1to1 = base + 10_000
        self._put("wa_vip_1", "vip@s.whatsapp.net", late_1to1)

        rows = inbox.list_new(self.conn, limit=100)
        jids = [r["jid"] for r in rows]

        # The late 1:1 MUST be represented despite 300 older flood rows — the old flat
        # ORDER BY created_at LIMIT 100 would have excluded it entirely.
        self.assertIn("vip@s.whatsapp.net", jids,
                      "late 1:1 was starved out of the planner window by the flood")
        # The flood jid is capped, never monopolising the whole budget.
        flood_count = sum(1 for j in jids if j == "flood@g.us")
        self.assertLessEqual(flood_count, 5,
                             "a single flooding jid exceeded its per-jid cap")

    def test_every_distinct_jid_is_represented(self):
        # 40 distinct 1:1 conversations, one pending row each.
        for i in range(40):
            self._put(f"wa_c_{i}", f"c{i}@s.whatsapp.net", 2_000_000 + i)
        rows = inbox.list_new(self.conn, limit=100)
        seen = {r["jid"] for r in rows}
        self.assertEqual(len(seen), 40, "not every conversation was represented")

    def test_round_robin_serves_one_per_jid_before_deepening(self):
        # Two jids, jid A has many rows, jid B has one. With limit small enough that a
        # flat window would take only A's rows, round-robin must still include B.
        for i in range(10):
            self._put(f"wa_a_{i}", "a@s.whatsapp.net", 3_000_000 + i)
        self._put("wa_b_0", "b@s.whatsapp.net", 3_000_500)  # newer than all of A
        rows = inbox.list_new(self.conn, limit=2)
        jids = {r["jid"] for r in rows}
        self.assertEqual(jids, {"a@s.whatsapp.net", "b@s.whatsapp.net"},
                         "round-robin did not represent both jids within the budget")

    def test_empty_inbox_returns_empty(self):
        self.assertEqual(inbox.list_new(self.conn), [])

    def test_only_new_rows_selected(self):
        self._put("wa_q_0", "x@s.whatsapp.net", 4_000_000)
        inbox.mark_queued(self.conn, "wa_q_0")
        self._put("wa_n_0", "y@s.whatsapp.net", 4_000_001)
        rows = inbox.list_new(self.conn)
        self.assertEqual([r["message_id"] for r in rows], ["wa_n_0"])

    def test_respects_overall_limit(self):
        for i in range(20):
            self._put(f"wa_z_{i}", f"z{i}@s.whatsapp.net", 5_000_000 + i)
        rows = inbox.list_new(self.conn, limit=7)
        self.assertEqual(len(rows), 7)


# ─────────────────────────────────────────────────────────────────────────────
# control-state-presence-7 — group-aware presence suppression
# ─────────────────────────────────────────────────────────────────────────────
class TestGroupPresence(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def _settings(self):
        from assistant.config import Settings
        # App-focus disabled → the only suppression signal available is the outbound
        # shortcut, which must NOT apply to groups.
        return Settings(presence_app_focus_enabled=False,
                        presence_outbound_cooldown_seconds=300)

    def _record_owner_post(self, jid, *, is_group, ts=None):
        from assistant.storage import wa_messages
        wa_messages.record(
            self.conn,
            {"message_id": f"wa_out_{jid}", "jid": jid, "body": "hey",
             "is_group": is_group, "ts": int(ts or time.time())},
            from_me=True,
        )

    def test_owner_group_post_does_not_suppress_group_messages(self):
        from assistant.control import presence
        gjid = "team@g.us"
        self._record_owner_post(gjid, is_group=True)  # just now, within cooldown
        # Old behavior would have returned True (suppress) for the full 5-min window.
        self.assertFalse(
            presence.is_actively_handling(self.conn, self._settings(), gjid),
            "a single owner group post must not silence the whole group",
        )

    def test_one_to_one_outbound_still_suppresses(self):
        from assistant.control import presence
        jid = "friend@s.whatsapp.net"
        self._record_owner_post(jid, is_group=False)  # just now
        self.assertTrue(
            presence.is_actively_handling(self.conn, self._settings(), jid),
            "1:1 presence suppression must be unchanged",
        )

    def test_explicit_is_group_flag_overrides_suffix(self):
        from assistant.control import presence
        # A jid that does NOT look like a group, but the caller knows it is one.
        jid = "weirdjid"
        self._record_owner_post(jid, is_group=True)
        self.assertFalse(
            presence.is_actively_handling(self.conn, self._settings(), jid, is_group=True)
        )
        # Same jid, treated as 1:1 → outbound shortcut applies again.
        self.assertTrue(
            presence.is_actively_handling(self.conn, self._settings(), jid, is_group=False)
        )

    def test_group_jid_detection(self):
        from assistant.control import presence
        self.assertTrue(presence._is_group_jid("x@g.us"))
        self.assertFalse(presence._is_group_jid("x@s.whatsapp.net"))
        self.assertFalse(presence._is_group_jid(""))


# ─────────────────────────────────────────────────────────────────────────────
# approval-telegram-5 — namespaced, card-bound pending-edit routing
#
# The telegram_bot module imports python-telegram-bot at module top, which the test
# harness does not provide. We therefore test the PURE state helpers in isolation by
# loading just those functions from the module source (no telegram import needed).
# ─────────────────────────────────────────────────────────────────────────────
def _load_edit_state_helpers():
    """Exec the pure approval-telegram-5 helpers out of telegram_bot.py without importing
    the whole module (which would require python-telegram-bot). Stdlib only."""
    import ast
    import pathlib

    src = pathlib.Path(
        os.path.join(os.path.dirname(__file__), "..", "assistant", "control", "telegram_bot.py")
    ).read_text()
    tree = ast.parse(src)
    wanted = {
        "_awaiting_map", "_prune_awaiting", "_record_awaiting",
        "_resolve_awaiting", "_consume_awaiting",
    }
    wanted_consts = {"_K_AWAITING", "_K_AWAITING_MAP", "_AWAITING_TTL_SECONDS"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & wanted_consts:
                nodes.append(node)
    module = ast.Module(body=nodes, type_ignores=[])
    ns: dict = {"time": time}
    exec(compile(module, "<telegram_edit_helpers>", "exec"), ns)
    return ns


class TestEditStateRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = _load_edit_state_helpers()

    def test_second_edit_does_not_overwrite_first(self):
        h = self.h
        ud = {}
        m = h["_awaiting_map"](ud)
        # Owner taps Edit on card #5 (msg id 105), then on card #9 (msg id 109).
        other = h["_record_awaiting"](m, 5, card_msg_id=105, label="spouse")
        self.assertIsNone(other)
        other = h["_record_awaiting"](m, 9, card_msg_id=109, label="client")
        self.assertEqual(other, 5, "tapping a second Edit must report the coexisting one")
        # BOTH edits are still pending (the old single-slot design lost #5 here).
        self.assertEqual(set(m), {5, 9})

    def test_reply_to_card_routes_to_correct_action(self):
        h = self.h
        m = {}
        h["_record_awaiting"](m, 5, card_msg_id=105, label="spouse")
        h["_record_awaiting"](m, 9, card_msg_id=109, label="client")
        # The owner types the #5 text as a REPLY to card #5 (msg id 105).
        outcome, aid = h["_resolve_awaiting"](m, reply_to_msg_id=105)
        self.assertEqual((outcome, aid), ("match", 5),
                         "reply-to-card binding must route to the replied card")

    def test_ambiguous_when_multiple_pending_and_no_reply(self):
        h = self.h
        m = {}
        h["_record_awaiting"](m, 5, card_msg_id=105)
        h["_record_awaiting"](m, 9, card_msg_id=109)
        outcome, aid = h["_resolve_awaiting"](m, reply_to_msg_id=None)
        self.assertEqual(outcome, "ambiguous")
        self.assertIsNone(aid, "must NOT guess a target when several edits are pending")

    def test_lone_pending_edit_accepted_without_reply(self):
        h = self.h
        m = {}
        h["_record_awaiting"](m, 7, card_msg_id=107)
        # Common single-card flow: owner just types the new text (no reply binding).
        outcome, aid = h["_resolve_awaiting"](m, reply_to_msg_id=None)
        self.assertEqual((outcome, aid), ("match", 7))

    def test_no_pending_edits_is_none(self):
        h = self.h
        outcome, aid = h["_resolve_awaiting"]({}, reply_to_msg_id=None)
        self.assertEqual((outcome, aid), ("none", None))

    def test_reply_to_unrelated_message_with_lone_edit_still_matches(self):
        h = self.h
        m = {}
        h["_record_awaiting"](m, 3, card_msg_id=103)
        # Owner replies to some OTHER message (not a card we're editing); with exactly
        # one edit pending we still accept it rather than block the only edit in flight.
        outcome, aid = h["_resolve_awaiting"](m, reply_to_msg_id=999)
        self.assertEqual((outcome, aid), ("match", 3))

    def test_stale_edits_expire(self):
        h = self.h
        m = {}
        h["_record_awaiting"](m, 1, card_msg_id=101, now=1000.0)
        # 16 minutes later the abandoned edit must not silently capture new text.
        ttl = h["_AWAITING_TTL_SECONDS"]
        outcome, aid = h["_resolve_awaiting"](m, reply_to_msg_id=None, now=1000.0 + ttl + 1)
        self.assertEqual(outcome, "none")
        self.assertEqual(m, {}, "stale edit was not pruned")

    def test_legacy_single_slot_migrates(self):
        h = self.h
        # An older build left the un-namespaced int slot; it must migrate, not be lost.
        ud = {h["_K_AWAITING"]: 42}
        m = h["_awaiting_map"](ud)
        self.assertIn(42, m)
        self.assertNotIn(h["_K_AWAITING"], ud)

    def test_consume_removes_only_target(self):
        h = self.h
        m = {}
        h["_record_awaiting"](m, 5, card_msg_id=105)
        h["_record_awaiting"](m, 9, card_msg_id=109)
        h["_consume_awaiting"](m, 5)
        self.assertEqual(set(m), {9})


# ─────────────────────────────────────────────────────────────────────────────
# config-secrets-deploy-2 — launchd KeepAlive is conditional, not unconditional
# ─────────────────────────────────────────────────────────────────────────────
class TestLaunchdKeepAlive(unittest.TestCase):
    def _template(self):
        import pathlib
        return pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..", "deploy",
                         "com.cos.assistant.plist.template")
        ).read_text()

    def test_keepalive_is_not_unconditional_true(self):
        import plistlib
        src = self._template()
        # Render the template (placeholders → harmless values) and parse it.
        rendered = src.replace("{{PYTHON}}", "/usr/bin/python3").replace(
            "{{WORKDIR}}", "/tmp/x")
        plist = plistlib.loads(rendered.encode("utf-8"))
        ka = plist.get("KeepAlive")
        self.assertIsInstance(ka, dict,
                              "KeepAlive must be a conditional dict, not an unconditional bool")
        # Only relaunch on a crash; a clean config-error exit() is therefore terminal.
        self.assertEqual(ka.get("Crashed"), True)
        self.assertNotIn("SuccessfulExit", ka,
                         "must not unconditionally relaunch on non-zero clean exits")

    def test_still_runs_at_load_and_throttles(self):
        import plistlib
        rendered = self._template().replace("{{PYTHON}}", "/usr/bin/python3").replace(
            "{{WORKDIR}}", "/tmp/x")
        plist = plistlib.loads(rendered.encode("utf-8"))
        self.assertTrue(plist.get("RunAtLoad"))
        self.assertEqual(plist.get("ThrottleInterval"), 30)


# ─────────────────────────────────────────────────────────────────────────────
# classifier-brain-5 — VERIFY-ONLY (already closed by classifier-brain-1)
# ─────────────────────────────────────────────────────────────────────────────
class TestConfidentSpamStillFloorsInvestor(unittest.TestCase):
    """The confident-spam early return must NOT bypass the deterministic investor-firm-
    domain / legal-attachment structural floors. This was the classifier-brain-5 gap; the
    sibling classifier-brain-1 fix routes confident spam through _finish_spam which applies
    those floors. Asserted here so any future regression that re-introduces a raw early
    return is caught."""

    def _thread_from_investor(self, body):
        from assistant.models import Message, Thread
        m = Message(
            id="m1", thread_id="t1",
            sender_email="partner@sequoia.com",
            subject="intro",
            body_text=body,
            from_me=False,
            recipients=["me@x.com"],
        )
        return Thread(id="t1", subject="intro", messages=[m])

    def test_confident_spam_does_not_bypass_investor_firm_floor(self):
        from assistant.brain import guardrails
        from assistant.models import Contact, Tier
        from tests.helpers import make_decision

        # First-contact investor: no flags, no relationship, importance 0 — exactly the
        # not-yet-flagged case classifier-brain-5 was about. Body trips the scam pattern.
        thread = self._thread_from_investor(
            "next of kin inheritance fund, double your investment with bitcoin")
        contact = Contact(email="partner@sequoia.com", name="")
        decision = make_decision(category="spam_promotional", confidence=0.98)

        res = guardrails.evaluate(thread, decision, contact)
        self.assertGreaterEqual(
            int(res.floor), int(Tier.ASK),
            "confident-spam verdict bypassed the investor-firm-domain floor",
        )
        self.assertTrue(
            any("investor-firm domain" in r for r in res.reasons),
            "the investor-firm-domain structural floor did not fire on confident spam",
        )

    def test_confident_spam_non_investor_still_short_circuits(self):
        # Control: a plain confident-spam item from a nobody is NOT floored up by these
        # structural rules (so the fix is additive, not a blanket floor on all spam).
        from assistant.brain import guardrails
        from assistant.models import Contact, Message, Thread, Tier
        from tests.helpers import make_decision

        m = Message(id="m1", thread_id="t1", sender_email="spammer@spam.example",
                    subject="win", body_text="double your investment with bitcoin now",
                    from_me=False, recipients=["me@x.com"])
        thread = Thread(id="t1", subject="win", messages=[m])
        contact = Contact(email="spammer@spam.example", name="")
        decision = make_decision(category="spam_promotional", confidence=0.98)
        res = guardrails.evaluate(thread, decision, contact)
        self.assertEqual(int(res.floor), int(Tier.SILENT),
                         "plain spam should not be floored up by the structural rules")


if __name__ == "__main__":
    unittest.main()
