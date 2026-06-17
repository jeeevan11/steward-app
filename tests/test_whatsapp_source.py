"""WhatsApp source: normalization, group-skip, intake, voice prefix, personal flag,
and wa_/gmail ledger non-collision. Stdlib only (no Node, no network, no Baileys)."""

import json
import unittest

from assistant import main as orchestrator
from assistant.config import Settings
from assistant.ingest import whatsapp_source as wa
from assistant.models import Channel
from assistant.storage import db, ledger
from assistant.storage import repositories as repo
from assistant.storage import whatsapp_inbox as inbox

ME = "me@s.whatsapp.net"
FRIEND = "919876500000@s.whatsapp.net"
PERSONAL = "919999900000@s.whatsapp.net"


def settings():
    # Settling OFF here: these tests cover intake/queue/pipeline mechanics, where a
    # freshly-arrived message must hand off immediately. The settle/debounce gate has
    # its own coverage in test_whatsapp_settle.py.
    return Settings(
        mode="dry_run", db_path=":memory:",
        wa_user_jid=ME, personal_jids=(PERSONAL,), watch_keywords=("urgent", "decision"),
        whatsapp_enabled=True, whatsapp_settle_enabled=False,
        # Presence reads the live Mac's frontmost app via osascript; disable it so these
        # pipeline tests are hermetic (otherwise they fail when WhatsApp is frontmost).
        presence_suppression_enabled=False,
    )


def dm_payload(mid="abc", body="hey are you free?", jid=FRIEND, **kw):
    base = dict(messageId=mid, jid=jid, sender_jid=jid, push_name="Friend",
                body=body, media_type="", is_group=False, group_name="",
                quoted_body="", mentions=[], timestamp=1700000000)
    base.update(kw)
    return base


class FakeLLM:
    def __init__(self, transcript="call me back"):
        self.transcript = transcript

    def transcribe(self, *, audio_b64, audio_format="ogg", model=None):
        return self.transcript

    # GAP 8: _materialize now calls transcribe_audio (returns None on failure).
    def transcribe_audio(self, audio_b64, audio_format="ogg", model=None):
        return self.transcript


class TestNormalize(unittest.TestCase):
    def test_wa_id_prefix(self):
        self.assertEqual(wa.wa_id("abc"), "wa_abc")

    def test_text_message(self):
        m = wa.normalize(dm_payload(), settings())
        self.assertEqual(m.id, "wa_abc")
        self.assertEqual(m.channel, Channel.WHATSAPP)
        self.assertEqual(m.sender_email, FRIEND)
        self.assertEqual(m.thread_id, FRIEND)
        self.assertEqual(m.body_text, "hey are you free?")
        self.assertFalse(m.from_me)

    def test_voice_with_transcript(self):
        m = wa.normalize(dm_payload(media_type="audio", body=""), settings(), transcript="call me back")
        self.assertEqual(m.body_text, "[voice note, transcribed]: call me back")

    def test_voice_without_transcript_placeholder(self):
        m = wa.normalize(dm_payload(media_type="audio", body=""), settings(), transcript=None)
        self.assertIn("[voice note", m.body_text)
        # GAP 8: untranscribable voice notes use the "could not transcribe" placeholder.
        self.assertIn("could not transcribe", m.body_text)

    def test_image_placeholder(self):
        m = wa.normalize(dm_payload(media_type="image", body="look"), settings())
        self.assertEqual(m.body_text, "[image] look")

    def test_quoted_prefix(self):
        m = wa.normalize(dm_payload(quoted_body="earlier msg"), settings())
        self.assertIn("[replying to: earlier msg]", m.body_text)


class TestGroupSkip(unittest.TestCase):
    def test_dm_never_skipped(self):
        self.assertFalse(wa.should_skip_group(dm_payload(), settings()))

    def test_group_without_mention_or_keyword_is_skipped(self):
        p = dm_payload(is_group=True, group_name="Family", body="lol nice")
        self.assertTrue(wa.should_skip_group(p, settings()))

    def test_group_with_mention_not_skipped(self):
        p = dm_payload(is_group=True, body="hey", mentions=[ME])
        self.assertFalse(wa.should_skip_group(p, settings()))

    def test_group_with_keyword_not_skipped(self):
        p = dm_payload(is_group=True, body="this is URGENT please look")
        self.assertFalse(wa.should_skip_group(p, settings()))


