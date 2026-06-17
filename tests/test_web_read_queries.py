"""Read-only console queries + plain-English mapping. Stdlib only (no FastAPI)."""

import unittest

from assistant.models import Decision, FinalDecision, Message, Thread, Tier
from assistant.storage import db, decision_log
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo


def seed_decision(conn, mid, *, sender, subject, body, base, final,
                  category="personal", needs_reply=True, reasoning="because reasons"):
    msg = Message(id=mid, thread_id=f"t-{mid}", sender_email=sender,
                  sender_name=sender.split("@")[0], subject=subject, body_text=body,
                  timestamp=repo.now_epoch())
    th = Thread(id=f"t-{mid}", subject=subject, messages=[msg])
    dec = Decision(category=category, intent="asks", sender_importance=10, stakes="medium",
                   reversibility="reversible", proposed_tier=base, confidence=0.8,
                   needs_reply=needs_reply, reasoning=reasoning, suggested_action="reply",
                   one_line_summary=subject)
    fin = FinalDecision(final_tier=Tier(final), base_tier=Tier(base), confidence=0.8, decision=dec)
    decision_log.record(conn, message=msg, thread=th, decision=dec, final=fin, dry_run=True)


class FakeSettings:
    dry_run = True
    gmail_address = "me@x.com"
    poll_interval_seconds = 20


class TestReadQueries(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        seed_decision(self.conn, "m1", sender="a@x.com", subject="promo", body="50% off", base=0, final=0)
        seed_decision(self.conn, "m2", sender="b@x.com", subject="fyi", body="moved doc", base=1, final=1)
        seed_decision(self.conn, "m3", sender="c@x.com", subject="call?", body="free tomorrow?", base=2, final=2)
        # near-miss: brain wanted to file it (base 0) but a guardrail surfaced it (final 3)
        seed_decision(self.conn, "m4", sender="vendor@x.com", subject="invoice", body="wire payment", base=0, final=3)
        # a pending reply draft for m3
        repo.create_pending(self.conn, idempotency_key="m3:2", message_id="m3", thread_id="t-m3",
                            tier=2, kind="reply_draft", summary="c: call?", draft_text="Hi [name], sure.")

    def tearDown(self):
        self.conn.close()

    def test_stats(self):
        s = rq.get_stats(self.conn)
        self.assertEqual(s["handled_quietly"], 2)   # m1, m2
        self.assertEqual(s["flagged_for_you"], 2)   # m3, m4
        self.assertEqual(s["replies_waiting"], 1)   # m3 pending draft
        self.assertEqual(s["near_misses"], 1)       # m4

    def test_queue_plain_labels_recent_first(self):
        items = rq.get_queue(self.conn)
        self.assertEqual(len(items), 4)
        labels = {it["message_id"]: it["label"] for it in items}
        self.assertEqual(labels["m1"], "Filed away quietly")
        self.assertEqual(labels["m2"], "Told you, handled")
        self.assertEqual(labels["m3"], "Drafting a reply for you")
        self.assertEqual(labels["m4"], "Needs your decision")
        m3 = next(it for it in items if it["message_id"] == "m3")
        self.assertTrue(m3["has_draft"])
        # only the four plain-English labels ever appear; no tier number leaks
        for it in items:
            self.assertIn(it["label"], set(rq.TIER_LABEL.values()))
            self.assertNotIn("tier", it)

    def test_email_detail_with_draft_and_placeholders(self):
        d = rq.get_email(self.conn, "m3")
        self.assertEqual(d["label"], "Drafting a reply for you")
        self.assertEqual(d["arrived"]["subject"], "call?")
        self.assertIn("free tomorrow", d["arrived"]["quote"])
        self.assertEqual(d["ai"]["urgency"], "Worth noting")
        self.assertEqual(d["ai"]["undo"], "Easily undone")
        self.assertIn("sure", d["ai"]["confidence"])  # phrase like "80% sure"
        self.assertTrue(d["ai"]["why"])
        self.assertIsNotNone(d["draft"])
        self.assertEqual(d["draft"]["placeholders"], ["name"])

    def test_email_detail_missing(self):
        self.assertIsNone(rq.get_email(self.conn, "nope"))

    def test_status(self):
        s = rq.get_status(self.conn, FakeSettings())
        self.assertFalse(s["live"])
        self.assertEqual(s["mode_label"], "DRY-RUN")

    def test_channel_derived_from_id_prefix(self):
        seed_decision(self.conn, "wa_xyz", sender="919@s.whatsapp.net", subject="hey",
                      body="free?", base=2, final=2)
        items = {it["message_id"]: it for it in rq.get_queue(self.conn)}
        self.assertEqual(items["wa_xyz"]["channel"], "whatsapp")
        self.assertEqual(items["wa_xyz"]["channel_label"], "WhatsApp")
        self.assertEqual(items["m1"]["channel"], "gmail")
        d = rq.get_email(self.conn, "wa_xyz")
        self.assertEqual(d["channel"], "whatsapp")

    def test_wastatus_disabled_by_default(self):
        self.assertEqual(rq.get_wastatus(FakeSettings()), {"enabled": False})

    def test_contacts_rules_audit_shapes(self):
        c = repo.get_or_default_contact(self.conn, "c@x.com"); c.importance = 80
        repo.upsert_contact(self.conn, c)
        repo.add_rule(self.conn, scope="global", instruction="be terse")
        repo.log_action(self.conn, kind="archive", message_id="m1", summary="filed promo",
                        reversible=True, dry_run=True)
        people = rq.list_contacts(self.conn)
        self.assertTrue(any(p["is_vip"] for p in people))
        rules = rq.list_rules(self.conn)
        self.assertEqual(rules[0]["rule"], "be terse")
        audit = rq.list_audit(self.conn, 0)
        self.assertEqual(audit[0]["what"], "Filed away")


if __name__ == "__main__":
    unittest.main()
