import unittest

from assistant.brain import guardrails
from assistant.models import Reversibility, Stakes, Tier
from tests.helpers import make_contact, make_decision, make_message, make_thread


class TestGuardrails(unittest.TestCase):
    def test_benign_newsletter_no_floor(self):
        thread = make_thread(make_message("Our weekly digest of cat photos."))
        dec = make_decision(category="newsletter", stakes=Stakes.LOW,
                            proposed_tier=Tier.SILENT, needs_reply=False)
        res = guardrails.evaluate(thread, dec, make_contact())
        self.assertEqual(res.floor, Tier.SILENT)

    def test_investor_contact_floors_to_approve(self):
        thread = make_thread(make_message("How's the quarter looking?"))
        dec = make_decision(category="personal", proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact(flags={"investor"}))
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_legal_relationship_floors(self):
        thread = make_thread(make_message("Quick question."))
        dec = make_decision(proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact(relationship="lawyer"))
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_money_keyword_floors(self):
        thread = make_thread(make_message("Please pay the attached invoice by Friday."))
        dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact())
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_legal_keyword_floors(self):
        thread = make_thread(make_message("Attached is the NDA and contract for review."))
        dec = make_decision(category="personal", proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact())
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_consequential_category_floors(self):
        thread = make_thread(make_message("hi"))
        dec = make_decision(category="financial", proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact())
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_irreversible_floors_to_approve(self):
        thread = make_thread(make_message("Can you confirm?"))
        dec = make_decision(reversibility=Reversibility.IRREVERSIBLE, proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact())
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_payment_redirection_goes_to_ask(self):
        thread = make_thread(make_message(
            "Urgent: please update the wire transfer to a new bank account number."))
        dec = make_decision(category="personal", proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact())
        self.assertEqual(res.floor, Tier.ASK)

    def test_my_own_text_not_scanned(self):
        # money keywords only in MY sent message should not floor it.
        thread = make_thread(
            make_message("here is the invoice info", from_me=True),
            make_message("thanks, got it!"),
        )
        dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact())
        # "invoice" is in my own message; inbound is benign → no money floor
        self.assertEqual(res.floor, Tier.SILENT)

    # ── Jatin deployment-specific floors ──
    def test_investor_terms_force_ask(self):
        thread = make_thread(make_message("Sharing the term sheet and cap table for the round."))
        dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT,
                            needs_reply=False)
        self.assertEqual(guardrails.evaluate(thread, dec, make_contact()).floor, Tier.ASK)

    def test_investor_firm_domain_forces_ask(self):
        thread = make_thread(make_message("quick intro", sender="partner@a venture firm.com"))
        dec = make_decision(category="personal", proposed_tier=Tier.SILENT)
        res = guardrails.evaluate(thread, dec, make_contact(email="partner@a venture firm.com"))
        self.assertEqual(res.floor, Tier.ASK)

    def test_legal_document_attachment_forces_ask(self):
        from assistant.models import Attachment, Message, Thread
        m = Message(id="m1", thread_id="t1", sender_email="vendor@x.com",
                    recipients=["me@x.com"], subject="paperwork", body_text="see attached",
                    attachments=[Attachment(filename="MutualNDA_v3.pdf", mime_type="application/pdf")])
        thread = Thread(id="t1", messages=[m])
        dec = make_decision(category="personal", proposed_tier=Tier.SILENT)
        self.assertEqual(guardrails.evaluate(thread, dec, make_contact()).floor, Tier.ASK)

    def test_product_and_hardware_and_media_floor_to_approve(self):
        for body in ("Excited about Acme — when does it ship?",
                     "Manufacturing quote attached, lead time is 6 weeks.",
                     "I'm a journalist with an interview request."):
            thread = make_thread(make_message(body))
            dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT,
                                needs_reply=False)
            self.assertGreaterEqual(guardrails.evaluate(thread, dec, make_contact()).floor, Tier.APPROVE,
                                    f"should floor to >= APPROVE: {body}")

    def test_alumni_flag_floors_to_approve(self):
        thread = make_thread(make_message("hey, long time!"))
        dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT, needs_reply=False)
        res = guardrails.evaluate(thread, dec, make_contact(flags={"alumni"}))
        self.assertGreaterEqual(res.floor, Tier.APPROVE)

    def test_ambiguous_words_do_not_over_fire(self):
        # "safe" and "board" alone (not "safe note"/"board seat") must NOT trigger
        # the investor floor — otherwise half the inbox gets surfaced.
        thread = make_thread(make_message("Let's keep the launch plan safe and get everyone on board."))
        dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT,
                            needs_reply=False)
        self.assertEqual(guardrails.evaluate(thread, dec, make_contact()).floor, Tier.SILENT)

    def test_personal_contact_floors_to_ask(self):
        # A personal contact (e.g. a WhatsApp PERSONAL_JID) is always surfaced (Tier 3),
        # never auto-handled — even for a benign-looking low-stakes message.
        thread = make_thread(make_message("haha yeah see you sunday"))
        dec = make_decision(category="personal", stakes=Stakes.LOW, proposed_tier=Tier.SILENT,
                            needs_reply=False)
        res = guardrails.evaluate(thread, dec, make_contact(flags={"personal"}))
        self.assertEqual(res.floor, Tier.ASK)

    def test_failsafe_decision_floors_to_ask(self):
        from assistant.models import Decision
        thread = make_thread(make_message("hi"))
        res = guardrails.evaluate(thread, Decision.failsafe("test"), make_contact())
        self.assertEqual(res.floor, Tier.ASK)


if __name__ == "__main__":
    unittest.main()
