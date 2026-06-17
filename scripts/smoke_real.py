"""READ-ONLY real smoke test: real OpenRouter + real Gmail, changes nothing.

Pulls a few recent inbox threads, runs the real classifier + tier engine, prints
the decision for each, and drafts one sample reply. No archive/label/send, and it
uses a throwaway DB so it never touches the running app's ledger/historyId.
"""

import tempfile

from assistant.brain import classifier
from assistant.brain.tiers import TierConfig, decide
from assistant.config import load_settings
from assistant.llm.client import LLMClient
from assistant.action import drafting
from assistant.memory import contacts as mem_contacts
from assistant.memory import retrieval
from assistant.storage import db

s = load_settings()
llm = LLMClient(s)

print("=== 1) OpenRouter reachable + JSON classify works ===")
try:
    from assistant.brain import schema
    raw = llm.classify(
        system_prefix="You are a triage classifier.",
        thread_text="From: a friend\nSubject: lunch\nWant to grab lunch Friday?",
        schema=schema.DECISION_JSON_SCHEMA,
    )
    d = schema.parse_decision(raw)
    print(f"   judge model OK ({s.judge_model}); parsed tier={d.proposed_tier} "
          f"cat={d.category} failsafe={d.is_failsafe}")
except Exception as exc:  # noqa: BLE001
    print(f"   !! classify failed: {exc}")

print("\n=== 2) Real Gmail (read-only) + full pipeline on recent inbox ===")
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
conn = db.open_db(tmp)
try:
    from assistant.ingest.gmail_source import GmailSource
    mail = GmailSource(conn, s)
    mail.connect()
    resp = mail.service.users().messages().list(
        userId="me", q="in:inbox", maxResults=4
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    print(f"   pulled {len(ids)} recent inbox message(s)\n")

    cfg = TierConfig.from_settings(s)
    first_reply = None
    for mid in ids:
        th = mail.get_thread(mid)
        inbound = th.latest_inbound or th.latest
        contact = mem_contacts.resolve_sender(conn, inbound) if inbound else None
        ctx = retrieval.get_context(conn, th, contact) if contact else None
        dec = classifier.classify_thread(conn, llm, th, ctx, prompts_dir=s.prompts_dir)
        fin = decide(th, dec, contact, cfg)
        who = (inbound.sender_name or inbound.sender_email) if inbound else "?"
        print(f"   • from {who[:38]:38}  subj={ (th.subject or '')[:30]:30}")
        print(f"     → TIER {int(fin.final_tier)}  cat={dec.category:22} conf={dec.confidence:.2f}"
              f"  {('[' + fin.surfaced_reason + ']') if fin.surfaced_reason else ''}")
        print(f"       {dec.one_line_summary[:90]}")
        if first_reply is None and int(fin.final_tier) >= 2 and dec.needs_reply:
            first_reply = (th, contact, fin)

    if first_reply:
        th, contact, fin = first_reply
        print("\n=== 3) Sample draft in your voice (no em-dashes), nothing sent ===")
        draft = drafting.draft_reply(conn, llm, s, th, contact, fin)
        print("   " + draft.replace("\n", "\n   "))
        print(f"\n   em-dash present? {'—' in draft}  (should be False)")
    else:
        print("\n   (no reply-worthy thread in the sample to draft for)")
finally:
    conn.close()
print("\n=== done — nothing was changed or sent ===")