class TestIntake(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.s = settings()

    def tearDown(self):
        self.conn.close()

    def test_dm_is_queued(self):
        mid = wa.ingest_payload(self.conn, self.s, dm_payload())
        self.assertEqual(mid, "wa_abc")
        self.assertEqual(inbox.get(self.conn, "wa_abc")["status"], "new")

    def test_skipped_group_marked_done_not_queued(self):
        mid = wa.ingest_payload(self.conn, self.s, dm_payload(is_group=True, body="hi", group_name="G"))
        self.assertIsNone(mid)
        row = ledger.get(self.conn, "wa_abc")
        self.assertEqual(row["state"], ledger.DONE)
        self.assertEqual(row["category"], "group_skipped")

    def test_personal_jid_stamped(self):
        wa.ingest_payload(self.conn, self.s, dm_payload(jid=PERSONAL, sender_jid=PERSONAL))
        c = repo.get_contact(self.conn, PERSONAL)
        self.assertIsNotNone(c)
        self.assertIn("personal", c.flags)

    def test_fetch_marks_seen_and_queued(self):
        wa.ingest_payload(self.conn, self.s, dm_payload())
        src = wa.WhatsAppSource(self.conn, self.s, llm=None)  # don't connect() — no socket
        ids = src.fetch_new_message_ids()
        self.assertEqual(ids, ["wa_abc"])
        self.assertEqual(inbox.get(self.conn, "wa_abc")["status"], "queued")
        self.assertIsNotNone(ledger.get(self.conn, "wa_abc"))  # durably recorded

    def test_get_thread_text(self):
        wa.ingest_payload(self.conn, self.s, dm_payload(body="free tonight?"))
        src = wa.WhatsAppSource(self.conn, self.s, llm=None)
        th = src.get_thread("wa_abc")
        self.assertEqual(th.channel, Channel.WHATSAPP)
        self.assertEqual(th.latest_inbound.body_text, "free tonight?")

    def test_get_thread_transcribes_voice(self):
        wa.ingest_payload(self.conn, self.s, dm_payload(media_type="audio", body="", mid="v1"))
        # set a fake audio blob so get_thread tries transcription
        inbox.set_body(self.conn, "wa_v1", "")  # no-op safety
        self.conn.execute("UPDATE whatsapp_inbox SET media_b64='AAA', audio_format='ogg' WHERE message_id='wa_v1'")
        src = wa.WhatsAppSource(self.conn, self.s, llm=FakeLLM("call me back"))
        th = src.get_thread("wa_v1")
        self.assertIn("[voice note, transcribed]: call me back", th.latest_inbound.body_text)
        # audio blob cleared after caching
        self.assertEqual(inbox.get(self.conn, "wa_v1")["media_b64"], "")

    def test_get_thread_missing(self):
        src = wa.WhatsAppSource(self.conn, self.s, llm=None)
        th = src.get_thread("wa_nope")
        self.assertEqual(th.messages, [])


class PipelineLLM:
    def __init__(self, classify_obj):
        self.classify_obj = classify_obj

    def noise_pass(self, **kw):
        return json.dumps({"is_noise": False, "confidence": 0.0, "label": "", "reason": "n"})

    def classify(self, **kw):
        return json.dumps(self.classify_obj)

    def draft(self, **kw):
        return "sure, see you then"

    def transcribe(self, **kw):
        return "x"

    def complete_text(self, **kw):
        return ""


class PipelineNotifier:
    def __init__(self):
        self.sent = []

    def _r(self, k, *a):
        self.sent.append((k, a)); return "tg1"

    def send_text(self, t): return self._r("text", t)
    def fyi(self, t): return self._r("fyi", t)
    def send_approval(self, i, s, d, *, sender="", mail="", quote="", **kwargs): return self._r("approval", i, s, d, sender)
    def send_ask(self, i, s, sug, *, sender="", mail="", quote="", **kwargs): return self._r("ask", i, s, sug, sender)
    def error(self, t): return self._r("error", t)


_IS_WA = lambda m: m.startswith("wa_")  # noqa: E731


class TestWhatsAppPipeline(unittest.TestCase):
    """Drive a real WhatsAppSource through the actual brain pipeline (fakes only at
    the LLM + Telegram edges; no Node, no sockets)."""

    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.s = settings()

    def tearDown(self):
        self.conn.close()

    def _run(self, payload, classify_obj):
        wa.ingest_payload(self.conn, self.s, payload)
        llm = PipelineLLM(classify_obj)
        src = wa.WhatsAppSource(self.conn, self.s, llm=llm)  # no connect() — no receiver
        notifier = PipelineNotifier()
        orchestrator.poll_and_process(self.conn, self.s, src, llm, notifier,
                                      owns=_IS_WA, do_redeliver=False)
        return notifier

    def test_dm_produces_whatsapp_approval_card(self):
        n = self._run(
            dm_payload(body="free for a call tomorrow?"),
            {"category": "scheduling", "intent": "asks", "sender_importance": 30,
             "stakes": "medium", "reversibility": "reversible", "proposed_tier": 2,
             "confidence": 0.9, "needs_reply": True, "reasoning": "wants a call",
             "suggested_action": "reply", "one_line_summary": "wants a call tomorrow"},
        )
        pend = repo.open_pending(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["kind"], "reply_draft")
        self.assertTrue(pend[0]["summary"].startswith("[WhatsApp]"))
        self.assertTrue(any(k == "approval" for k, _ in n.sent))

    def test_personal_jid_forced_to_tier3(self):
        # even though the model says "silent", the personal flag floors it to ASK
        self._run(
            dm_payload(jid=PERSONAL, sender_jid=PERSONAL, body="haha see you sunday"),
            {"category": "personal", "intent": "chat", "sender_importance": 10,
             "stakes": "low", "reversibility": "reversible", "proposed_tier": 0,
             "confidence": 0.99, "needs_reply": False, "reasoning": "casual",
             "suggested_action": "archive", "one_line_summary": "weekend plan"},
        )
        pend = repo.open_pending(self.conn)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["tier"], 3)         # personal → ASK
        self.assertEqual(pend[0]["kind"], "ask")

    def test_skipped_group_produces_nothing(self):
        n = self._run(
            dm_payload(is_group=True, group_name="Family", body="lol"),
            {"category": "social", "intent": "chat", "sender_importance": 0,
             "stakes": "low", "reversibility": "reversible", "proposed_tier": 0,
             "confidence": 0.9, "needs_reply": False, "reasoning": "x",
             "suggested_action": "archive", "one_line_summary": "x"},
        )
        self.assertEqual(repo.open_pending(self.conn), [])
        self.assertEqual(n.sent, [])
        self.assertEqual(ledger.get(self.conn, "wa_abc")["category"], "group_skipped")


