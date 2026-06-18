"""One-click backtrack to the source: exact Gmail thread, or the WhatsApp chat (native app).

WhatsApp has no per-message deep link — the best any app can do is open the CHAT — so a
WhatsApp source link only exists when we have the person's number (a phone JID, or an @lid
resolved via the person graph / address book). An unresolved @lid yields no link.
"""

from __future__ import annotations

import unittest

from assistant.storage import db
from assistant.storage import read_queries as rq
from assistant.storage import repositories as repo


class TestSourceLink(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def test_gmail_opens_exact_thread(self):
        s = rq.source_link("m1", "THREAD123", "gmail")
        self.assertEqual(s["url"], "https://mail.google.com/mail/u/0/#all/THREAD123")
        self.assertEqual(s["label"], "Open in Gmail")

    def test_whatsapp_phone_jid_uses_native_scheme(self):
        s = rq.source_link("wa_x", "t", "whatsapp", sender_email="918750715626@s.whatsapp.net")
        self.assertEqual(s["url"], "whatsapp://send?phone=918750715626")
        self.assertEqual(s["label"], "Open in WhatsApp")

    def test_whatsapp_saved_lid_resolves_number_via_person_graph(self):
        # Simba: an @lid whose number is known only through the saved person → still openable
        repo.save_contact(self.conn, "269419204890650@lid", "Simba", phone="918750715626")
        num = rq._wa_number_for(self.conn, "269419204890650@lid")
        self.assertEqual(num, "918750715626")
        s = rq.source_link("wa_y", "t", "whatsapp",
                           sender_email="269419204890650@lid", phone_number=num)
        self.assertEqual(s["url"], "whatsapp://send?phone=918750715626")

    def test_unresolved_lid_has_no_link(self):
        self.assertEqual(rq._wa_number_for(self.conn, "999000@lid"), "")
        s = rq.source_link("wa_z", "t", "whatsapp", sender_email="999000@lid")
        self.assertEqual(s["url"], "")          # card shows "Save contact" instead

    def test_decisions_endpoint_includes_source_url(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:  # noqa: BLE001
            self.skipTest("fastapi TestClient not installed")
        import tempfile

        from assistant.config import Settings
        from assistant.models import Decision, FinalDecision, Message, Thread, Tier
        from assistant.storage import decision_log
        from assistant.web import api as webapi

        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        orig = webapi._settings
        webapi._settings = Settings(db_path=tf.name)
        try:
            conn = db.open_db(tf.name)
            msg = Message(id="gm1", thread_id="TID9", sender_email="x@y.com",
                          sender_name="X", subject="hi", body_text="hello")
            th = Thread(id="TID9", subject="hi", messages=[msg])
            dec = Decision(category="personal", intent="asks", proposed_tier=Tier.APPROVE,
                           confidence=0.8, needs_reply=True, one_line_summary="hi")
            fin = FinalDecision(final_tier=Tier.APPROVE, base_tier=Tier.APPROVE,
                                confidence=0.8, decision=dec)
            decision_log.record(conn, message=msg, thread=th, decision=dec, final=fin, dry_run=True)
            repo.create_pending(conn, idempotency_key="gm1", message_id="gm1", thread_id="TID9",
                                tier=2, kind="reply_draft", summary="reply")
            conn.commit()
            items = TestClient(webapi.app).get("/api/decisions").json()["items"]
            card = next((i for i in items if i["message_id"] == "gm1"), None)
            self.assertIsNotNone(card)
            self.assertEqual(card["source_url"], "https://mail.google.com/mail/u/0/#all/TID9")
            self.assertEqual(card["source_label"], "Open in Gmail")
        finally:
            webapi._settings = orig


if __name__ == "__main__":
    unittest.main()
