"""Exhaustive use-case test: drives the REAL pipeline (classifier → tiers →
guardrails → dispatcher → drafting → gmail_actions), faking only the three outer
edges (Gmail, the LLM, Telegram). Covers every tier, every guardrail, the live
send path + double-send guard, edit/skip/undo, attachment scanning, VIP elevation,
and the no-em-dash drafting rule.
"""

import json
import unittest

from assistant import main as orchestrator
from assistant.action import gmail_actions
from assistant.config import Settings
from assistant.ingest.base import MailSource
from assistant.models import Attachment, Message, Thread
from assistant.storage import db, ledger
from assistant.storage import repositories as repo


def settings(mode="dry_run") -> Settings:
    return Settings(
        openrouter_api_key="x", telegram_bot_token="x", telegram_chat_id="1",
        gmail_address="me@x.com", mode=mode, db_path=":memory:", prompts_dir="./prompts",
    )


class FakeLLM:
    def __init__(self, classify_obj, noise_obj=None, draft="Sounds good — talk soon.\n- Jatin"):
        self.classify_obj, self.noise_obj, self.draft_text = classify_obj, noise_obj, draft

    def noise_pass(self, *, system_prefix, thread_text, schema):
        return json.dumps(self.noise_obj or {"is_noise": False, "confidence": 0.0, "label": "", "reason": "n"})

    def classify(self, *, system_prefix, thread_text, schema, effort="high", **kw):
        return json.dumps(self.classify_obj)

    def draft(self, *, system_prefix, user_prompt, max_tokens=1200, effort="high", **kw):
        return self.draft_text

    def complete_text(self, **kw):
        return ""


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def _r(self, k, *a):
        self.sent.append((k, a)); return "tg1"

    def send_text(self, t): return self._r("text", t)
    def fyi(self, t): return self._r("fyi", t)
    def send_approval(self, i, s, d, **kw): return self._r("approval", i, s, d)
    def send_ask(self, i, s, sug, **kw): return self._r("ask", i, s, sug)
    def error(self, t): return self._r("error", t)


class FakeMail(MailSource):
    def __init__(self, thread):
        self._thread, self._served = thread, False
        self.sent, self.archived, self.labeled, self.undone = [], [], [], []

    def connect(self): pass

    def fetch_new_message_ids(self):
        if self._served:
            return []
        self._served = True
        return [self._thread.latest.id]

    def get_thread(self, mid): return self._thread
    def archive(self, mid): self.archived.append(mid); return {"op": "archive", "message_id": mid}
    def apply_label(self, mid, label): self.labeled.append((mid, label)); return {"op": "label", "message_id": mid, "label": label}
    def undo(self, undo_data): self.undone.append(undo_data)

    def send_reply(self, *, thread_id, to, cc, subject, body, in_reply_to_gmail_id):
        self.sent.append({"to": to, "subject": subject, "body": body}); return "sent-gid-1"


def thread(body, *, sender="x@x.com", name="X", subject="Hi", attachments=None):
    m = Message(id="m1", thread_id="t1", sender_email=sender, sender_name=name,
                recipients=["me@x.com"], subject=subject, body_text=body,
                attachments=attachments or [])
    return Thread(id="t1", subject=subject, messages=[m])


def decision(**kw):
    base = dict(category="personal", intent="x", sender_importance=20, stakes="medium",
                reversibility="reversible", proposed_tier=2, confidence=0.9, needs_reply=True,
                reasoning="", suggested_action="reply", one_line_summary="x")
    base.update(kw); return base


def run(conn, st, mail, llm):
    notifier = FakeNotifier()
    orchestrator.poll_and_process(conn, st, mail, llm, notifier)
    return notifier


