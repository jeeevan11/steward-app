"""End-to-end dry-run integration: drive the REAL pipeline (classifier → tiers →
dispatcher → gmail_actions) with fakes only at the three external edges (Gmail,
the LLM, Telegram). Proves the independently-built leaf modules compose and that
dry-run changes nothing in Gmail and never sends.
"""

import json
import unittest

from assistant import main as orchestrator
from assistant.config import Settings
from assistant.ingest.base import MailSource
from assistant.models import Attachment, Message, Thread
from assistant.storage import db, ledger
from assistant.storage import repositories as repo


def _settings() -> Settings:
    return Settings(
        openrouter_api_key="x", telegram_bot_token="x", telegram_chat_id="1",
        gmail_address="me@x.com", mode="dry_run", db_path=":memory:",
        prompts_dir="./prompts",
    )


class FakeLLM:
    def __init__(self, classify_obj, noise_obj=None, draft="Sounds good — let's do [confirm date].\n— me"):
        self.classify_obj = classify_obj
        self.noise_obj = noise_obj or {"is_noise": False, "confidence": 0.0, "label": "", "reason": "n/a"}
        self.draft_text = draft

    def noise_pass(self, *, system_prefix, thread_text, schema):
        return json.dumps(self.noise_obj)

    def classify(self, *, system_prefix, thread_text, schema, effort="high", **kw):
        return json.dumps(self.classify_obj)

    def draft(self, *, system_prefix, user_prompt, max_tokens=2000, effort="high", **kw):
        return self.draft_text

    def complete_text(self, **kw):
        return ""


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def _rec(self, kind, *a):
        self.sent.append((kind, a))
        return "tg-msg-1"

    def send_text(self, text): return self._rec("text", text)
    def fyi(self, text): return self._rec("fyi", text)
    def send_approval(self, action_id, summary, draft, **kw): return self._rec("approval", action_id, summary, draft)
    def send_ask(self, action_id, summary, suggestion, **kw): return self._rec("ask", action_id, summary, suggestion)
    def error(self, text): return self._rec("error", text)


class FakeMail(MailSource):
    def __init__(self, thread: Thread):
        self._thread = thread
        self.new_ids_returned = False
        self.archived = []
        self.labeled = []
        self.sent = []

    def connect(self): pass

    def fetch_new_message_ids(self):
        if self.new_ids_returned:
            return []
        self.new_ids_returned = True
        return [self._thread.latest.id]

    def get_thread(self, message_id): return self._thread
    def archive(self, message_id): self.archived.append(message_id); return {"op": "archive", "message_id": message_id}
    def apply_label(self, message_id, label): self.labeled.append((message_id, label)); return {"op": "label"}
    def undo(self, undo_data): pass

    def send_reply(self, *, thread_id, to, cc, subject, body, in_reply_to_gmail_id):
        self.sent.append((to, subject, body))
        return "should-not-happen-in-dry-run"


def _thread(body, *, sender="boss@x.com", name="Boss", subject="Quick ask"):
    m = Message(id="m1", thread_id="t1", sender_email=sender, sender_name=name,
                recipients=["me@x.com"], subject=subject, body_text=body)
    return Thread(id="t1", subject=subject, messages=[m])