class TestChannelRouting(unittest.TestCase):
    """The reviewer-found critical bug: a WhatsApp approval must send via WhatsApp,
    not Gmail. MailRouter routes execute_send by the action's message-id prefix."""

    def setUp(self):
        self.conn = db.open_db(":memory:")
        self.s = Settings(mode="live", db_path=":memory:")  # LIVE so send actually routes

    def tearDown(self):
        self.conn.close()

    def _fake_source(self):
        class FakeSrc:
            def __init__(s): s.sent = []
            def get_thread(s, mid):
                from assistant.models import Message, Thread
                m = Message(id=mid, thread_id="t", sender_email="x@x", recipients=["me@x"])
                return Thread(id="t", messages=[m])
            def send_reply(s, **kw): s.sent.append(kw); return "sent-id"
        return FakeSrc()

    def test_whatsapp_action_routes_to_whatsapp_not_gmail(self):
        from assistant.action import gmail_actions
        from assistant.ingest.router import MailRouter
        gmail, wa = self._fake_source(), self._fake_source()
        router = MailRouter({"gmail": gmail, "whatsapp": wa})
        aid = repo.create_pending(self.conn, idempotency_key="k", message_id="wa_abc",
                                  thread_id="jid@s.whatsapp.net", tier=2, kind="reply_draft",
                                  summary="s", draft_text="hi")
        repo.mark_approved(self.conn, aid)
        ok = gmail_actions.execute_send(self.conn, router, self.s, aid)
        self.assertTrue(ok)
        self.assertEqual(len(wa.sent), 1)       # routed to WhatsApp
        self.assertEqual(gmail.sent, [])        # NOT Gmail
        self.assertEqual(wa.sent[0]["thread_id"], "jid@s.whatsapp.net")

    def test_gmail_action_routes_to_gmail(self):
        from assistant.action import gmail_actions
        from assistant.ingest.router import MailRouter
        gmail, wa = self._fake_source(), self._fake_source()
        router = MailRouter({"gmail": gmail, "whatsapp": wa})
        aid = repo.create_pending(self.conn, idempotency_key="k2", message_id="gmailid",
                                  thread_id="t", tier=2, kind="reply_draft", summary="s", draft_text="hi")
        repo.mark_approved(self.conn, aid)
        gmail_actions.execute_send(self.conn, router, self.s, aid)
        self.assertEqual(len(gmail.sent), 1)
        self.assertEqual(wa.sent, [])


class TestAtomicSkip(unittest.TestCase):
    def test_record_skipped_is_done_in_one_step(self):
        conn = db.open_db(":memory:")
        ledger.record_skipped(conn, "wa_g1", "grp@g.us", "group_skipped")
        row = ledger.get(conn, "wa_g1")
        self.assertEqual(row["state"], ledger.DONE)         # never passes through SEEN
        self.assertEqual(row["category"], "group_skipped")
        self.assertFalse(ledger.claim(conn, "wa_g1"))       # a poller can never claim it
        conn.close()


class TestLedgerNonCollision(unittest.TestCase):
    def test_wa_and_gmail_ids_coexist(self):
        conn = db.open_db(":memory:")
        self.assertTrue(ledger.mark_seen(conn, "wa_abc", "jid"))
        self.assertTrue(ledger.mark_seen(conn, "abc", "thread"))  # plain gmail-style id
        self.assertFalse(ledger.mark_seen(conn, "wa_abc"))         # dedup still works
        self.assertIsNotNone(ledger.get(conn, "wa_abc"))
        self.assertIsNotNone(ledger.get(conn, "abc"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
