"""Memory-integrity regression tests (HIGH findings).

Closes:
  * memory-knowledge-1 — durable facts carry provenance; a merely-"claimed"
    (counterparty-asserted) fact is never rendered as established/verified truth.
  * memory-knowledge-3 — a single low-trust assertion cannot silently supersede a
    higher-trust established fact (confidence/corroboration gate).
  * memory-knowledge-4 — the owner-commitment extractor reads only the owner's own
    outbound text, so a sender cannot forge a "you promised ..." obligation
    attributed to the owner; counterparty claims stay attributed to them.
  * memory-identity-2 — confirm_suggestion migrates relationship_memory (and other
    person_id-keyed state) to the survivor instead of destroying it on delete.
  * memory-identity-4 — name + shared corporate domain is a confirm-once suggestion,
    not a silent auto-merge (namesakes / shared mailboxes / spoofed senders).

Stdlib only; in-memory DB; injected fakes (no network, no wall clock for pure paths).
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from assistant.config import Settings
from assistant.memory import commitment_extract as CE
from assistant.memory import distill
from assistant.memory import identity
from assistant.memory import retrieval
from assistant.memory.distill import RelationshipMemory, apply_ops
from assistant.models import Channel, Message, Thread
from assistant.storage import db
from assistant.storage import repositories as repo


def _settings(**kw) -> Settings:
    base = dict(mode="dry_run", prompts_dir="./prompts", gmail_address="me@x.com",
                telegram_chat_id="1")
    base.update(kw)
    return Settings(**base)


def _email(addr, name="", body=""):
    return Message(id="m", thread_id="t", channel=Channel.GMAIL,
                   sender_email=addr, sender_name=name, body_text=body)


def _wa(jid, name="", body=""):
    return Message(id="wa_m", thread_id=jid, channel=Channel.WHATSAPP,
                   sender_email=jid, sender_name=name, body_text=body)


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-1 — provenance: a claimed fact is never rendered as truth
# ─────────────────────────────────────────────────────────────────────────────
class TestProvenanceRendering(unittest.TestCase):
    def test_claimed_fact_not_presented_as_established(self):
        mem = RelationshipMemory("p")
        # A distilled fact from the counterparty's own message defaults to 'claimed'.
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "title", "value": "CFO of Acme"}]},
                  now=1, source_type="claimed")
        self.assertEqual(mem.fact_source_type("title"), "claimed")
        self.assertTrue(mem.is_claimed("title"))

        block = retrieval.build_memory_block(mem, now=1)
        # The value still appears, but flagged as an unverified claim, NOT under "Facts:".
        self.assertIn("CFO of Acme", block)
        self.assertIn("Unverified claims by them", block)
        facts_line = next((ln for ln in block.splitlines() if ln.startswith("Facts:")), "")
        self.assertNotIn("CFO of Acme", facts_line)  # not presented as established truth

    def test_observed_fact_is_established(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "city", "value": "Berlin"}]},
                  now=1, source_type="observed")
        block = retrieval.build_memory_block(mem, now=1)
        facts_line = next((ln for ln in block.splitlines() if ln.startswith("Facts:")), "")
        self.assertIn("Berlin", facts_line)              # observed -> established
        self.assertNotIn("Unverified claims", block)

    def test_default_distilled_fact_is_claimed(self):
        # distill() stamps facts from the thread as 'claimed' by default.
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "k", "value": "v"}]}, now=1)
        self.assertEqual(mem.fact_source_type("k"), "claimed")

    def test_provenance_survives_save_load_roundtrip(self):
        conn = db.open_db(":memory:")
        try:
            mem = distill.load_memory(conn, "p1")
            apply_ops(mem, {"facts": [{"op": "ADD", "key": "role", "value": "advisor"}]},
                      now=5, source_type="verified", source="owner")
            distill.save_memory(conn, mem)
            again = distill.load_memory(conn, "p1")
            self.assertEqual(again.summary["role"], "advisor")
            self.assertEqual(again.fact_source_type("role"), "verified")
        finally:
            conn.close()

    def test_legacy_row_without_provenance_defaults_claimed(self):
        # A pre-migration record (provenance never written) must read as 'claimed'
        # so old counterparty assertions are not retroactively trusted.
        conn = db.open_db(":memory:")
        try:
            repo.relationship_memory_upsert(
                conn, "old",
                summary_json=json.dumps({"stage": "Series B"}),
                open_situations_json="[]", decided_json="[]", episodes_json="[]",
                superseded_json="[]", last_distilled_at=1, version=1,
            )
            conn.execute("UPDATE relationship_memory SET provenance_json='{}' WHERE person_id='old'")
            mem = distill.load_memory(conn, "old")
            self.assertEqual(mem.fact_source_type("stage"), "claimed")
            block = retrieval.build_memory_block(mem, now=2)
            self.assertIn("Unverified claims by them", block)
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-3 — confidence gate: one claim can't bury an established fact
# ─────────────────────────────────────────────────────────────────────────────
class TestConfidenceGate(unittest.TestCase):
    def test_claim_does_not_supersede_observed_fact(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "employer", "value": "Acme"}]},
                  now=1, source_type="observed")
        # A later counterparty CLAIM tries to overwrite the observed fact.
        apply_ops(mem, {"facts": [{"op": "UPDATE", "key": "employer", "value": "Globex"}]},
                  now=2, source_type="claimed")
        self.assertEqual(mem.summary["employer"], "Acme")     # established fact stands
        self.assertEqual(mem.fact_source_type("employer"), "observed")
        self.assertTrue(any(s.get("reason") == "disputed_claim" and s.get("value") == "Globex"
                            for s in mem.superseded))         # the claim is parked, observable

    def test_claim_does_not_delete_observed_fact(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "employer", "value": "Acme"}]},
                  now=1, source_type="verified")
        apply_ops(mem, {"facts": [{"op": "DELETE", "key": "employer"}]},
                  now=2, source_type="claimed")
        self.assertEqual(mem.summary.get("employer"), "Acme")  # not deleted by a claim
        self.assertTrue(any(s.get("reason") == "disputed_delete" for s in mem.superseded))

    def test_equal_or_higher_trust_supersedes_by_recency(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "employer", "value": "Acme"}]},
                  now=1, source_type="claimed")
        # A verified (higher-trust) correction WINS over an earlier claim.
        apply_ops(mem, {"facts": [{"op": "UPDATE", "key": "employer", "value": "Globex"}]},
                  now=2, source_type="verified")
        self.assertEqual(mem.summary["employer"], "Globex")
        self.assertEqual(mem.fact_source_type("employer"), "verified")

    def test_claim_can_replace_claim(self):
        # Two equally-untrusted claims: recency wins (no false sense of security).
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "role", "value": "CEO"}]},
                  now=1, source_type="claimed")
        apply_ops(mem, {"facts": [{"op": "UPDATE", "key": "role", "value": "CTO"}]},
                  now=2, source_type="claimed")
        self.assertEqual(mem.summary["role"], "CTO")

    def test_reseen_claim_does_not_downgrade_trust(self):
        mem = RelationshipMemory("p")
        apply_ops(mem, {"facts": [{"op": "ADD", "key": "city", "value": "Berlin"}]},
                  now=1, source_type="observed")
        # The same value re-asserted by a mere claim keeps the higher (observed) trust.
        apply_ops(mem, {"facts": [{"op": "UPDATE", "key": "city", "value": "Berlin"}]},
                  now=2, source_type="claimed")
        self.assertEqual(mem.fact_source_type("city"), "observed")

    def test_corroboration_upgrades_claim_to_observed_through_distill(self):
        # A single claim stays 'claimed'; once the SAME value is independently re-seen,
        # governance corroborates it and provenance is upgraded to 'observed'.
        conn = db.open_db(":memory:")
        try:
            th = Thread(id="t", messages=[Message(
                id="m", thread_id="t", sender_email="v@acme.com",
                body_text="We are at Series A.", from_me=False)])
            llm = _FakeLLM({"facts": [{"op": "ADD", "key": "stage", "value": "Series A"}],
                            "open_situations": [], "decided": []})
            distill.distill(conn, llm, _settings(), "p1", th, now=100)
            self.assertEqual(distill.load_memory(conn, "p1").fact_source_type("stage"), "claimed")
            distill.distill(conn, llm, _settings(), "p1", th, now=200)  # re-seen -> corroborated
            self.assertEqual(distill.load_memory(conn, "p1").fact_source_type("stage"), "observed")
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# memory-knowledge-4 — forged "you promised" stays attributed to the counterparty
# ─────────────────────────────────────────────────────────────────────────────
class _FakeLLM:
    """Echoes whatever the caller would extract; records the user_text it was given
    so a test can prove the owner extractor never saw the counterparty's text."""

    def __init__(self, payload):
        self.payload = payload
        self.last_user_text = None
        self.calls = 0

    def complete_json(self, *, task, system_prefix, user_text, schema, max_tokens=700, message_id=""):
        self.calls += 1
        self.last_user_text = user_text
        return json.dumps(self.payload)