class TestAllUseCases(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    # ---- A. Tiers ----------------------------------------------------------
    def test_tier2_reply_drafted_and_surfaced(self):
        n = run(self.conn, settings(), FakeMail(thread("Free for a call tomorrow?")),
                FakeLLM(decision(category="scheduling", proposed_tier=2)))
        pend = repo.open_pending(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "reply_draft")
        self.assertTrue(any(k == "approval" for k, _ in n.sent))

    def test_tier0_noise_silent(self):
        n = run(self.conn, settings(), FakeMail(thread("50% OFF, unsubscribe here")),
                FakeLLM(decision(), noise_obj={"is_noise": True, "confidence": 0.97, "label": "", "reason": "promo"}))
        self.assertEqual(repo.open_pending(self.conn), [])
        self.assertFalse(any(k == "approval" for k, _ in n.sent))

    def test_tier1_fyi(self):
        n = run(self.conn, settings(), FakeMail(thread("FYI moved the doc, no action needed")),
                FakeLLM(decision(category="personal", stakes="low", proposed_tier=1,
                                 needs_reply=False, confidence=0.95, suggested_action="fyi")))
        self.assertTrue(any(k == "fyi" for k, _ in n.sent))
        self.assertEqual(repo.open_pending(self.conn), [])

    def test_tier3_low_confidence_consequential_asks(self):
        n = run(self.conn, settings(), FakeMail(thread("We need to talk about the thing.")),
                FakeLLM(decision(stakes="high", proposed_tier=2, confidence=0.4)))
        pend = repo.open_pending(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["tier"], 3)
        self.assertTrue(any(k == "ask" for k, _ in n.sent))

    # ---- B. Guardrails -----------------------------------------------------
    def test_money_keyword_forces_surface(self):
        run(self.conn, settings(), FakeMail(thread("Please wire the payment for invoice #44.")),
            FakeLLM(decision(category="personal", stakes="low", proposed_tier=0,
                             confidence=0.99, needs_reply=False, suggested_action="archive")))
        self.assertTrue(repo.open_pending(self.conn))

    def test_legal_keyword_forces_surface(self):
        run(self.conn, settings(), FakeMail(thread("Attaching the NDA and contract for signature.")),
            FakeLLM(decision(category="personal", proposed_tier=0, confidence=0.99,
                             needs_reply=False, suggested_action="archive")))
        self.assertTrue(repo.open_pending(self.conn))

    def test_payment_redirection_is_tier3(self):
        run(self.conn, settings(), FakeMail(thread("Urgent: update the wire transfer to a new bank account number.")),
            FakeLLM(decision(category="personal", proposed_tier=0, confidence=0.99,
                             needs_reply=False, suggested_action="archive")))
        pend = repo.open_pending(self.conn)
        self.assertTrue(pend and pend[0]["tier"] == 3)

    def test_attachment_text_triggers_money_guardrail(self):
        att = [Attachment(filename="invoice.pdf", mime_type="application/pdf",
                          extracted_text="Total due: please wire payment to account 123")]
        run(self.conn, settings(), FakeMail(thread("see attached", attachments=att)),
            FakeLLM(decision(category="personal", stakes="low", proposed_tier=0,
                             confidence=0.99, needs_reply=False, suggested_action="archive")))
        self.assertTrue(repo.open_pending(self.conn), "PDF content with money terms must surface")

    # ---- C. VIP elevation from memory --------------------------------------
    def test_vip_contact_elevated(self):
        c = repo.get_or_default_contact(self.conn, "boss@x.com"); c.importance = 95
        repo.upsert_contact(self.conn, c)
        run(self.conn, settings(), FakeMail(thread("quick q?", sender="boss@x.com", name="Boss")),
            FakeLLM(decision(category="personal", sender_importance=5, proposed_tier=1,
                             stakes="medium", confidence=0.95, needs_reply=True)))
        pend = repo.open_pending(self.conn)
        self.assertTrue(pend and pend[0]["tier"] >= 2)

    # ---- D. Live send + double-send guard ----------------------------------
    def test_live_approve_sends_once_even_on_double_tap(self):
        st = settings("live")
        mail = FakeMail(thread("can you confirm tomorrow?", sender="a@x.com"))
        run(self.conn, st, mail, FakeLLM(decision(category="scheduling", proposed_tier=2)))
        aid = repo.open_pending(self.conn)[0]["id"]
        # first tap
        self.assertTrue(repo.mark_approved(self.conn, aid))
        self.assertTrue(gmail_actions.execute_send(self.conn, mail, st, aid))
        # second (stale) tap
        self.assertFalse(repo.mark_approved(self.conn, aid))
        self.assertFalse(gmail_actions.execute_send(self.conn, mail, st, aid))
        self.assertEqual(len(mail.sent), 1, "must send exactly once")
        self.assertEqual(repo.get_pending(self.conn, aid)["status"], "SENT")

    def test_edit_then_approve_sends_edited_text(self):
        st = settings("live")
        mail = FakeMail(thread("can you confirm?", sender="a@x.com"))
        run(self.conn, st, mail, FakeLLM(decision(category="scheduling", proposed_tier=2)))
        aid = repo.open_pending(self.conn)[0]["id"]
        self.assertTrue(repo.set_pending_draft(self.conn, aid, "Yes, 3pm works."))
        self.assertTrue(repo.mark_approved(self.conn, aid))
        self.assertTrue(gmail_actions.execute_send(self.conn, mail, st, aid))
        self.assertEqual(mail.sent[0]["body"], "Yes, 3pm works.")

    def test_skip_does_not_send(self):
        st = settings("live")
        mail = FakeMail(thread("hi", sender="a@x.com"))
        run(self.conn, st, mail, FakeLLM(decision(category="scheduling", proposed_tier=2)))
        aid = repo.open_pending(self.conn)[0]["id"]
        self.assertTrue(repo.mark_skipped(self.conn, aid))
        self.assertFalse(gmail_actions.execute_send(self.conn, mail, st, aid))
        self.assertEqual(mail.sent, [])

    # ---- E. Silent action + undo (live) ------------------------------------
    def test_live_archive_then_undo(self):
        st = settings("live")
        mail = FakeMail(thread("weekly digest", sender="news@x.com"))
        run(self.conn, st, mail, FakeLLM(decision(),
            noise_obj={"is_noise": True, "confidence": 0.97, "label": "", "reason": "bulk"}))
        self.assertEqual(mail.archived, ["m1"])             # actually archived in live
        msg = gmail_actions.undo_last(self.conn, mail, st)
        self.assertTrue(mail.undone)                         # undo reached Gmail
        self.assertIn("Undid", msg)

    # ---- F. Drafting: no em-dashes through the real draft path --------------
    def test_draft_has_no_em_dash(self):
        st = settings()
        mail = FakeMail(thread("can you confirm tomorrow?", sender="a@x.com"))
        # FakeLLM.draft returns text containing an em dash; the pipeline must strip it.
        run(self.conn, st, mail, FakeLLM(decision(category="scheduling", proposed_tier=2),
                                         draft="Sure — let's do 3pm — works for me."))
        draft = repo.open_pending(self.conn)[0]["draft_text"]
        self.assertNotIn("—", draft)
        self.assertIn(",", draft)  # em dashes became commas

    # ---- exactly-once across a re-poll -------------------------------------
    def test_same_message_not_surfaced_twice(self):
        st = settings()
        mail = FakeMail(thread("free for a call?", sender="a@x.com"))
        llm = FakeLLM(decision(category="scheduling", proposed_tier=2))
        run(self.conn, st, mail, llm)
        # force a reprocess (as recover_stale would)
        self.conn.execute("UPDATE processed_messages SET state='PROCESSING' WHERE message_id='m1'")
        ledger.recover_stale(self.conn)
        run(self.conn, st, mail, llm)
        self.assertEqual(len(repo.open_pending(self.conn)), 1, "must not double-queue")


if __name__ == "__main__":
    unittest.main()
