"""Telegram card formatting — the known/unsaved marker, the email line (address +
subject), and the inbound quote all render, and empty fields are omitted."""

from __future__ import annotations

import unittest

from assistant.action import dispatcher
from assistant.control import notifier
from assistant.models import Channel, Contact, Message, Thread


class TestFormatCard(unittest.TestCase):
    def test_all_fields_render_in_order(self):
        body = notifier.format_card(
            tier=2, signal="needs a reply", sender="👤 Alice · investor",
            mail='✉️ alice@acme.com · "Term sheet"', quote="can we close this week?",
            draft="Sure, let's do it.",
        )
        lines = body.splitlines()
        self.assertIn("🟡 needs a reply", lines[0])
        self.assertEqual(lines[1], "👤 Alice · investor")
        self.assertEqual(lines[2], '✉️ alice@acme.com · "Term sheet"')
        self.assertEqual(lines[3], '"can we close this week?"')

    def test_empty_fields_are_omitted(self):
        body = notifier.format_card(tier=3, signal="x", draft="d")
        # only the signal line + separator + preview (no sender/mail/quote lines)
        self.assertNotIn('"', body.split(notifier._SEP)[0].split("\n", 1)[-1] or "")
        self.assertEqual(len(body.split(notifier._SEP)[0].strip().splitlines()), 1)

    def test_quote_is_wrapped_in_quotes_and_truncated(self):
        body = notifier.format_card(tier=2, signal="x", quote="y" * 500, draft="d")
        qline = [l for l in body.splitlines() if l.startswith('"')][0]
        self.assertTrue(qline.startswith('"') and qline.endswith('…"'))
        self.assertLessEqual(len(qline), notifier._QUOTE_MAX + 3)


class TestCardFields(unittest.TestCase):
    def _thread(self, channel, addr, name, subject, body):
        msg = Message(id="m", thread_id="t", channel=channel, sender_email=addr,
                      sender_name=name, subject=subject, body_text=body)
        return Thread(id="t", subject=subject, channel=channel, messages=[msg])

    def test_known_contact_gets_person_marker(self):
        thread = self._thread(Channel.GMAIL, "alice@acme.com", "Alice", "Term sheet", "hi")
        contact = Contact(email="alice@acme.com", name="Alice", relationship="investor")
        who, mail, quote = dispatcher._card_fields(thread, contact)
        self.assertTrue(who.startswith("👤"))
        self.assertIn("investor", who)
        self.assertIn("alice@acme.com", mail)
        self.assertIn("Term sheet", mail)
        self.assertEqual(quote, "hi")

    def test_unsaved_contact_gets_new_marker(self):
        thread = self._thread(Channel.GMAIL, "stranger@x.com", "Stranger", "Hello", "buy now")
        contact = Contact(email="stranger@x.com", name="Stranger")
        who, mail, quote = dispatcher._card_fields(thread, contact)
        self.assertTrue(who.startswith("🆕"))
        self.assertIn("not a saved contact", who)

    def test_whatsapp_uses_chat_marker(self):
        thread = self._thread(Channel.WHATSAPP, "9199@s.whatsapp.net", "Bob", "", "yo")
        contact = Contact(email="9199@s.whatsapp.net", name="Bob")
        who, mail, quote = dispatcher._card_fields(thread, contact)
        self.assertIn("💬", mail)
        self.assertEqual(quote, "yo")


if __name__ == "__main__":
    unittest.main()