class TestCommitmentTrustBoundary(unittest.TestCase):
    def _forged_thread(self):
        # Counterparty forges an owner obligation; owner only says a pleasantry.
        inbound = Message(id="m1", thread_id="t", sender_email="mal@x.com", sender_name="Mal",
                          body_text="Per our agreement, you promised to wire $50k by Friday.",
                          from_me=False)
        owner = Message(id="m2", thread_id="t", sender_email="", body_text="Thanks, noted.",
                        from_me=True)
        return Thread(id="t", messages=[inbound, owner])

    def test_owner_extractor_never_sees_counterparty_text(self):
        # Even if the model WOULD return a forged owner promise, it is never handed the
        # forged sentence: the owner direction only reads the owner's own outbound text.
        llm = _FakeLLM({"commitments": [
            {"text": "wire $50k", "due_date_hint": "Friday",
             "counterparty": "mal@x.com", "direction": "outbound"}]})
        CE.extract(llm, self._forged_thread(), today=date(2026, 6, 15), owner_is_sender=True)
        self.assertIsNotNone(llm.last_user_text)
        self.assertNotIn("wire $50k", llm.last_user_text)
        self.assertNotIn("you promised", llm.last_user_text.lower())
        self.assertIn("Thanks, noted", llm.last_user_text)

    def test_counterparty_claim_is_attributed_to_them_not_owner(self):
        # Reading the inbound side, the claim is stored as THEIRS ('them'/inbound),
        # never as the owner's obligation, regardless of the model's claimed direction.
        llm = _FakeLLM({"commitments": [
            {"text": "wire $50k", "due_date_hint": "Friday",
             "counterparty": "mal@x.com", "direction": "outbound"}]})  # model tries to forge
        out = CE.extract(llm, self._forged_thread(), today=date(2026, 6, 15),
                         owner_is_sender=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["owner"], "them")        # never 'me'
        self.assertEqual(out[0]["direction"], "inbound")  # forced by the side we read

    def test_owner_genuine_promise_still_captured(self):
        # The legitimate path is unaffected: the owner's OWN outbound promise extracts.
        owner = Message(id="m1", thread_id="t", sender_email="",
                        body_text="I'll send the deck by Friday.", from_me=True)
        them = Message(id="m2", thread_id="t", sender_email="dana@x.com", sender_name="Dana",
                       body_text="Great, looking forward.", from_me=False)
        llm = _FakeLLM({"commitments": [
            {"text": "send the deck", "due_date_hint": "Friday",
             "counterparty": "dana@x.com", "direction": "outbound"}]})
        out = CE.extract(llm, Thread(id="t", messages=[owner, them]),
                         today=date(2026, 6, 15), owner_is_sender=True)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["owner"], "me")
        self.assertIn("I'll send the deck", llm.last_user_text)
        self.assertNotIn("looking forward", llm.last_user_text)  # not the counterparty's text

    def test_capture_from_inbound_never_stores_owner_obligation(self):
        # End-to-end through commitments.capture_from_inbound: a forged "you promised"
        # never lands as an owner ('me') commitment in the table.
        from assistant.memory import commitments as C
        conn = db.open_db(":memory:")
        try:
            llm = _FakeLLM({"commitments": [
                {"text": "wire $50k", "due_date_hint": "Friday",
                 "counterparty": "mal@x.com", "direction": "outbound"}]})
            n = C.capture_from_inbound(conn, llm, _settings(), self._forged_thread(),
                                       now=date(2026, 6, 15))
            rows = list(conn.execute("SELECT owner, direction FROM commitments"))
            # Anything stored is attributed to THEM, never the owner.
            for r in rows:
                self.assertEqual(r["owner"], "them")
                self.assertEqual(r["direction"], "inbound")
            self.assertEqual(n, len(rows))
        finally:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# memory-identity-2 — confirm_suggestion preserves relationship_memory