class TestDryRunPipeline(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.settings = _settings()
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.conn.close()

    def test_tier2_drafts_and_surfaces_without_sending(self):
        # An important sender asking a question → draft a reply for approval.
        repo.upsert_contact(self.conn, repo.get_or_default_contact(self.conn, "boss@x.com"))
        c = repo.get_contact(self.conn, "boss@x.com"); c.importance = 90; repo.upsert_contact(self.conn, c)

        thread = _thread("Can you review the deck and reply by tomorrow?")
        mail = FakeMail(thread)
        llm = FakeLLM({
            "category": "work_request", "intent": "requests a reply",
            "sender_importance": 60, "stakes": "medium", "reversibility": "reversible",
            "proposed_tier": 2, "confidence": 0.9, "needs_reply": True,
            "reasoning": "boss wants a reply", "suggested_action": "reply",
            "one_line_summary": "Boss wants the deck reviewed by tomorrow",
        })

        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)

        # ledger: processed exactly once, DONE
        self.assertTrue(ledger.is_done(self.conn, "m1"))
        # a pending reply was created and surfaced via Telegram
        pend = repo.open_pending(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "reply_draft")
        self.assertTrue(pend[0]["draft_text"])
        self.assertTrue(any(k == "approval" for k, _ in self.notifier.sent))
        # DRY RUN: nothing sent, nothing changed in Gmail
        self.assertEqual(mail.sent, [])
        self.assertEqual(mail.archived, [])

    def test_money_keyword_forces_surface_even_if_model_says_silent(self):
        thread = _thread("Please wire the payment for invoice #44 to our account.",
                         sender="vendor@x.com", name="Vendor")
        mail = FakeMail(thread)
        llm = FakeLLM({
            "category": "personal", "intent": "fyi", "sender_importance": 10,
            "stakes": "low", "reversibility": "reversible", "proposed_tier": 0,
            "confidence": 0.99, "needs_reply": False, "reasoning": "looks routine",
            "suggested_action": "archive", "one_line_summary": "vendor note",
        })
        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)
        # guardrail floor forced this above silent → surfaced, not archived
        self.assertTrue(ledger.is_done(self.conn, "m1"))
        self.assertEqual(mail.archived, [])
        self.assertTrue(repo.open_pending(self.conn), "money keyword must surface, not auto-handle")

    def test_noise_is_handled_silently_in_dry_run(self):
        thread = _thread("Our weekly newsletter: 10 cat photos!", sender="news@x.com", name="News")
        mail = FakeMail(thread)
        llm = FakeLLM(
            classify_obj={  # won't be reached — noise pass short-circuits
                "category": "newsletter", "intent": "fyi", "sender_importance": 0,
                "stakes": "low", "reversibility": "reversible", "proposed_tier": 0,
                "confidence": 0.9, "needs_reply": False, "reasoning": "", "suggested_action": "archive",
                "one_line_summary": "newsletter",
            },
            noise_obj={"is_noise": True, "confidence": 0.96, "label": "Newsletters", "reason": "bulk"},
        )
        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)
        self.assertTrue(ledger.is_done(self.conn, "m1"))
        # no pending item, no approval message — handled silently
        self.assertEqual(repo.open_pending(self.conn), [])
        # dry-run: did NOT actually touch Gmail, but DID write an audit row
        self.assertEqual(mail.archived, [])
        self.assertEqual(mail.labeled, [])
        actions = repo.recent_actions(self.conn, 0)
        self.assertTrue(actions, "a silent action should be audited even in dry-run")
        self.assertTrue(all(a["dry_run"] == 1 for a in actions))

    def _simulate_crash_and_reprocess(self, mail, llm):
        # Crash AFTER dispatch's effects but BEFORE ledger.complete: the row is left
        # PROCESSING; recover_stale re-queues it and the next poll re-runs it.
        self.conn.execute("UPDATE processed_messages SET state='PROCESSING' WHERE message_id='m1'")
        ledger.recover_stale(self.conn)
        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)

    def test_silent_action_reprocess_is_idempotent(self):
        thread = _thread("Weekly newsletter: 10 cat photos!", sender="news@x.com", name="News")
        mail = FakeMail(thread)
        llm = FakeLLM(
            classify_obj={"category": "newsletter", "intent": "fyi", "sender_importance": 0,
                          "stakes": "low", "reversibility": "reversible", "proposed_tier": 0,
                          "confidence": 0.9, "needs_reply": False, "reasoning": "",
                          "suggested_action": "label:Newsletters", "one_line_summary": "nl"},
            noise_obj={"is_noise": True, "confidence": 0.96, "label": "Newsletters", "reason": "bulk"},
        )
        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)
        first = repo.recent_actions(self.conn, 0)
        self.assertEqual(len([a for a in first if a["kind"] in ("archive", "label")]), 1)

        self._simulate_crash_and_reprocess(mail, llm)
        after = repo.recent_actions(self.conn, 0)
        # No DUPLICATE silent action / audit row after reprocessing.
        self.assertEqual(len([a for a in after if a["kind"] in ("archive", "label")]), 1)

    def test_fyi_reprocess_does_not_double_notify(self):
        thread = _thread("FYI: the build finished.", sender="ci@x.com", name="CI")
        mail = FakeMail(thread)
        llm = FakeLLM({
            "category": "personal", "intent": "fyi", "sender_importance": 10,
            "stakes": "low", "reversibility": "reversible", "proposed_tier": 1,
            "confidence": 0.95, "needs_reply": False, "reasoning": "",
            "suggested_action": "fyi", "one_line_summary": "build finished",
        })
        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)
        fyi_count_1 = sum(1 for k, _ in self.notifier.sent if k == "fyi")
        self.assertEqual(fyi_count_1, 1)

        self._simulate_crash_and_reprocess(mail, llm)
        fyi_count_2 = sum(1 for k, _ in self.notifier.sent if k == "fyi")
        self.assertEqual(fyi_count_2, 1, "reprocessing must not re-send the FYI")

    def test_classifier_failsafe_surfaces(self):
        thread = _thread("???")
        mail = FakeMail(thread)

        class BadLLM(FakeLLM):
            def classify(self, **kw):
                return "this is not json at all"

        llm = BadLLM({})
        orchestrator.poll_and_process(self.conn, self.settings, mail, llm, self.notifier)
        self.assertTrue(ledger.is_done(self.conn, "m1"))
        # fail-safe → Tier 3 ask, surfaced to the human
        self.assertTrue(repo.open_pending(self.conn))
        self.assertEqual(mail.sent, [])


if __name__ == "__main__":
    unittest.main()