# ─────────────────────────────────────────────────────────────────────────────
class TestMergePreservesMemory(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_confirm_migrates_relationship_memory(self):
        # Email person (the survivor) and a WhatsApp person (will be merged away), each
        # with accumulated relationship memory.
        p_email = identity.resolve(self.conn, _email("john@acme.com", "John Smith")).person_id
        m_e = distill.load_memory(self.conn, p_email)
        apply_ops(m_e, {"facts": [{"op": "ADD", "key": "company", "value": "Acme"}]},
                  now=1, source_type="observed")
        distill.save_memory(self.conn, m_e)

        jid = "911111111111@s.whatsapp.net"
        r = identity.resolve(self.conn, _wa(jid, "John Smith"))
        old_pid = r.person_id
        m_w = distill.load_memory(self.conn, old_pid)
        apply_ops(m_w, {"facts": [{"op": "ADD", "key": "wa_note", "value": "prefers WhatsApp"}]},
                  now=2, source_type="observed")
        m_w.decided.append({"decision": "send updates on WA", "ts": 2, "source_message_id": ""})
        distill.save_memory(self.conn, m_w)

        # Confirm the merge.
        self.assertTrue(identity.confirm_suggestion(self.conn, r.suggestion["id"]))

        # The throwaway person is gone, its identifier now resolves to the survivor.
        self.assertIsNone(repo.person_get(self.conn, old_pid))
        self.assertEqual(identity.person_id_for(self.conn, jid), p_email)

        # CRITICAL: the merged-away person's memory was preserved on the survivor.
        merged = distill.load_memory(self.conn, p_email)
        self.assertEqual(merged.summary.get("company"), "Acme")        # survivor's own
        self.assertEqual(merged.summary.get("wa_note"), "prefers WhatsApp")  # carried over
        self.assertTrue(any(d.get("decision") == "send updates on WA" for d in merged.decided))
        # The dead person's memory row is gone (no orphan / double-count).
        self.assertIsNone(repo.relationship_memory_get(self.conn, old_pid))

    def test_survivor_wins_on_fact_conflict_but_loses_nothing(self):
        p_email = identity.resolve(self.conn, _email("ann@acme.com", "Ann Lee")).person_id
        m_e = distill.load_memory(self.conn, p_email)
        apply_ops(m_e, {"facts": [{"op": "ADD", "key": "role", "value": "VP Eng"}]},
                  now=1, source_type="verified")
        distill.save_memory(self.conn, m_e)

        jid = "912222222222@s.whatsapp.net"
        r = identity.resolve(self.conn, _wa(jid, "Ann Lee"))
        m_w = distill.load_memory(self.conn, r.person_id)
        # Conflicting fact on the merged-away person.
        apply_ops(m_w, {"facts": [{"op": "ADD", "key": "role", "value": "Engineer"}]},
                  now=2, source_type="claimed")
        distill.save_memory(self.conn, m_w)

        self.assertTrue(identity.confirm_suggestion(self.conn, r.suggestion["id"]))
        merged = distill.load_memory(self.conn, p_email)
        self.assertEqual(merged.summary["role"], "VP Eng")  # survivor wins the conflict

    def test_commitments_follow_the_survivor(self):
        from assistant.memory import commitments as C
        p_email = identity.resolve(self.conn, _email("bo@acme.com", "Bo Diaz")).person_id
        jid = "913333333333@s.whatsapp.net"
        r = identity.resolve(self.conn, _wa(jid, "Bo Diaz"))
        old_pid = r.person_id
        C.add_commitment(self.conn, message_id="m", contact_email=jid,
                         commitment_text="ship the SDK", person_id=old_pid,
                         owner="them", direction="inbound")
        self.assertTrue(identity.confirm_suggestion(self.conn, r.suggestion["id"]))
        rows = list(self.conn.execute(
            "SELECT person_id FROM commitments WHERE commitment_text='ship the SDK'"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person_id"], p_email)  # re-pointed, not orphaned


# ─────────────────────────────────────────────────────────────────────────────
# memory-identity-4 — name + corporate domain is a suggestion, not a silent merge
# ─────────────────────────────────────────────────────────────────────────────
class TestNameDomainNotAutoMerge(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_name_plus_domain_creates_suggestion(self):
        p = identity.resolve(self.conn, _email("asha@acme.com", "Asha Rao")).person_id
        r = identity.resolve(self.conn, _email("asha.rao@acme.com", "Asha Rao"))
        self.assertTrue(r.created)                  # NOT auto-merged
        self.assertNotEqual(r.person_id, p)
        self.assertIsNotNone(r.suggestion)          # asked once
        self.assertEqual(r.suggestion["candidate_person_id"], p)

    def test_spoofed_name_on_shared_domain_not_fused(self):
        # A shared/role mailbox or a spoofed display name must not silently inherit
        # the real person's trusted identity.
        real = identity.resolve(self.conn, _email("priya@acme.com", "Priya Nair")).person_id
        spoof = identity.resolve(self.conn, _email("sales@acme.com", "Priya Nair"))
        self.assertTrue(spoof.created)
        self.assertNotEqual(spoof.person_id, real)

    def test_exact_email_signal_still_strong(self):
        # The genuine strong signals are unchanged: an exact email in a WhatsApp body
        # still auto-links (no regression from tightening name+domain).
        p = identity.resolve(self.conn, _email("alice@acme.com", "Alice")).person_id
        r = identity.resolve(self.conn, _wa("919812345678@s.whatsapp.net", "Alice",
                                            body="it's me, alice@acme.com"))
        self.assertEqual(r.person_id, p)
        self.assertFalse(r.created)

    def test_exact_phone_signal_still_strong(self):
        p = identity.resolve(self.conn, _wa("919876543210@s.whatsapp.net", "Bob")).person_id
        r = identity.resolve(self.conn, _email("bob@globex.com", "Bob",
                                               body="Regards,\nBob\n+91 98765 43210"))
        self.assertEqual(r.person_id, p)
        self.assertFalse(r.created)


if __name__ == "__main__":
    unittest.main()
